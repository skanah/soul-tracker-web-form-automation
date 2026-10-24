"""Validation for the track / add_then_track run modes (no browser, no data/ writes).

Run: .venv/Scripts/python.exe test_track_flow.py
"""
import asyncio
import os
import sys
import tempfile
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
os.environ["AUTO_RESUME"] = "0"  # importing app must never start a real worker
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import app as app_mod  # noqa: E402
from automation.job_store import JobStore  # noqa: E402
from automation.track_filler import (  # noqa: E402
    TrackFiller,
    extract_tracking,
    has_tracking_data,
    match_soul,
    normalize_church_service,
    normalize_foundation_class,
    soul_display,
    tracking_columns,
)

fails: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("PASS " if cond else "FAIL ") + name + (f"  [{detail}]" if detail else ""))
    if not cond:
        fails.append(name)


def expect_error(fn, name: str, needle: str = "") -> None:
    try:
        fn()
    except ValueError as exc:
        check(name, needle in str(exc), str(exc))
    else:
        check(name, False, "no error raised")


tmpdir = tempfile.mkdtemp(prefix="soultracker-track-test-")

# ---- 1. track column detection -------------------------------------------
cols = tracking_columns(["firstName", "churchService", "foundationClass", "kingsChatId"])
check(
    "tracking_columns finds the drawer inputs",
    sorted(cols) == sorted(["churchService", "foundationClass", "kingsChatId"]),
    str(cols),
)
cols = tracking_columns(["Church Service", "church_service", "Foundation School", "KingsChat", "phone"])
check("tracking_columns is case/spacing-insensitive", len(cols) == 4, str(cols))
check("tracking_columns ignores other columns", tracking_columns(["firstName", "phone"]) == [])

# ---- 2. run mode resolution ----------------------------------------------
check("resolve_mode add", app_mod.resolve_mode("add") == "add")
check("resolve_mode track", app_mod.resolve_mode("Track Souls") == "track")
check("resolve_mode add_then_track", app_mod.resolve_mode("add_then_track") == "add_then_track")
check("resolve_mode add+track alias", app_mod.resolve_mode("add+track") == "add_then_track")
check("resolve_mode 'add and track' alias", app_mod.resolve_mode("add and track") == "add_then_track")
check("resolve_mode 'both' alias", app_mod.resolve_mode("both") == "add_then_track")
check("resolve_mode rejects unknown", app_mod.resolve_mode("banana") == "")

# ---- 3. drawer value mapping ---------------------------------------------
check("churchService code 3 -> label", normalize_church_service("3") == "Attended 2 church services")
check("churchService 0 -> untouched", normalize_church_service("0") is None)
check("churchService 'not attended'", normalize_church_service("not attended") == "Not attended any church service yet")
check("churchService 'attended 3 church services'", normalize_church_service("attended 3 church services") == "Attended 3 church services")
check("churchService blank -> untouched", normalize_church_service("") is None)
expect_error(lambda: normalize_church_service("9"), "churchService out of range raises", "out of range")
expect_error(lambda: normalize_church_service("soon"), "churchService garbage raises", "cannot interpret")

check("foundationClass code 8 -> Completed", normalize_foundation_class("8") == "Completed")
check("foundationClass 'graduated'", normalize_foundation_class("graduated") == "Completed")
check("foundationClass 'Class 3'", normalize_foundation_class("Class 3") == "Class 3")
check("foundationClass 'class 7'", normalize_foundation_class("class 7") == "Class 7")
check("foundationClass 'not yet started' -> untouched", normalize_foundation_class("not yet started") is None)
expect_error(lambda: normalize_foundation_class("9"), "foundationClass out of range raises", "out of range")

values = extract_tracking(
    {
        "churchService": "2",
        "foundationClass": "Class 3",
        "kingsChatId": "john",
        "phone": "08012345678",
        "email": "a@x.com",
    }
)
check(
    "extract_tracking maps every drawer field",
    values
    == {
        "church_service": "Attended 1 church service",
        "foundation_class": "Class 3",
        "kingschat_id": "@john",
        "phone": "08012345678",
        "email": "a@x.com",
    },
    str(values),
)
values = extract_tracking({"church": "5", "phone": "abc", "email": "bad"})
check(
    "aliases work; invalid phone/email dropped",
    values["church_service"] == "Attended 4 church services"
    and values["phone"] is None
    and values["email"] is None,
    str(values),
)

# ---- 4. card matching ----------------------------------------------------
check("match_soul by name", match_soul({"firstName": "Olu", "lastName": "Sheun"}, "Olu Sheun", "x") == "name")
check(
    "match_soul by phone tail",
    match_soul({"firstName": "X", "phone": "08012345678"}, "Zed", "call 080 1234 5678") == "phone",
)
check("match_soul by email", match_soul({"email": "A@x.com"}, "Name", "mail A@x.com") == "email")
check("match_soul no match", match_soul({"firstName": "Q", "lastName": "W"}, "Other", "nothing") is None)
check("has_tracking_data true", has_tracking_data({"kingsChatId": "x"}) is True)
check("has_tracking_data false", has_tracking_data({"firstName": "x"}) is False)
check("soul_display name", soul_display({"firstName": "A", "lastName": "B"}) == "A B")
check("soul_display fallback", soul_display({}) == "unknown soul")

# ---- 5. real TrackFiller row decisions (browser must stay untouched) ------
class ExplodingPage:
    url = "https://site/dashboard/track-souls"

    def __getattr__(self, name):
        raise AssertionError(f"the browser must not be touched (asked for {name})")


class LoginPage(ExplodingPage):
    url = "https://site/login?next=/"


class FakeSession:
    account_id = "acc"

    def __init__(self, page=None):
        self.page = page or ExplodingPage()
        self.last_refresh_at = time.time()
        self.login_calls = 0
        self.login_result = True

    async def refresh_session(self, log=None):
        pass

    async def login(self, pool, log=None):
        self.login_calls += 1
        return self.login_result


quiet = TrackFiller(FakeSession(), log=lambda *a, **k: None)
res = asyncio.run(quiet.track({"firstName": "A"}))
check("track() skips rows with no tracking values", res["status"] == "skipped", res["error"])
res = asyncio.run(quiet.track({"firstName": "A", "churchService": "9"}))
check(
    "track() fails loudly on unmappable values",
    res["status"] == "failed" and "out of range" in (res["error"] or ""),
    res["error"],
)
res = asyncio.run(
    TrackFiller(FakeSession(LoginPage()), log=lambda *a, **k: None).track(
        {"firstName": "A", "churchService": "2"}
    )
)
check(
    "track() reports dead sessions in the worker's retry wording",
    res["status"] == "failed" and "session expired" in (res["error"] or "").lower(),
    res["error"],
)

# ---- 6. _run_stage state machine (fakes for the browser classes) ----------
app_mod.Config.PACE_BETWEEN_SOULS = 0
app_mod.Config.LOGIN_STAGGER_SECONDS = 0
app_mod.push_log = lambda *a, **k: None
app_mod.set_progress = lambda *a, **k: None
app_mod.queue_result = lambda *a, **k: None
app_mod.browser_pool = object()

_seq = {"n": 0}
HEADER = ["firstName", "lastName", "phone", "churchService", "foundationClass", "kingsChatId"]


def scratch_store(payloads, mode):
    _seq["n"] += 1
    n = _seq["n"]
    souls_csv = os.path.join(tmpdir, f"souls{n}.csv")
    with open(souls_csv, "w", newline="", encoding="utf-8") as fh:
        fh.write(",".join(HEADER) + "\n")
        for p in payloads:
            fh.write(",".join(str(p.get(h, "")) for h in HEADER) + "\n")
    store = JobStore(db_path=os.path.join(tmpdir, f"job{n}.db"))
    store.create_job(
        mode,
        [{"account_id": "acc1", "email": "a@x.com", "password": "1", "quota": None}],
        HEADER,
        souls_csv,
    )
    return store


def status_counts(store):
    acct = store.summary()["accounts"][0]
    out = {}
    for src, dst in (("ok", "success"), ("fail", "failed"), ("skip", "skipped"),
                     ("added", "added"), ("pending", "pending"), ("running", "running")):
        if acct[src]:
            out[dst] = int(acct[src])
    return out


class FakeForm:
    def __init__(self, *a, **k):
        pass

    async def fill(self, payload):
        if payload.get("phone") == "080FAIL":
            return {"status": "failed", "error": "boom", "soul": payload.get("firstName")}
        return {"status": "success", "soul": payload.get("firstName")}


class FakeTrack:
    opens = 0

    def __init__(self, *a, **k):
        pass

    async def open(self):
        FakeTrack.opens += 1

    async def track(self, payload):
        return {"status": "success", "soul": payload.get("firstName"), "applied": {}}


class FakeTrackSkip:  # mirrors the real contract: no tracking values -> skipped
    def __init__(self, *a, **k):
        pass

    async def open(self):
        pass

    async def track(self, payload):
        if not (payload.get("churchService") or payload.get("foundationClass") or payload.get("kingsChatId")):
            return {"status": "skipped", "error": "no tracking values for this row"}
        return {"status": "success", "soul": payload.get("firstName"), "applied": {}}


app_mod.FormFiller = FakeForm
app_mod.TrackFiller = FakeTrack

# ---- 6a. add_then_track: pending -> added -> success ----------------------
store = scratch_store(
    [
        {"firstName": "A", "lastName": "One", "phone": "08012345678", "churchService": "2"},
        {"firstName": "B", "lastName": "Two", "phone": "08012345679", "foundationClass": "Class 3"},
        {"firstName": "C", "lastName": "Three", "phone": "080FAIL", "churchService": "2"},
    ],
    "add_then_track",
)
sess = FakeSession()
done, failed = asyncio.run(app_mod._run_stage(store, "add_then_track", "add", "acc1", sess, 3))
check("add stage: 2 ok, 1 failed", (done, failed) == (2, 1), f"{done},{failed}")
check(
    "add_then_track success rows wait as 'added'",
    status_counts(store) == {"added": 2, "failed": 1},
    str(status_counts(store)),
)
check("unfinished counts 'added' rows", app_mod._unfinished(store.summary()) == 2)
check(
    "regression: account with only 'added' rows still has work",
    app_mod._account_has_work(store, "acc1", "add_then_track") is True,
)

done, failed = asyncio.run(app_mod._run_stage(store, "add_then_track", "track", "acc1", sess, 3))
check("track stage claims only the 'added' rows", (done, failed) == (2, 0), f"{done},{failed}")
check(
    "add_then_track finishes rows as 'success'",
    status_counts(store) == {"success": 2, "failed": 1},
    str(status_counts(store)),
)
check("track stage opened the Track page once", FakeTrack.opens == 1, str(FakeTrack.opens))
check(
    "regression: all-terminal account has no work",
    app_mod._account_has_work(store, "acc1", "add_then_track") is False,
)

# ---- 6b. pure track mode: pending -> success / skipped --------------------
app_mod.TrackFiller = FakeTrackSkip
store = scratch_store(
    [
        {"firstName": "D", "lastName": "Four", "phone": "08012345680", "churchService": "2"},
        {"firstName": "E", "lastName": "Five", "phone": "08012345681"},
    ],
    "track",
)
done, failed = asyncio.run(app_mod._run_stage(store, "track", "track", "acc1", sess, 2))
check("track mode: 1 ok, 0 failed", (done, failed) == (1, 0), f"{done},{failed}")
check(
    "track mode: success + skipped rows",
    status_counts(store) == {"success": 1, "skipped": 1},
    str(status_counts(store)),
)

# ---- 6c. dead session mid-stage -> re-login and retry the SAME row --------
class ExpiringForm:
    calls = {}

    def __init__(self, *a, **k):
        pass

    async def fill(self, payload):
        n = ExpiringForm.calls.get(payload.get("firstName"), 0)
        ExpiringForm.calls[payload["firstName"]] = n + 1
        if n == 0:
            return {"status": "failed", "error": "redirected to the login page - the session expired"}
        return {"status": "success", "soul": payload.get("firstName")}


class AlwaysExpiredForm:
    def __init__(self, *a, **k):
        pass

    async def fill(self, payload):
        return {"status": "failed", "error": "redirected to the login page - the session expired"}


app_mod.FormFiller = ExpiringForm
store = scratch_store([{"firstName": "F", "lastName": "Six", "phone": "08012345682"}], "add")
sess = FakeSession()
done, failed = asyncio.run(app_mod._run_stage(store, "add", "add", "acc1", sess, 1))
check(
    "session death -> re-login -> row retried and saved",
    (done, failed) == (1, 0) and sess.login_calls == 1 and status_counts(store) == {"success": 1},
    f"{done},{failed} logins={sess.login_calls} {status_counts(store)}",
)

app_mod.FormFiller = AlwaysExpiredForm
store = scratch_store([{"firstName": "G", "lastName": "Seven", "phone": "08012345683"}], "add")
sess = FakeSession()
sess.login_result = False
done, failed = asyncio.run(app_mod._run_stage(store, "add", "add", "acc1", sess, 1))
check(
    "unrecoverable session -> row back to pending, nothing burned",
    (done, failed) == (0, 0) and status_counts(store) == {"pending": 1},
    f"{done},{failed} {status_counts(store)}",
)

# ---- 6d. form that never renders pauses the account ----------------------
class StuckForm:
    def __init__(self, *a, **k):
        pass

    async def fill(self, payload):
        return {"status": "failed", "error": "the Add Soul form did not render (page is at /x)"}


app_mod.FormFiller = StuckForm
store = scratch_store(
    [{"firstName": f"H{i}", "lastName": "X", "phone": f"0801234568{i}"} for i in range(3)],
    "add",
)
sess = FakeSession()
done, failed = asyncio.run(app_mod._run_stage(store, "add", "add", "acc1", sess, 3))
counts = status_counts(store)
check(
    "render-failure never burns rows (account pauses, rows stay pending)",
    (done, failed) == (0, 0) and counts == {"pending": 3},
    f"{done},{failed} {counts}",
)
check(
    "account is flagged 'blocked'",
    store.summary()["accounts"][0]["state"] == "blocked",
    store.summary()["accounts"][0]["state"],
)

# ---- 7. POST /run validation for track modes ------------------------------
http_db = os.path.join(tmpdir, "http-job.db")
http_souls = os.path.join(tmpdir, "http-souls.csv")
http_accounts = os.path.join(tmpdir, "http-accounts.csv")
with open(http_souls, "w", newline="", encoding="utf-8") as fh:  # NO tracking columns
    fh.write("firstName,lastName,phone\nSolo,Row,08012345690\n")
with open(http_accounts, "w", newline="", encoding="utf-8") as fh:
    fh.write("account_id,email,password,quota\nacc1,a@x.com,1,\n")

app_mod.JobStore = lambda: JobStore(db_path=http_db)
app_mod.staged_souls_path = lambda: http_souls
app_mod.staged_accounts_path = lambda: http_accounts

client = app_mod.app.test_client()
with client.session_transaction() as sess_flask:
    sess_flask["admin_authed"] = True

resp = client.post("/run", json={"mode": "track"})
check(
    "track run without tracking columns -> 400",
    resp.status_code == 400 and "tracking" in (resp.get_json().get("error") or "").lower(),
    f"{resp.status_code} {resp.get_json()}",
)
check("rejected request creates no job", JobStore(db_path=http_db).get_job() is None)

resp = client.post("/run", json={"mode": "banana"})
check("unknown run mode -> 400", resp.status_code == 400, str(resp.status_code))

print()
print("FAILURES:", fails if fails else "none")
sys.exit(1 if fails else 0)
