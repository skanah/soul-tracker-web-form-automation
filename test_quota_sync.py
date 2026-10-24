"""Validation for the quota fixes + resume quota sync (no data/ writes).

Run: .venv/Scripts/python.exe test_quota_sync.py
"""
import csv
import os
import sys
import tempfile

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
os.environ["AUTO_RESUME"] = "0"  # importing app must never start a real worker
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import app as app_mod  # noqa: E402  (imports config + automation too)
from automation.job_store import JobStore  # noqa: E402

fails: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("PASS " if cond else "FAIL ") + name + (f"  [{detail}]" if detail else ""))
    if not cond:
        fails.append(name)


tmpdir = tempfile.mkdtemp(prefix="soultracker-test-")

# ---- 1. account columns are matched case-insensitively -----------------------
src = os.path.join(tmpdir, "upload.csv")
with open(src, "w", newline="", encoding="utf-8") as fh:
    fh.write("Account_ID,Email,Password,Quota\n")
    fh.write("a1,a@x.com,pw,50k\n")
    fh.write("a2,b@x.com,pw,\n")
    fh.write('a3,c@x.com,pw,"2,000"\n')

staged = os.path.join(tmpdir, "accounts.csv")
app_mod.staged_accounts_path = lambda: staged
rows = app_mod._normalize_accounts(src, "upload.csv")
check(
    "quota column matched case-insensitively",
    [r["quota"] for r in rows] == [50000, None, 2000],
    str([r["quota"] for r in rows]),
)
with open(staged, newline="", encoding="utf-8") as fh:
    staged_rows = list(csv.DictReader(fh))
check(
    "staged file keeps the quota values",
    staged_rows[0]["quota"] == "50000" and staged_rows[2]["quota"] == "2000",
    str(staged_rows),
)
loaded = app_mod._load_staged_accounts()
check(
    "staged file round-trips quotas",
    [r["quota"] for r in loaded] == [50000, None, 2000],
    str([r["quota"] for r in loaded]),
)

# duplicate case-collision must fail loudly, never silently drop
src2 = os.path.join(tmpdir, "dup.csv")
with open(src2, "w", newline="", encoding="utf-8") as fh:
    fh.write("account_id,email,password,Quota,quota\n")
    fh.write("a1,a@x.com,pw,1,2\n")
try:
    app_mod._normalize_accounts(src2, "dup.csv")
    check("duplicate quota columns raise", False, "no error raised")
except app_mod.UploadError as exc:
    check("duplicate quota columns raise", True, str(exc))

# a misnamed quota-like column must fail loudly - never drop quotas silently
src3 = os.path.join(tmpdir, "misnamed.csv")
with open(src3, "w", newline="", encoding="utf-8") as fh:
    fh.write("account_id,email,password,Quota Allocation\n")
    fh.write("a1,a@x.com,pw,50k\n")
try:
    app_mod._normalize_accounts(src3, "misnamed.csv")
    check("misnamed quota column raises", False, "no error raised")
except app_mod.UploadError as exc:
    check("misnamed quota column raises", True, str(exc))

# unrelated extra columns are ignored (and reported), not fatal
src4 = os.path.join(tmpdir, "extra.csv")
with open(src4, "w", newline="", encoding="utf-8") as fh:
    fh.write("account_id,email,password,quota,notes\n")
    fh.write("a1,a@x.com,pw,5000,team A\n")
rows = app_mod._normalize_accounts(src4, "extra.csv")
check("extra columns ignored, quota kept", rows[0]["quota"] == 5000, str(rows))

# ---- 2. apply_quotas re-slices pending rows only ----------------------------
db = os.path.join(tmpdir, "job.db")
souls = os.path.join(tmpdir, "souls.csv")
with open(souls, "w", newline="", encoding="utf-8") as fh:
    fh.write("firstName,lastName,phone\n")
    for i in range(12):
        fh.write(f"F{i},L{i},080{i:07d}\n")

store = JobStore(db_path=db)
accts = [
    {"account_id": "A", "email": "a@x.com", "password": "1", "quota": None},
    {"account_id": "B", "email": "b@x.com", "password": "2", "quota": None},
    {"account_id": "C", "email": "c@x.com", "password": "3", "quota": None},
]
store.create_job("add", accts, ["firstName", "lastName", "phone"], souls)
plan = {p["account_id"]: p["assigned"] for p in store.summary()["accounts"]}
check("initial even split 4/4/4", plan == {"A": 4, "B": 4, "C": 4}, str(plan))

for rid in (1, 2, 3):
    store.mark(rid, "success")   # A finishes 3 rows
store.mark(5, "running")         # crash leftover -> must become movable pending

sync = store.apply_quotas(
    [
        {"account_id": "A", "email": "a@x.com", "password": "1", "quota": 6},
        {"account_id": "B", "email": "b@x.com", "password": "2", "quota": 1},
        {"account_id": "C", "email": "c@x.com", "password": "3", "quota": None},
    ]
)
rows_by_acct = {r["account_id"]: r for r in store.summary()["accounts"]}
check(
    "quota applied: A holds 6",
    rows_by_acct["A"]["assigned"] == 6 and rows_by_acct["A"]["quota"] == 6,
    str(rows_by_acct["A"]),
)
check(
    "quota applied: B holds 1",
    rows_by_acct["B"]["assigned"] == 1 and rows_by_acct["B"]["quota"] == 1,
    str(rows_by_acct["B"]),
)
check(
    "blank quota gets the rest: C holds 5",
    rows_by_acct["C"]["assigned"] == 5 and rows_by_acct["C"]["quota"] is None,
    str(rows_by_acct["C"]),
)
check(
    "finished rows kept their credit (A done=3)",
    rows_by_acct["A"]["done"] == 3,
    str(rows_by_acct["A"]),
)
check(
    "every row still assigned",
    sum(r["assigned"] for r in rows_by_acct.values()) == 12,
    str({k: v["assigned"] for k, v in rows_by_acct.items()}),
)

# ---- 3. an account removed from the file hands back its pending rows ---------
sync = store.apply_quotas(
    [
        {"account_id": "A", "email": "a@x.com", "password": "1", "quota": 6},
        {"account_id": "C", "email": "c@x.com", "password": "3", "quota": None},
    ]
)
rows_by_acct = {r["account_id"]: r for r in store.summary()["accounts"]}
check("A unchanged at 6", rows_by_acct["A"]["assigned"] == 6, str(rows_by_acct["A"]))
check(
    "removed B's rows landed on C (6)",
    rows_by_acct["C"]["assigned"] == 6 and rows_by_acct["B"]["assigned"] == 0,
    str(rows_by_acct["C"]),
)
check(
    "no rows lost when an account leaves",
    sum(r["assigned"] for r in rows_by_acct.values()) == 12,
    str({k: v["assigned"] for k, v in rows_by_acct.items()}),
)

print()
print("FAILURES:", fails if fails else "none")
sys.exit(1 if fails else 0)