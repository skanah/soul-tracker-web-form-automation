"""Requeue transient failed rows + orphaned running rows back to pending.

Terminal failures (kept): phone already in records, blank gender/name (bad source data).
Run ONLY while the worker is stopped (it self-checks via the job state when possible).
"""
import sqlite3
import time

DB = "data/job.db"

# Failures that can never succeed on retry -> stay 'failed'
TERMINAL = (
    "phone verification failed: A soul with this phone number is already in your records.",
    "Invalid gender:",
    "firstName and lastName are required",
)

def is_terminal(err: str) -> bool:
    err = (err or "").strip()
    return any(err == t or err.startswith(t) for t in TERMINAL)

def main():
    c = sqlite3.connect(DB)
    c.execute("PRAGMA journal_mode=WAL")
    now = time.time()
    total_before = c.execute("SELECT COUNT(*) FROM souls").fetchone()[0]

    # 1. Orphaned running rows (server died mid-flight) -> pending
    cur = c.execute(
        "UPDATE souls SET status='pending', error=NULL, updated_at=? "
        "WHERE status='running'",
        (now,),
    )
    requeued_running = cur.rowcount

    # 2. Transient failed rows -> pending (terminal ones untouched)
    rows = c.execute(
        "SELECT row_id, error FROM souls WHERE status='failed'"
    ).fetchall()
    transient = [r[0] for r in rows if not is_terminal(r[1])]
    kept_terminal = len(rows) - len(transient)
    if transient:
        c.executemany(
            "UPDATE souls SET status='pending', error=NULL, updated_at=? "
            "WHERE row_id=?",
            [(now, rid) for rid in transient],
        )

    # 3. Reset account state for any account that still has pending rows
    cur = c.execute(
        "UPDATE accounts SET state='pending' "
        "WHERE account_id IN (SELECT DISTINCT account_id FROM souls WHERE status='pending') "
        "AND state != 'pending'"
    )
    states_reset = cur.rowcount

    # Verify totals still add up BEFORE committing (no row may vanish)
    counts = dict(c.execute("SELECT status, COUNT(*) FROM souls GROUP BY status").fetchall())
    total = sum(counts.values())
    assert total == total_before, (
        f"ROW COUNT MISMATCH ({total} != {total_before}) - rolling back"
    )
    c.commit()

    print(f"requeued running : {requeued_running}")
    print(f"requeued failed  : {len(transient)} (terminal kept: {kept_terminal})")
    print(f"accounts reset   : {states_reset}")
    print(f"status counts    : {counts}")
    print(f"total rows       : {total}")
    c.close()

if __name__ == "__main__":
    main()
