"""SQLite job store: assignment, status tracking and resume for large runs.

One row per soul in ``souls``. Workers claim batches (pending -> running) and
then mark a terminal status (success/failed/skipped), so a crash can never
re-submit finished rows: on resume anything still ``running`` is re-queued.
The single-row ``job`` table holds run metadata. Standard library only -
no new dependencies.
"""

from __future__ import annotations

import csv
import json
import os
import sqlite3
import time
from typing import Sequence

from config import Config

STATUSES = ("pending", "running", "added", "success", "failed", "skipped")
TERMINAL = ("success", "failed", "skipped")
INSERT_CHUNK = 20_000


class JobError(ValueError):
    """User-facing validation problem (bad quota, missing columns, ...)."""


def parse_quota(value):
    """``"50000"`` / ``"50,000"`` / ``"50k"`` -> int; blank -> None (even share)."""
    if value is None:
        return None
    text = str(value).strip().replace(",", "")
    if not text or text.lower() in ("nan", "none", "-", "nat"):
        return None
    multiplier = 1
    if text.lower().endswith("k"):
        multiplier = 1000
        text = text[:-1]
    try:
        quota = int(float(text) * multiplier)
    except ValueError:
        raise JobError(
            f"quota {value!r} is not a number - use 50000, 50,000 or 50k"
        ) from None
    if quota < 0:
        raise JobError(f"quota {value!r} must be zero or positive")
    return quota


def assign(accounts: Sequence[dict], total: int) -> tuple[list[int], list[str]]:
    """Slice ``total`` rows across ``accounts`` (in file order).

    Accounts with an explicit ``quota`` get exactly that many rows, taking them
    from the front of the file in order. Accounts without a quota split what is
    left evenly (the first ``remainder`` accounts get one extra row).

    Returns ``(targets, warnings)``.
    """
    targets = [0] * len(accounts)
    warnings: list[str] = []
    blanks: list[int] = []
    used = 0

    for i, acct in enumerate(accounts):
        quota = acct.get("quota")
        if quota is None:
            blanks.append(i)
            continue
        take = min(quota, max(total - used, 0))
        targets[i] = take
        used += take
        if take < quota:
            warnings.append(
                f"{acct.get('account_id', i)}: quota {quota:,} exceeds the rows "
                f"left - assigned {take:,}"
            )

    left = total - used
    if blanks:
        base, extra = divmod(max(left, 0), len(blanks))
        for n, i in enumerate(blanks):
            targets[i] = base + (1 if n < extra else 0)
        if left <= 0 and any(a.get("quota") for a in accounts):
            warnings.append(
                "quotas use every row - accounts without a quota got 0 rows"
            )
    elif left > 0:
        warnings.append(
            f"{left:,} rows are unassigned (every account has an explicit quota)"
        )
    return targets, warnings


def _iter_data_rows(souls_csv: str):
    """Yield data rows (skips header and blank lines) - shared by count+insert
    so both passes always agree on the row set."""
    with open(souls_csv, newline="", encoding="utf-8-sig") as fh:
        reader = csv.reader(fh)
        next(reader, None)
        for row in reader:
            if any(cell.strip() for cell in row):
                yield row


def count_rows(souls_csv: str) -> int:
    return sum(1 for _ in _iter_data_rows(souls_csv))


def read_header(souls_csv: str) -> list[str]:
    with open(souls_csv, newline="", encoding="utf-8-sig") as fh:
        header = next(csv.reader(fh), None)
    return [str(col).strip() for col in (header or [])]


_SCHEMA = """
CREATE TABLE IF NOT EXISTS job (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    mode TEXT NOT NULL,
    state TEXT NOT NULL,
    header_json TEXT NOT NULL,
    warnings TEXT NOT NULL DEFAULT '[]',
    total INTEGER NOT NULL,
    created_at REAL NOT NULL,
    started_at REAL,
    finished_at REAL
);
CREATE TABLE IF NOT EXISTS accounts (
    seq INTEGER PRIMARY KEY,
    account_id TEXT UNIQUE NOT NULL,
    email TEXT NOT NULL,
    password TEXT NOT NULL,
    quota INTEGER,
    assigned INTEGER NOT NULL DEFAULT 0,
    state TEXT NOT NULL DEFAULT 'queued'
);
CREATE TABLE IF NOT EXISTS souls (
    row_id INTEGER PRIMARY KEY,
    account_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    error TEXT,
    payload TEXT NOT NULL,
    updated_at REAL
);
CREATE INDEX IF NOT EXISTS idx_souls_account
    ON souls(account_id, status, row_id);
CREATE INDEX IF NOT EXISTS idx_souls_status ON souls(status);
"""
# -- end part1 --


class JobStore:
    """All database access for the single active job."""

    def __init__(self, db_path: str | None = None):
        self.db_path = db_path or Config.JOB_DB
        directory = os.path.dirname(self.db_path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=60)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    # ---------------------------- job lifecycle ----------------------------
    def get_job(self) -> dict | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM job WHERE id = 1").fetchone()
        if row is None:
            return None
        job = dict(row)
        job["warnings"] = json.loads(job.get("warnings") or "[]")
        job["header"] = json.loads(job.pop("header_json") or "[]")
        return job

    def reset(self) -> None:
        """Delete the current job entirely (start-over path)."""
        with self._connect() as conn:
            conn.execute("DELETE FROM souls")
            conn.execute("DELETE FROM accounts")
            conn.execute("DELETE FROM job")

    def create_job(
        self,
        mode: str,
        accounts: Sequence[dict],
        header: Sequence[str],
        souls_csv: str,
    ) -> dict:
        """Assign every soul to an account and persist the plan.

        Rows are streamed from ``souls_csv`` in chunks so a million-row file
        never has to fit in memory together with its JSON payloads.
        """
        if not accounts:
            raise JobError("no accounts uploaded")
        if not header:
            raise JobError("the souls file is empty")
        if self.get_job() is not None:
            raise JobError("a job already exists - reset it before a new run")

        total = count_rows(souls_csv)
        if total == 0:
            raise JobError("the souls file has no data rows")

        targets, warnings = assign(accounts, total)
        ranges = []
        cursor = 0
        for target in targets:
            ranges.append((cursor, cursor + target))
            cursor += target

        conn = self._connect()
        try:
            with conn:
                conn.execute(
                    "INSERT INTO job (id, mode, state, header_json, warnings,"
                    " total, created_at) VALUES (1, ?, 'pending', ?, ?, ?, ?)",
                    (
                        mode,
                        json.dumps(list(header)),
                        json.dumps(warnings),
                        total,
                        time.time(),
                    ),
                )
                for seq, acct in enumerate(accounts):
                    conn.execute(
                        "INSERT INTO accounts (seq, account_id, email, password,"
                        " quota, assigned) VALUES (?, ?, ?, ?, ?, ?)",
                        (
                            seq,
                            acct["account_id"],
                            acct["email"],
                            acct["password"],
                            acct.get("quota"),
                            targets[seq],
                        ),
                    )

                chunk: list[tuple] = []
                row_id = 0
                slot = 0
                for row in _iter_data_rows(souls_csv):
                    while slot < len(ranges) and row_id >= ranges[slot][1]:
                        slot += 1
                    if slot >= len(ranges):
                        break  # unassigned tail (already reported as a warning)
                    row_id += 1
                    payload = dict(zip(header, row))
                    chunk.append(
                        (
                            row_id,
                            accounts[slot]["account_id"],
                            json.dumps(payload, ensure_ascii=False),
                        )
                    )
                    if len(chunk) >= INSERT_CHUNK:
                        conn.executemany(
                            "INSERT INTO souls (row_id, account_id, payload)"
                            " VALUES (?, ?, ?)",
                            chunk,
                        )
                        chunk.clear()
                if chunk:
                    conn.executemany(
                        "INSERT INTO souls (row_id, account_id, payload)"
                        " VALUES (?, ?, ?)",
                        chunk,
                    )
                if row_id != sum(targets):
                    raise JobError(
                        f"assignment mismatch: planned {sum(targets):,} rows "
                        f"but inserted {row_id:,}"
                    )
        finally:
            conn.close()

        return {
            "total": total,
            "warnings": warnings,
            "plan": [
                {"account_id": a["account_id"], "assigned": t}
                for a, t in zip(accounts, targets)
            ],
        }
    def apply_quotas(self, accounts: Sequence[dict]) -> dict:
        """Re-slice still-pending rows so the plan matches ``accounts`` quotas.

        Used when accounts are re-uploaded mid-job (Resume): the job was created
        with one quota plan, the new file brings another. Finished rows never
        move - the account that processed a row keeps the credit - so quotas are
        matched as closely as the finished rows allow. Pending rows of accounts
        that left the file are handed to the new plan.

        Returns ``{"moved": int, "warnings": [...], "plan": [...]}``.
        """
        if not accounts:
            raise JobError("no accounts uploaded")
        now = time.time()
        with self._connect() as conn:
            with conn:
                # rows a crashed run left mid-flight count as pending again
                conn.execute(
                    "UPDATE souls SET status = 'pending', updated_at = ?"
                    " WHERE status = 'running'",
                    (now,),
                )
                total = conn.execute("SELECT COUNT(*) FROM souls").fetchone()[0]
                if not total:
                    raise JobError("the job has no rows")
                targets, warnings = assign(accounts, total)

                held: dict[str, int] = {}
                pend: dict[str, int] = {}
                for row in conn.execute(
                    "SELECT account_id,"
                    " SUM(status != 'pending') AS done,"
                    " SUM(status = 'pending') AS open"
                    " FROM souls GROUP BY account_id"
                ):
                    held[row["account_id"]] = int(row["done"] or 0)
                    pend[row["account_id"]] = int(row["open"] or 0)

                target_pending: dict[str, int] = {}
                for acct, target in zip(accounts, targets):
                    name = acct["account_id"]
                    quota = acct.get("quota")
                    if quota is not None and held.get(name, 0) > quota:
                        warnings.append(
                            f"{name}: {held[name]:,} finished rows already exceed"
                            f" the quota {quota:,} - only pending rows can be"
                            " re-sliced"
                        )
                    target_pending[name] = max(target - held.get(name, 0), 0)

                # rows that must move: accounts over their target give up the
                # excess (accounts that left the file give up every pending row)
                pool: list[int] = []
                for name, open_count in pend.items():
                    give = open_count - target_pending.get(name, 0)
                    if give > 0:
                        pool.extend(
                            row["row_id"]
                            for row in conn.execute(
                                "SELECT row_id FROM souls WHERE account_id = ?"
                                " AND status = 'pending' ORDER BY row_id LIMIT ?",
                                [name, give],
                            )
                        )

                def _move(row_ids: list[int], name: str) -> None:
                    for i in range(0, len(row_ids), 500):
                        chunk = row_ids[i : i + 500]
                        marks = ",".join("?" * len(chunk))
                        conn.execute(
                            f"UPDATE souls SET account_id = ?, updated_at = ?"
                            f" WHERE row_id IN ({marks})",
                            [name, now, *chunk],
                        )

                moved = 0
                for acct in accounts:
                    name = acct["account_id"]
                    need = target_pending[name] - min(
                        pend.get(name, 0), target_pending[name]
                    )
                    if need <= 0 or not pool:
                        continue
                    take = pool[:need]
                    del pool[:need]
                    _move(take, name)
                    moved += len(take)

                # rows that fit no quota still belong to someone: spread them
                if pool:
                    warnings.append(
                        f"{len(pool):,} rows did not fit the quotas (finished rows"
                        " pin accounts over quota) - they were split across the"
                        " accounts instead"
                    )
                    for n, row_id in enumerate(pool):
                        _move([row_id], accounts[n % len(accounts)]["account_id"])
                    moved += len(pool)

                # sync the accounts table with the new plan
                seqs = {
                    row["account_id"]: row["seq"]
                    for row in conn.execute("SELECT seq, account_id FROM accounts")
                }
                counts = {
                    row["account_id"]: int(row["c"] or 0)
                    for row in conn.execute(
                        "SELECT account_id, COUNT(*) c FROM souls GROUP BY account_id"
                    )
                }
                next_seq = (max(seqs.values()) + 1) if seqs else 0
                in_file = {acct["account_id"] for acct in accounts}
                plan = []
                for acct in accounts:
                    name = acct["account_id"]
                    assigned = counts.get(name, 0)
                    plan.append({"account_id": name, "assigned": assigned})
                    if name in seqs:
                        conn.execute(
                            "UPDATE accounts SET email = ?, password = ?, quota = ?,"
                            " assigned = ? WHERE account_id = ?",
                            [
                                acct["email"],
                                acct["password"],
                                acct.get("quota"),
                                assigned,
                                name,
                            ],
                        )
                    else:
                        conn.execute(
                            "INSERT INTO accounts (seq, account_id, email, password,"
                            " quota, assigned) VALUES (?, ?, ?, ?, ?, ?)",
                            [
                                next_seq,
                                name,
                                acct["email"],
                                acct["password"],
                                acct.get("quota"),
                                assigned,
                            ],
                        )
                        next_seq += 1
                for name in seqs:
                    if name not in in_file:
                        conn.execute(
                            "UPDATE accounts SET assigned = ? WHERE account_id = ?",
                            [counts.get(name, 0), name],
                        )
        return {"moved": moved, "warnings": warnings, "plan": plan}
# -- end part2 --


    # ------------------------- worker operations -------------------------
    def begin(self) -> dict:
        """Mark the job running and re-queue rows left behind by a crash."""
        now = time.time()
        with self._connect() as conn:
            with conn:
                conn.execute(
                    "UPDATE job SET state = 'running', started_at = ?,"
                    " finished_at = NULL WHERE id = 1",
                    (now,),
                )
                conn.execute(
                    "UPDATE souls SET status = 'pending', updated_at = ?"
                    " WHERE status = 'running'",
                    (now,),
                )
                conn.execute(
                    "UPDATE accounts SET state = 'queued' WHERE account_id IN"
                    " (SELECT DISTINCT account_id FROM souls"
                    "  WHERE status = 'pending')"
                )
        return self.get_job() or {}

    def accounts(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT seq, account_id, email, password, quota, assigned"
                " FROM accounts ORDER BY seq"
            ).fetchall()
        return [dict(row) for row in rows]

    def next_batch(
        self,
        account_id: str,
        statuses: Sequence[str] = ("pending",),
        size: int | None = None,
    ) -> list[dict]:
        """Claim up to ``size`` rows for one account (their status -> running)."""
        size = size or Config.BATCH_SIZE
        statuses = list(statuses) or ["pending"]
        marks = ",".join("?" * len(statuses))
        now = time.time()
        with self._connect() as conn:
            with conn:
                ids = [
                    row["row_id"]
                    for row in conn.execute(
                        f"SELECT row_id FROM souls WHERE account_id = ?"
                        f" AND status IN ({marks}) ORDER BY row_id LIMIT ?",
                        [account_id, *statuses, size],
                    )
                ]
                if not ids:
                    return []
                marks = ",".join("?" * len(ids))
                conn.execute(
                    f"UPDATE souls SET status = 'running', updated_at = ?"
                    f" WHERE row_id IN ({marks})",
                    [now, *ids],
                )
                rows = conn.execute(
                    f"SELECT row_id, payload FROM souls WHERE row_id IN ({marks})"
                    f" ORDER BY row_id",
                    ids,
                ).fetchall()
        return [dict(row) for row in rows]

    def mark(self, row_id: int, status: str, error: str | None = None) -> None:
        if status not in STATUSES:
            raise JobError(f"invalid status {status!r}")
        with self._connect() as conn:
            with conn:
                conn.execute(
                    "UPDATE souls SET status = ?, error = ?, updated_at = ?"
                    " WHERE row_id = ?",
                    (status, error, time.time(), row_id),
                )

    def set_account_state(self, account_id: str, state: str) -> None:
        with self._connect() as conn:
            with conn:
                conn.execute(
                    "UPDATE accounts SET state = ? WHERE account_id = ?",
                    (state, account_id),
                )

    def finish(self, state: str = "done") -> dict:
        with self._connect() as conn:
            with conn:
                conn.execute(
                    "UPDATE job SET state = ?, finished_at = ? WHERE id = 1",
                    (state, time.time()),
                )
        return self.summary() or {}

    # ------------------------------ reporting ------------------------------
    def summary(self) -> dict | None:
        """Full progress snapshot: totals + per-account rows (one indexed pass)."""
        job = self.get_job()
        if job is None:
            return None
        with self._connect() as conn:
            accounts = [
                dict(row)
                for row in conn.execute(
                    "SELECT seq, account_id, quota, assigned, state"
                    " FROM accounts ORDER BY seq"
                )
            ]
            stats = {
                row["account_id"]: dict(row)
                for row in conn.execute(
                    "SELECT account_id,"
                    " SUM(status = 'success') AS ok,"
                    " SUM(status = 'failed') AS fail,"
                    " SUM(status = 'skipped') AS skip,"
                    " SUM(status = 'added') AS added,"
                    " SUM(status = 'pending') AS pending,"
                    " SUM(status = 'running') AS running"
                    " FROM souls GROUP BY account_id"
                )
            }
        keys = ("ok", "fail", "skip", "added", "pending", "running")
        for acct in accounts:
            row_stats = stats.get(acct["account_id"], {})
            for key in keys:
                acct[key] = int(row_stats.get(key) or 0)
            acct["done"] = acct["ok"] + acct["fail"] + acct["skip"]
        totals = {
            key: sum(acct[key] for acct in accounts) for key in (*keys, "done")
        }
        totals["processed"] = totals["done"] + totals["running"] + totals["added"]
        return {**job, **totals, "accounts": accounts}

    def has_rows(self, account_id: str, statuses: Sequence[str]) -> bool:
        """True when the account still has rows in one of ``statuses``."""
        statuses = list(statuses)
        if not statuses:
            return False
        marks = ",".join("?" * len(statuses))
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT 1 FROM souls WHERE account_id = ?"
                f" AND status IN ({marks}) LIMIT 1",
                [account_id, *statuses],
            ).fetchone()
        return row is not None

    def iter_failures(self):
        """Yield the payload dict of every failed row, in file order."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT payload FROM souls WHERE status = 'failed'"
                " ORDER BY row_id"
            )
            for row in rows:
                yield json.loads(row["payload"])
# -- end part3 --


