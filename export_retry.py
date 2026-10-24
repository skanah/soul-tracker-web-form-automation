#!/usr/bin/env python3
"""Export a retry CSV from the current job store (data/job.db).

Keeps :  pending + running rows (never finished)
         failed rows with a TRANSIENT error (timeouts etc. - worth retrying)
Drops :  success/added  (done - never resubmit)
         skipped        (source rows with no phone/email - un-submittable)
         failed rows with a permanent error (already-in-records / bad data)
         kept rows that would fail form validation up front (blank contact,
         missing names, blank/invalid gender, no city)

Writes :  souls_retry.csv           -> upload this one via the dashboard
          souls_dropped_review.csv -> every dropped row + reason (data fixes)

Stdlib only. Run from the project root:
    .venv/Scripts/python.exe export_retry.py [--db data/job.db]
"""
from __future__ import annotations

import argparse
import csv
import json
import sqlite3
from collections import Counter

DEFAULT_DB = "data/job.db"
OUT = "souls_retry.csv"
DROP_OUT = "souls_dropped_review.csv"

# Same permanent-failure wording requeue.py treats as non-retryable.
PERMANENT_PREFIXES = (
    "phone verification failed: A soul with this phone number is already in your records.",
    "Invalid gender:",
    "firstName and lastName are required",
    "Either phone or email required",
)


def is_permanent(error) -> bool:
    error = (error or "").strip()
    return any(error == p or error.startswith(p) for p in PERMANENT_PREFIXES)


def blank(value) -> bool:
    return not str(value or "").strip()


def gender_ok(value) -> bool:
    return str(value or "").strip().lower() in ("male", "m", "female", "f")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", default=DEFAULT_DB)
    args = ap.parse_args()

    conn = sqlite3.connect(args.db, timeout=60)
    conn.row_factory = sqlite3.Row
    job = conn.execute("SELECT * FROM job WHERE id = 1").fetchone()
    if job is None:
        raise SystemExit("no job in the store - nothing to export")
    header = json.loads(job["header_json"])

    kept: list[dict] = []
    dropped: list[tuple[str, dict]] = []
    for row in conn.execute("SELECT status, error, payload FROM souls ORDER BY row_id"):
        payload = json.loads(row["payload"])
        status, error = row["status"], row["error"]
        reason = None
        if status in ("success", "added"):
            reason = f"done ({status})"
        elif status == "skipped":
            reason = "skipped - no phone/email in source"
        elif status == "failed":
            if is_permanent(error):
                reason = f"permanent failure: {(error or '')[:60]}"
        if reason is None:  # pending/running/transient-failed -> pre-validate
            if blank(payload.get("phone")) and blank(payload.get("email")):
                reason = "no phone or email"
            elif blank(payload.get("firstName")) or blank(payload.get("lastName")):
                reason = "missing first/last name"
            elif not gender_ok(payload.get("gender")):
                reason = f"invalid gender {str(payload.get('gender'))[:20]!r}"
            elif blank(payload.get("city")):
                reason = "no city"
        if reason is None:
            kept.append(payload)
        else:
            dropped.append((reason, payload))

    with open(OUT, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(header)
        for p in kept:
            w.writerow([p.get(c, "") for c in header])

    with open(DROP_OUT, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(["drop_reason", *header])
        for reason, p in dropped:
            w.writerow([reason, *[p.get(c, "") for c in header]])

    reasons = Counter(r for r, _ in dropped)
    print(f"job     : mode={job['mode']} state={job['state']} total={job['total']:,}")
    print(f"retry   : {OUT} -> {len(kept):,} rows (upload this one)")
    print(f"dropped : {len(dropped):,} rows -> {DROP_OUT}")
    for reason, n in reasons.most_common(12):
        print(f"  {n:>7,}  {reason}")
    if len(reasons) > 12:
        print(f"  ... + {len(reasons) - 12} more reasons")


if __name__ == "__main__":
    main()