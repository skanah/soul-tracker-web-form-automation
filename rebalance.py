"""Rebalance pending rows so ALL accounts progress in parallel.

The source CSV lost its phone/email columns from row 55,561 onward, so the
33 tail accounts were assigned slices with nothing submittable. This script
moves pending rows from over-loaded accounts to the idle ones until every
account holds an equal share (~total/76), updating accounts.assigned so the
dashboard totals stay exact. Safe to run only while no worker is live.

Run: .venv/Scripts/python.exe rebalance.py
"""
import sqlite3
import sys
import time
from collections import deque

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

DB = "data/job.db"


def main() -> None:
    con = sqlite3.connect(DB)
    now = time.time()

    # 1. Anything left mid-flight by an interrupted run goes back to pending.
    cur = con.execute(
        "UPDATE souls SET status='pending', updated_at=? WHERE status='running'",
        (now,),
    )
    print("requeued running -> pending:", cur.rowcount)

    accounts = [r[0] for r in con.execute("SELECT account_id FROM accounts ORDER BY seq")]
    counts = dict(
        con.execute(
            "SELECT account_id, COUNT(*) FROM souls WHERE status='pending'"
            " GROUP BY account_id"
        )
    )
    total = sum(counts.values())
    base, extra = divmod(total, len(accounts))
    # First `extra` accounts (by seq) get one extra row so nothing is lost.
    targets = {a: base + (1 if i < extra else 0) for i, a in enumerate(accounts)}
    print(f"total pending: {total:,} across {len(accounts)} accounts -> target {base}/{base+1}")

    needies = deque(
        [a, targets[a] - counts.get(a, 0)]
        for a in accounts
        if counts.get(a, 0) < targets[a]
    )
    givers = [
        (a, counts[a] - targets[a])
        for a in accounts
        if counts.get(a, 0) > targets[a]
    ]

    moves = []  # (new_account, now, row_id)
    moved_out: dict[str, int] = {}
    moved_in: dict[str, int] = {}
    for donor, give in givers:
        row_ids = [
            r[0]
            for r in con.execute(
                "SELECT row_id FROM souls WHERE account_id=? AND status='pending'"
                " ORDER BY row_id DESC LIMIT ?",
                (donor, give),
            )
        ]
        for rid in row_ids:
            while needies and needies[0][1] == 0:
                needies.popleft()
            if not needies:
                break
            needy = needies[0][0]
            needies[0][1] -= 1
            moves.append((needy, now, rid))
            moved_out[donor] = moved_out.get(donor, 0) + 1
            moved_in[needy] = moved_in.get(needy, 0) + 1
        if not needies:
            break

    print(f"rows to move: {len(moves):,} from {len(moved_out)} accounts to {len(moved_in)} accounts")
    with con:
        con.executemany(
            "UPDATE souls SET account_id=?, updated_at=? WHERE row_id=?", moves
        )
        for acct, n in moved_out.items():
            con.execute(
                "UPDATE accounts SET assigned = assigned - ? WHERE account_id=?",
                (n, acct),
            )
        for acct, n in moved_in.items():
            con.execute(
                "UPDATE accounts SET assigned = assigned + ? WHERE account_id=?",
                (n, acct),
            )
        # Accounts previously 'done' with an empty slice now have work again.
        con.execute(
            "UPDATE accounts SET state='pending' WHERE state='done' AND account_id"
            " IN (SELECT account_id FROM souls WHERE status='pending')"
        )

    # ---- verification ----
    new_counts = dict(
        con.execute(
            "SELECT account_id, COUNT(*) FROM souls WHERE status='pending'"
            " GROUP BY account_id"
        )
    )
    vals = [new_counts.get(a, 0) for a in accounts]
    asg = con.execute("SELECT SUM(assigned) FROM accounts").fetchone()[0]
    total_after = sum(vals)
    statuses = dict(con.execute("SELECT status, COUNT(*) FROM souls GROUP BY status"))
    print("--- verification ---")
    print(f"pending per account: min {min(vals)}, max {max(vals)}, sum {total_after:,}")
    print(f"sum(assigned) = {asg:,} (must equal {sum(statuses.values()):,})")
    print("statuses:", statuses)
    if total_after != total or asg != sum(statuses.values()):
        print("!! MISMATCH - do not resume")
        sys.exit(1)
    print("OK - every account now has rows; safe to Resume.")


if __name__ == "__main__":
    main()
