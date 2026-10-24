#!/usr/bin/env python3
"""Filter a souls CSV against rows the current job already finished.

Reads the job store (data/job.db), collects the phone/email of every soul
whose status is in --done-statuses (default ``success,added``), then streams
a souls CSV and writes a copy WITHOUT those souls - so a fresh job never
re-submits people the previous run already added.

Run it from the project root (it is stdlib-only, any Python works):

    .venv/bin/python filter_done.py data/souls.csv souls_remaining.csv
        -> the not-done remainder of the CURRENT souls file (retry pass)

    .venv/bin/python filter_done.py /root/souls_1m.csv /root/souls_1m_filtered.csv
        -> the 1M file minus every soul the 54k job already finished

Run AFTER the current job shows done (green badge / ``GET /status``): rows
still pending/running keep being processed by the old job and would
duplicate in the new one.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sqlite3
import time

DEFAULT_DB = "data/job.db"
DEFAULT_DONE = "success,added"


def _digits(value) -> str | None:
    """Normalize a phone to bare digits (leading zeros dropped)."""
    d = re.sub(r"\D", "", str(value or ""))
    d = d.lstrip("0")
    return d or None


def _norm_email(value) -> str | None:
    e = str(value or "").strip().lower()
    return e or None


def _field(payload: dict, *names: str) -> str | None:
    """Case-insensitive column lookup that skips blank values."""
    low = {str(k).lower(): v for k, v in payload.items()}
    for name in names:
        v = low.get(name.lower())
        if v is not None and str(v).strip():
            return str(v).strip()
    return None


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=60)
    conn.row_factory = sqlite3.Row
    return conn


def print_job_snapshot(db_path: str) -> None:
    """Show the current job state + per-status counts (sanity check)."""
    with _connect(db_path) as conn:
        job = conn.execute(
            "SELECT mode, state, total, started_at, finished_at"
            " FROM job WHERE id = 1"
        ).fetchone()
        if job is None:
            raise SystemExit("no job in data/job.db - nothing to filter against")
        counts = {
            r["status"]: r["n"]
            for r in conn.execute(
                "SELECT status, COUNT(*) AS n FROM souls GROUP BY status"
            )
        }
    fmt = lambda ts: ts and time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))  # noqa: E731
    print(f"job     : mode={job['mode']} state={job['state']} total={job['total']:,}")
    print(f"started : {fmt(job['started_at'])}")
    print(f"finished: {fmt(job['finished_at'])}")
    for status in ("success", "added", "failed", "skipped", "running", "pending"):
        if counts.get(status):
            print(f"  {status:>8}: {counts[status]:,}")
    if counts.get("pending") or counts.get("running"):
        print(
            "\nWARNING: the job still has pending/running rows. Let it finish\n"
            "before filtering, or those souls will be processed by BOTH jobs\n"
            "(duplicate submissions)."
        )

def load_done_keys(db_path: str, statuses: list[str]) -> tuple[set[str], set[str], int]:
    """Phone-digit and email key sets for every soul in ``statuses``."""
    marks = ",".join("?" * len(statuses))
    phones: set[str] = set()
    emails: set[str] = set()
    rows = 0
    with _connect(db_path) as conn:
        for row in conn.execute(
            f"SELECT payload FROM souls WHERE status IN ({marks})", statuses
        ):
            payload = json.loads(row["payload"])
            phone = _digits(_field(payload, "phone"))
            email = _norm_email(_field(payload, "email"))
            if phone:
                phones.add(phone)
            if email:
                emails.add(email)
            rows += 1
    return phones, emails, rows


def filter_csv(
    in_path: str, out_path: str, phones: set[str], emails: set[str]
) -> tuple[int, int, int]:
    """Stream ``in_path`` -> ``out_path`` minus done souls. (total, kept, dropped)"""
    total = kept = dropped = 0
    with open(in_path, newline="", encoding="utf-8-sig") as src, open(
        out_path, "w", newline="", encoding="utf-8"
    ) as dst:
        reader = csv.reader(src)
        writer = csv.writer(dst, lineterminator="\n")
        header = next(reader, None)
        if header is None:
            raise SystemExit(f"{in_path}: no header row")
        writer.writerow(header)
        for row in reader:
            if not any(cell.strip() for cell in row):
                continue  # blank line - staging drops these too
            total += 1
            payload = dict(zip(header, row))
            phone = _digits(_field(payload, "phone"))
            email = _norm_email(_field(payload, "email"))
            if (phone and phone in phones) or (email and email in emails):
                dropped += 1
                continue
            kept += 1
            writer.writerow(row)
    return total, kept, dropped


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("input", help="souls CSV to filter (e.g. the new 1M file)")
    ap.add_argument("output", help="filtered souls CSV to write")
    ap.add_argument(
        "--db", default=DEFAULT_DB, help=f"job store path (default {DEFAULT_DB})"
    )
    ap.add_argument(
        "--done-statuses",
        default=DEFAULT_DONE,
        help=f"comma-separated statuses counted as done (default {DEFAULT_DONE})",
    )
    args = ap.parse_args()

    print_job_snapshot(args.db)
    statuses = [s.strip() for s in args.done_statuses.split(",") if s.strip()]
    phones, emails, done_rows = load_done_keys(args.db, statuses)
    print(f"\ndone rows in store ({','.join(statuses)}): {done_rows:,}")
    print(f"unique done keys  : phones={len(phones):,} emails={len(emails):,}")

    total, kept, dropped = filter_csv(args.input, args.output, phones, emails)
    print(f"\n{args.input}: {total:,} data rows")
    print(f"  dropped (done) : {dropped:,}")
    print(f"  kept           : {kept:,}")
    print(f"\nwrote {args.output}")


if __name__ == "__main__":
    main()