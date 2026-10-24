"""SoulTracker multi-account automation dashboard.

Run modes (POST /run):
    add             - fill the New Soul form for every assigned row
    track           - track every assigned row on the Track Souls page
    add_then_track  - add every row first (status 'added'), then track it

Rows live in a SQLite job store (automation/job_store.py), so million-row
runs are resumable and never pass through the browser.
"""
import asyncio
import csv
import hmac
import io
import json
import os
import shutil
import threading
import time
import urllib.request
from collections import deque
from datetime import timedelta

import pandas as pd
from flask import (
    Flask,
    Response,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from flask_socketio import SocketIO
from werkzeug.utils import secure_filename

from config import Config
from automation import (
    AccountSession,
    FormFiller,
    JobError,
    JobStore,
    TrackFiller,
    browser_pool,
    parse_quota,
    read_header,
    soul_display,
    tracking_columns,
)

app = Flask(__name__)
app.config.from_object(Config)
# async_mode is env-driven so the same code runs under the local eventlet
# server and under a plain WSGI host (cPanel/Passenger -> "threading").
socketio = SocketIO(
    app, cors_allowed_origins="*", async_mode=os.getenv("SOCKETIO_ASYNC_MODE", "eventlet")
)

os.makedirs(Config.UPLOAD_FOLDER, exist_ok=True)

# ---------------- admin auth ----------------
# The dashboard exposes data/accounts.csv (real account passwords), uploads
# and run controls - everything behind a single admin password. The session
# cookie is signed with SECRET_KEY and lasts 12 hours.
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(hours=12)

# Brute-force throttle: per-IP failed-attempt counter with a lockout window.
_LOGIN_MAX_FAILS = 5
_LOGIN_LOCKOUT_SECONDS = 300
_login_fails: dict[str, tuple[int, float]] = {}  # ip -> (fails, locked_until)
_login_lock = threading.Lock()


def _is_admin() -> bool:
    return bool(session.get("admin_authed"))


def _client_ip() -> str:
    forwarded = request.headers.get("X-Forwarded-For", "")
    return (forwarded.split(",")[0].strip() if forwarded else request.remote_addr) or "?"


@app.before_request
def _require_admin():
    """Gate every route behind the admin login.

    GET/HEAD requests are redirected to /login; API-style requests (POST and
    anything a browser would not navigate to) get a 401 JSON so fetch() can
    handle it. Static files and the auth endpoints stay public.
    """
    if request.endpoint in ("static", "login", "logout"):
        return None
    if _is_admin():
        return None
    if request.method in ("GET", "HEAD"):
        return redirect(url_for("login", next=request.full_path.rstrip("?")))
    return jsonify({"error": "not logged in"}), 401


@socketio.on("connect")
def _reject_unauthenticated_socket():
    """Live logs/progress are data too - reject sockets without a login."""
    if not _is_admin():
        return False  # returning False makes flask-socketio refuse the connect


@app.route("/login", methods=["GET", "POST"])
def login():
    error = ""
    nxt = request.values.get("next", "")
    if request.method == "POST":
        ip = _client_ip()
        now = time.time()
        with _login_lock:
            fails, locked_until = _login_fails.get(ip, (0, 0.0))
            if now < locked_until:
                error = "Too many failed attempts - try again in a few minutes."
            elif hmac.compare_digest(
                request.form.get("password", "").encode("utf-8"),
                Config.ADMIN_PASSWORD.encode("utf-8"),
            ):
                _login_fails.pop(ip, None)
                session.clear()
                session["admin_authed"] = True
                session.permanent = True
                # Only allow same-site relative redirects (no open redirect).
                if not nxt.startswith("/") or nxt.startswith("//"):
                    nxt = "/"
                return redirect(nxt)
            else:
                fails += 1
                locked_until = (
                    now + _LOGIN_LOCKOUT_SECONDS if fails >= _LOGIN_MAX_FAILS else 0.0
                )
                _login_fails[ip] = (fails, locked_until)
                error = (
                    "Wrong password."
                    if fails < _LOGIN_MAX_FAILS
                    else "Too many failed attempts - try again in a few minutes."
                )
    return render_template("login.html", error=error, next=nxt)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ---------------- ROUTES ----------------
@app.route("/")
def index():
    return render_template("index.html")
ADD_MODES = {"add", "add_then_track"}
TRACK_MODES = {"track", "add_then_track"}
VALID_MODES = ADD_MODES | TRACK_MODES
MODE_ALIASES = {
    "add_souls": "add",
    "track_souls": "track",
    "add_track": "add_then_track",
    "add_and_track": "add_then_track",
    "add_plus_track": "add_then_track",
    "both": "add_then_track",
}


def resolve_mode(raw) -> str:
    """Return a valid run mode, or "" when the requested mode is unknown."""
    text = str("add" if raw is None else raw).strip().lower()
    text = text.replace("+", " plus ").replace("&", " plus ")
    mode = "_".join(text.replace("-", " ").split())
    mode = MODE_ALIASES.get(mode, mode)
    return mode if mode in VALID_MODES else ""


# ---------------- staging helpers ----------------
class UploadError(ValueError):
    """User-facing problem with an uploaded file."""


def staged_souls_path() -> str:
    return os.path.join(Config.UPLOAD_FOLDER, "souls.csv")


def staged_accounts_path() -> str:
    return os.path.join(Config.UPLOAD_FOLDER, "accounts.csv")


def _check_soul_columns(header) -> None:
    columns = set(header)
    missing = {"firstName", "lastName"} - columns
    if missing:
        raise UploadError(
            f"File must have columns: {sorted(missing)} "
            "(firstName and lastName are required)"
        )
    if not ({"phone", "email"} & columns):
        raise UploadError(
            "Either phone or email is required (add a phone and/or email column)."
        )


def _normalize_souls(tmp_path: str, ext: str):
    """Validate the upload and stage it as UTF-8 CSV.

    CSV files are streamed (header check + count) and then copied byte for
    byte, so a million rows never sit in RAM and leading zeros survive.
    xlsx is parsed once and re-serialised to CSV.
    Returns (header, row_count, sample_rows).
    """
    if ext == ".csv":
        with open(tmp_path, newline="", encoding="utf-8-sig") as fh:
            reader = csv.reader(fh)
            header = [col.strip() for col in (next(reader, None) or [])]
            if not header:
                raise UploadError("the souls file is empty")
            _check_soul_columns(header)
            count = 0
            sample = []
            for row in reader:
                if not any(cell.strip() for cell in row):
                    continue
                count += 1
                if len(sample) < 5:
                    sample.append(dict(zip(header, row)))
        if count == 0:
            raise UploadError("the souls file has no data rows")
        shutil.copyfile(tmp_path, staged_souls_path())
        return header, count, sample

    try:
        df = pd.read_excel(tmp_path, dtype=str)
    except Exception as exc:
        raise UploadError(f"could not read the spreadsheet: {exc}") from exc
    header = [str(col).strip() for col in df.columns]
    if not header:
        raise UploadError("the souls file is empty")
    _check_soul_columns(header)
    if len(df) == 0:
        raise UploadError("the souls file has no data rows")
    df.to_csv(staged_souls_path(), index=False, encoding="utf-8", lineterminator="\n")

    sample = df.head(5).fillna("").to_dict(orient="records")
    return header, len(df), sample


_ACCOUNT_COLUMNS = ("account_id", "email", "password", "quota")


def _lower_account_columns(df: pd.DataFrame) -> None:
    """Match the account columns case-insensitively ("Quota" == "quota").

    An Excel header case difference must never silently drop the quota column.
    """
    rename = {}
    for col in df.columns:
        key = col.lower()
        if key in _ACCOUNT_COLUMNS and key != col:
            if key in df.columns or key in rename.values():
                raise UploadError(f"duplicate column {col!r} (case-insensitive)")
            rename[col] = key
    if rename:
        df.rename(columns=rename, inplace=True)


def _normalize_accounts(tmp_path: str, filename: str) -> list[dict]:
    """Validate the accounts upload and stage it as ``accounts.csv``.

    Accepts .csv / .xlsx / .xls. An optional ``quota`` column (50000, 50,000
    or 50k) gives that account an explicit share of the souls file; blank means
    "even share of the remainder".
    """
    ext = os.path.splitext(filename)[1].lower()
    try:
        if ext in (".xlsx", ".xls"):
            df = pd.read_excel(tmp_path, dtype=str)
        else:
            df = pd.read_csv(tmp_path, dtype=str)
    except Exception as exc:
        raise UploadError(f"could not read {filename}: {exc}") from exc
    df.columns = [str(col).strip() for col in df.columns]
    _lower_account_columns(df)
    missing = {"account_id", "email", "password"} - set(df.columns)
    if missing:
        raise UploadError(f"CSV must have columns: {sorted(missing)}")
    # Unrecognized columns are ignored - but say so, and never let a quota-like
    # header disappear silently (that is how quotas got lost once).
    ignored = [col for col in df.columns if col not in _ACCOUNT_COLUMNS]
    for col in ignored:
        if "quota" in col.lower():
            raise UploadError(
                f"column {col!r} looks like the quota column but is not named"
                " exactly 'quota' - rename the header to 'quota' (any case)"
                " and re-upload"
            )
    if ignored:
        push_log(
            "[system] ⚠ Ignoring unrecognized accounts columns: " + ", ".join(ignored)
        )

    rows, seen = [], set()
    for raw in df.fillna("").to_dict(orient="records"):
        account_id = str(raw.get("account_id") or "").strip()
        if not account_id:
            continue
        if account_id in seen:
            raise UploadError(f"duplicate account_id {account_id!r}")
        seen.add(account_id)
        rows.append(
            {
                "account_id": account_id,
                "email": str(raw.get("email") or "").strip(),
                "password": str(raw.get("password") or ""),
                "quota": parse_quota(raw.get("quota")),
            }
        )
    if not rows:
        raise UploadError("the accounts file has no rows")

    with open(staged_accounts_path(), "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(
            fh, fieldnames=["account_id", "email", "password", "quota"]
        )
        writer.writeheader()
        for row in rows:
            writer.writerow({**row, "quota": row["quota"] or ""})
    return rows


def _load_staged_accounts() -> list[dict]:
    """Read back the staged accounts.csv written by ``_normalize_accounts``."""
    path = staged_accounts_path()
    if not os.path.exists(path):
        raise UploadError("upload the accounts file first")
    accounts, seen = [], set()
    with open(path, newline="", encoding="utf-8-sig") as fh:
        for raw in csv.DictReader(fh):
            # case-insensitive keys ("Quota" == "quota") like the upload path
            row = {(k or "").strip().lower(): (v or "") for k, v in raw.items()}
            account_id = (row.get("account_id") or "").strip()
            if not account_id:
                continue
            if account_id in seen:
                raise UploadError(f"duplicate account_id {account_id!r}")
            seen.add(account_id)
            accounts.append(
                {
                    "account_id": account_id,
                    "email": (row.get("email") or "").strip(),
                    "password": row.get("password") or "",
                    "quota": parse_quota(row.get("quota")),
                }
            )
    if not accounts:
        raise UploadError("the accounts file has no rows")
    return accounts


def _sync_staged_quotas(store: "JobStore") -> list[str]:
    """Honour quotas from a re-uploaded accounts file (no-op when unchanged).

    Re-slices the still-pending rows when the staged accounts file's quotas or
    account set differ from the job's stored plan. Finished rows always keep
    their credit. Returns user-facing warnings (already logged).
    """
    try:
        staged = _load_staged_accounts()
    except UploadError:
        return []  # no staged file - nothing to compare
    current = {(a["account_id"], a["quota"]) for a in store.accounts()}
    wanted = {(a["account_id"], a["quota"]) for a in staged}
    if current == wanted:
        return []
    sync = store.apply_quotas(staged)
    push_log(
        f"[system] Quota plan updated - {sync['moved']:,} pending rows "
        "re-assigned to the uploaded quotas (finished rows kept)."
    )
    for warning in sync["warnings"]:
        push_log(f"[system] ⚠ {warning}")
    return sync["warnings"]


# -- end C1 --


# ---------------------------------------------------------------------------
# Thread-safe log / progress pipeline.
#
# The worker runs in its own OS thread; Socket.IO (eventlet) lives in the main
# thread. The worker therefore never emits directly - it queues messages and
# snapshots here, and an eventlet background task (which owns the sockets)
# flushes them out, throttled. This also keeps a 1M-row run from flooding the
# browser with one event per soul.
# ---------------------------------------------------------------------------
_state_lock = threading.Lock()
_queued_logs: list = []
_queued_results: list = []
_progress_snapshot: dict | None = None
_progress_dirty = False
_done_payload: dict | None = None
_dispatcher_started = False
worker_live = False
_last_emit = 0.0
logs_ring = deque(maxlen=500)
_audit_handle = None
_audit_gave_up = False


def push_log(msg: str) -> None:
    """Queue a UI log line and append it to the on-disk audit log."""
    global _audit_handle, _audit_gave_up
    with _state_lock:
        logs_ring.append(msg)
        _queued_logs.append(msg)
    if not _audit_gave_up and _audit_handle is None:
        try:
            os.makedirs(Config.UPLOAD_FOLDER, exist_ok=True)
            stamp = time.strftime("%Y%m%d-%H%M%S")
            _audit_handle = open(
                os.path.join(Config.UPLOAD_FOLDER, f"run-{stamp}.log"),
                "a",
                encoding="utf-8",
            )
        except OSError:
            _audit_gave_up = True
    if _audit_handle:
        try:
            _audit_handle.write(msg + "\n")
            _audit_handle.flush()
        except OSError:
            pass


def queue_result(payload: dict) -> None:
    """Queue a per-row result for the UI (failures/skips only)."""
    with _state_lock:
        _queued_results.append(payload)


def _build_snapshot() -> dict | None:
    summary = JobStore().summary()
    if summary is None:
        return None
    total = summary.get("total") or 0
    processed = summary.get("processed") or 0
    eta = None
    started = summary.get("started_at")
    if worker_live and started and processed > 0:
        elapsed = max(time.time() - started, 1.0)
        rate = processed / elapsed
        if rate > 0:
            eta = int((total - processed) / rate)
    return {
        "mode": summary.get("mode"),
        "state": summary.get("state"),
        "live": worker_live,
        "total": total,
        "processed": processed,
        "ok": summary.get("ok", 0),
        "fail": summary.get("fail", 0),
        "skip": summary.get("skip", 0),
        "added": summary.get("added", 0),
        "running": summary.get("running", 0),
        "pending": summary.get("pending", 0),
        "pct": round(processed * 100 / total, 2) if total else 0.0,
        "etaSeconds": eta,
        "startedAt": summary.get("started_at"),
        "finishedAt": summary.get("finished_at"),
        "warnings": summary.get("warnings", []),
        "accounts": summary.get("accounts", []),
    }


def set_progress(force: bool = False) -> None:
    """Refresh the UI snapshot, throttled to Config.PROGRESS_EMIT_MS."""
    global _progress_snapshot, _progress_dirty, _last_emit
    now = time.monotonic()
    with _state_lock:
        if not force and (now - _last_emit) * 1000 < Config.PROGRESS_EMIT_MS:
            return
        _last_emit = now
    snapshot = _build_snapshot()
    with _state_lock:
        _progress_snapshot = snapshot
        _progress_dirty = True


def set_done(payload: dict | None) -> None:
    global _done_payload
    with _state_lock:
        _done_payload = payload


def ensure_dispatcher() -> None:
    """Start the eventlet task that flushes queues to Socket.IO (once)."""
    global _dispatcher_started
    with _state_lock:
        if _dispatcher_started:
            return
        _dispatcher_started = True
    socketio.start_background_task(_dispatcher)


def _dispatcher() -> None:
    global _progress_dirty, _done_payload
    while True:
        socketio.sleep(0.5)
        with _state_lock:
            logs = _queued_logs[:]
            _queued_logs.clear()
            results = _queued_results[:]
            _queued_results.clear()
            dirty = _progress_dirty
            snapshot = _progress_snapshot if dirty else None
            _progress_dirty = False
            done = _done_payload
            _done_payload = None
        for msg in logs:
            socketio.emit("log", {"message": msg})
        for result in results:
            socketio.emit("result", result)
        if dirty:
            # None here means "no job" (reset) - the UI hides the card.
            socketio.emit("progress", snapshot)
        if done is not None:
            socketio.emit("done", done)


def _account_has_work(store, account_id: str, mode: str) -> bool:
    """True when any stage of ``mode`` still has rows this account can claim.

    In ``add_then_track`` a resume can find an account whose rows are all
    ``added`` (add stage finished, track stage never ran) - it must still log
    in and track them instead of being marked done.
    """
    track_statuses = ["added"] if mode == "add_then_track" else ["pending"]
    first_statuses = ["pending"] if mode in ADD_MODES else track_statuses
    return store.has_rows(account_id, first_statuses) or (
        mode in TRACK_MODES and store.has_rows(account_id, track_statuses)
    )


def _unfinished(summary: dict) -> int:
    """Rows that still have work left in the current mode."""
    return (
        (summary.get("pending") or 0)
        + (summary.get("running") or 0)
        + (summary.get("added") or 0)
    )
# ---------------- ROUTES ----------------
@app.route("/upload/accounts", methods=["POST"])
def upload_accounts():
    f = request.files.get("file")
    if not f:
        return jsonify({"error": "No file"}), 400
    filename = secure_filename(f.filename or "accounts.csv")
    ext = os.path.splitext(filename)[1].lower() or ".csv"
    tmp_path = os.path.join(Config.UPLOAD_FOLDER, "_upload_accounts" + ext)
    f.save(tmp_path)
    try:
        accounts = _normalize_accounts(tmp_path, filename)
    except (UploadError, JobError) as exc:
        return jsonify({"error": str(exc)}), 400
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
    quotas = [row["quota"] for row in accounts if row["quota"]]
    if quotas:
        push_log(
            f"[system] Accounts loaded: {len(accounts)} "
            f"({len(quotas)} with explicit quotas totalling {sum(quotas):,})"
        )
    return jsonify({"ok": True, "count": len(accounts), "accounts": accounts})


@app.route("/upload/souls", methods=["POST"])
def upload_souls():
    f = request.files.get("file")
    if not f:
        return jsonify({"error": "No file"}), 400
    filename = secure_filename(f.filename or "souls.csv")
    ext = ".xlsx" if filename.lower().endswith((".xlsx", ".xls")) else ".csv"
    tmp_path = os.path.join(Config.UPLOAD_FOLDER, "_upload_souls" + ext)
    f.save(tmp_path)
    try:
        header, count, sample = _normalize_souls(tmp_path, ext)
    except (UploadError, JobError) as exc:
        return jsonify({"error": str(exc)}), 400
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
    track = tracking_columns(header)
    if track:
        push_log(f"[system] Track columns detected: {', '.join(track)}")
    else:
        push_log(
            "[system] ⚠ No tracking columns found (churchService, foundationClass, "
            "kingsChatId). Only track / add_then_track runs need them - add runs "
            "are unaffected."
        )
    push_log(f"[system] Souls file staged: {count:,} rows")
    return jsonify(
        {
            "ok": True,
            "count": count,
            "columns": header,
            "trackingColumns": track,
            "sample": sample,
        }
    )


@app.route("/run", methods=["POST"])
def run_automation():
    global worker_live
    payload = request.json or {}
    mode = resolve_mode(payload.get("mode"))
    if not mode:
        return jsonify(
            {"error": f"Unknown mode {payload.get('mode')!r}. Use one of: {sorted(VALID_MODES)}"}
        ), 400

    store = JobStore()

    with _state_lock:
        if worker_live:
            return jsonify(
                {"error": "A run is already in progress - watch the Progress card."}
            ), 409

    job = store.get_job()
    quota_warnings: list[str] = []
    resumed = False
    mode_switched = False
    if job is not None:
        summary = store.summary() or {}
        if job["mode"] != mode:
            if _unfinished(summary) > 0:
                return jsonify(
                    {
                        "error": f"Current job runs in {job['mode']!r} mode and has "
                        f"{_unfinished(summary)} unfinished rows - reset it before "
                        f"switching to {mode!r}."
                    }
                ), 409
            # Finished jobs may switch modes, but reset only after the new
            # mode's staged files pass validation below.
            job = None
            mode_switched = True
            resumed = False
            summary = {}
    if job is not None:
        if _unfinished(store.summary() or {}) == 0:
            return jsonify(
                {
                    "error": "The current job is finished - use "
                    "'Start new run' before running again."
                }
            ), 409
        resumed = True
        # Accounts re-uploaded mid-job may carry new quotas - re-slice the
        # still-pending rows so the quota is honoured (finished rows keep
        # their credit). An unchanged file is a no-op.
        try:
            quota_warnings.extend(_sync_staged_quotas(store))
        except JobError as exc:
            return jsonify({"error": str(exc)}), 400
    else:
        missing = [
            label
            for label, path in (
                ("souls file", staged_souls_path()),
                ("accounts file", staged_accounts_path()),
            )
            if not os.path.exists(path)
        ]
        if missing:
            return jsonify(
                {"error": "Upload the " + " and ".join(missing) + " first."}
            ), 400
        try:
            header = read_header(staged_souls_path())
            _check_soul_columns(header)
            accounts = _load_staged_accounts()
            if mode in TRACK_MODES and not tracking_columns(header):
                return jsonify(
                    {
                        "error": "No tracking columns in the souls file "
                        "(churchService, foundationClass and/or kingsChatId). "
                        "Add them or run in add mode."
                    }
                ), 400
            if mode_switched:
                store.reset()
                push_log(
                    f"[system] Finished job reset automatically to start {mode!r}."
                )
            store.create_job(mode, accounts, header, staged_souls_path())
        except (UploadError, JobError) as exc:
            return jsonify({"error": str(exc)}), 400

    # Validation passed - claim the right to start atomically so two rapid
    # clicks can never launch two workers. Every 4xx above leaves this free.
    with _state_lock:
        if worker_live:
            return jsonify(
                {"error": "A run is already in progress - watch the Progress card."}
            ), 409
        worker_live = True

    ensure_dispatcher()
    summary = store.summary() or {}
    accounts_plan = [
        {"account_id": row["account_id"], "assigned": row["assigned"]}
        for row in summary.get("accounts", [])
    ]
    push_log(
        f"[system] {'Resumed' if resumed else 'Started'} "
        f"{summary.get('total', 0):,} souls [{mode}] across "
        f"{len(accounts_plan)} accounts"
    )
    set_progress(force=True)
    thread = threading.Thread(
        target=start_async_run, args=(mode,), daemon=True, name="soul-worker"
    )
    thread.start()
    return jsonify(
        {
            "ok": True,
            "mode": mode,
            "resumed": resumed,
            "total": summary.get("total", 0),
            "plan": accounts_plan,
            "warnings": summary.get("warnings", []) + quota_warnings,
            "message": (
                ("Resumed" if resumed else "Started")
                + f" {summary.get('total', 0):,} souls [{mode}]"
            ),
        }
    )
# -- end C3 --


@app.route("/status")
def status():
    """Everything the UI needs to (re)render the Progress card after a reload."""
    with _state_lock:
        logs = list(logs_ring)
    return jsonify({"job": _build_snapshot(), "logs": logs})


@app.route("/failures.csv")
def failures_csv():
    store = JobStore()
    job = store.get_job()
    if job is None:
        return jsonify({"error": "no job has been created yet"}), 404
    header = job.get("header") or []

    def generate():
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(header)
        yield buf.getvalue()
        buf.seek(0)
        buf.truncate(0)
        for payload in store.iter_failures():
            writer.writerow([payload.get(col, "") for col in header])
            yield buf.getvalue()
            buf.seek(0)
            buf.truncate(0)

    return Response(
        generate(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=failed-rows.csv"},
    )


@app.route("/job/reset", methods=["POST"])
def job_reset():
    with _state_lock:
        if worker_live:
            return jsonify({"error": "Cannot reset while a run is in progress."}), 409
    JobStore().reset()
    push_log("[system] Job reset - ready for a new run.")
    set_progress(force=True)  # snapshot becomes None -> UI hides the card
    return jsonify({"ok": True})


# -- end C4 --


# ---------------- worker ----------------
async def _run_stage(store, mode, kind, account_id, sess, assigned, login_sem=None):
    """Run one stage (``add``/``track``) for one account's rows.

    Returns ``(done_count, failed_count)``. Raises if the Track page cannot be
    opened - nothing has been claimed yet then, so a later Resume retries.
    """
    statuses = (
        ["added"] if (kind == "track" and mode == "add_then_track") else ["pending"]
    )
    filler = tracker = None
    if kind == "add":
        filler = FormFiller(sess, log=push_log)
    else:
        tracker = TrackFiller(sess, log=push_log)
        await tracker.open()

    done_count = failed_count = 0
    render_failures = 0  # consecutive "form did not render" for this account
    while True:
        batch = store.next_batch(account_id, statuses)
        if not batch:
            break
        for row in batch:
            payload = json.loads(row["payload"])
            session_retries = 0
            while True:
                # Proactive keep-alive: renew tokens before the site's ~30-min
                # session death window (refresh endpoint is not rate-limited
                # like /login, so this keeps IP login volume near zero).
                if time.time() - sess.last_refresh_at >= (
                    Config.SESSION_REFRESH_MINUTES * 60
                ):
                    await sess.refresh_session(log=push_log)
                if kind == "add":
                    result = await filler.fill(payload)
                    status = (
                        "added"
                        if mode == "add_then_track"
                        and result.get("status") == "success"
                        else (result.get("status") or "failed")
                    )
                else:
                    result = await tracker.track(payload)
                    status = result.get("status") or "failed"

                # Site sessions die after ~30 min: re-login (through the
                # login throttle) and retry the same row instead of failing it.
                error = result.get("error") or ""

                # An account whose Add Soul form never renders (stuck on the
                # profile-setup page) pauses instead of burning a row per try.
                if status == "failed" and "did not render" in error:
                    render_failures += 1
                    if render_failures >= Config.RENDER_FAILURE_LIMIT:
                        store.mark(
                            row["row_id"],
                            "pending",
                            "account blocked on profile-setup page - will retry on Resume",
                        )
                        store.set_account_state(account_id, "blocked")
                        push_log(
                            f"[{account_id}] ⛔ The Add Soul form won't render "
                            f"({render_failures} in a row) - account paused; it "
                            "likely needs manual profile setup. Rows stay pending."
                        )
                        set_progress(force=True)
                        return done_count, failed_count
                    # The row did nothing wrong - an account-level render
                    # problem must never burn rows; requeue and keep counting.
                    status = "pending"
                elif status != "failed":
                    render_failures = 0
                elif "did not render" not in error:
                    render_failures = 0

                if (
                    status == "failed"
                    and "session expired" in error.lower()
                    and session_retries < Config.SESSION_RETRY_LIMIT
                ):
                    session_retries += 1
                    push_log(
                        f"[{account_id}] Session expired - re-logging in "
                        f"(attempt {session_retries}/{Config.SESSION_RETRY_LIMIT})..."
                    )
                    try:
                        if login_sem is not None:
                            await login_sem.acquire()
                        try:
                            recovered = await sess.login(browser_pool, log=push_log)
                            await asyncio.sleep(Config.LOGIN_STAGGER_SECONDS)
                        finally:
                            if login_sem is not None:
                                login_sem.release()
                        if recovered:
                            if kind == "track":
                                await tracker.open()
                            continue  # retry this row with the fresh session
                    except Exception as exc:
                        push_log(f"[{account_id}] Re-login attempt failed: {exc}")
                    # Could not recover: leave this row (and the rest) pending.
                    store.mark(
                        row["row_id"], "pending", "session expired - will retry on Resume"
                    )
                    push_log(
                        f"[{account_id}] Session could not be recovered - rows stay "
                        "pending, press Resume to retry."
                    )
                    set_progress(force=True)
                    return done_count, failed_count
                break

            store.mark(
                row["row_id"],
                status,
                None if status in ("added", "success") else (result.get("error") or ""),
            )
            if status in ("added", "success"):
                done_count += 1
                if done_count % Config.LOG_EVERY == 0:
                    push_log(f"[{account_id}] {kind}: {done_count:,}/{assigned:,}")
            else:
                if status == "failed":
                    failed_count += 1
                label = result.get("soul") or soul_display(payload)
                push_log(
                    f"[{account_id}] {kind} {status}: {label} - {result.get('error') or ''}"
                )
                queue_result({**result, "stage": kind, "account": account_id})
            set_progress()
            await asyncio.sleep(Config.PACE_BETWEEN_SOULS)
    return done_count, failed_count


def start_async_run(mode: str) -> None:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(_run_all(mode))
    finally:
        loop.close()


# -- end C5 --


def _site_preflight() -> tuple[bool, str]:
    """Verify the REAL SoulTracker (frontend + JSON API) is up before a run.

    The domain was hijacked in Oct 2026 (spam pages, 403 API): starting a run
    against a broken site only burns login attempts while rows stay pending.
    Returns (ok, reason).
    """
    def _get(url: str) -> tuple[int, bytes]:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, resp.read(4096)

    try:
        status, body = _get("https://api.christembassysoultracker.org/config/app")
        if status != 200:
            return False, f"API /config/app returned HTTP {status} (site not restored yet?)"
        try:
            json.loads(body.decode("utf-8", "replace"))
        except ValueError:
            return False, "API /config/app did not return JSON (domain still hijacked?)"
    except Exception as exc:
        return False, f"API unreachable: {exc}"
    try:
        status, body = _get("https://www.christembassysoultracker.org/login")
        if status != 200 or b"SoulTracker" not in body:
            return False, "www/login is not serving the SoulTracker app"
    except Exception as exc:
        return False, f"www unreachable: {exc}"
    return True, "ok"


async def _run_all(mode: str) -> None:
    global worker_live
    store = JobStore()
    with _state_lock:
        worker_live = True
    run_state = "done"
    try:
        site_ok, site_reason = _site_preflight()
        if not site_ok:
            push_log(f"[system] ❌ Pre-flight failed: {site_reason}")
            push_log(
                "[system] Run NOT started - no rows touched. "
                "Press Resume once the real site is back."
            )
            return
        store.begin()  # state=running, re-queue rows left 'running' by a crash
        accounts = store.accounts()
        track_statuses = ["added"] if mode == "add_then_track" else ["pending"]
        summary = store.summary() or {}
        push_log(
            f"[system] Worker started [{mode}] - {summary.get('total', 0):,} souls, "
            f"{len(accounts)} accounts, {Config.MAX_CONCURRENT_ACCOUNTS} concurrent"
        )
        set_progress(force=True)

        try:
            await browser_pool.start()
        except Exception as exc:
            run_state = "failed"
            push_log(f"[system] ❌ Could not start the browser: {exc}")
        else:
            sem = asyncio.Semaphore(Config.MAX_CONCURRENT_ACCOUNTS)
            # All accounts logging in at the same instant overloads the site
            # (and starves the local event loop): logins go in small waves
            # while form-filling concurrency stays at MAX_CONCURRENT_ACCOUNTS.
            login_sem = asyncio.Semaphore(Config.MAX_CONCURRENT_LOGINS)

            async def account_worker(acct: dict) -> None:
                account_id = acct["account_id"]
                assigned = acct["assigned"]
                async with sem:
                    if not _account_has_work(store, account_id, mode):
                        store.set_account_state(account_id, "done")
                        return
                    sess = None
                    try:
                        sess = AccountSession(
                            acct["account_id"], acct["email"], acct["password"]
                        )
                        # The site flaps (timeouts under load): retry a failed
                        # login after a cooldown instead of parking the whole
                        # account and leaving its rows pending until Resume.
                        login_tries = 1 + max(Config.LOGIN_RETRY_ATTEMPTS, 0)
                        logged_in = False
                        for attempt in range(login_tries):
                            async with login_sem:
                                logged_in = await sess.login(
                                    browser_pool, log=push_log
                                )
                                # hold the slot briefly so login starts stay spaced
                                await asyncio.sleep(Config.LOGIN_STAGGER_SECONDS)
                            if logged_in or attempt >= login_tries - 1:
                                break
                            push_log(
                                f"[{account_id}] ⏳ Login failed - retrying in "
                                f"{Config.LOGIN_RETRY_COOLDOWN:.0f}s "
                                f"(attempt {attempt + 1}/{login_tries})..."
                            )
                            set_progress(force=True)
                            await asyncio.sleep(Config.LOGIN_RETRY_COOLDOWN)
                        if not logged_in:
                            store.set_account_state(account_id, "login_failed")
                            push_log(
                                f"[{account_id}] ❌ Login failed - rows stay pending, "
                                "press Resume to retry."
                            )
                            set_progress(force=True)
                            return
                        store.set_account_state(account_id, "running")
                        set_progress(force=True)

                        if mode in ADD_MODES:
                            done, failed = await _run_stage(
                                store, mode, "add", account_id, sess, assigned, login_sem
                            )
                            push_log(
                                f"[{account_id}] add stage done: "
                                f"{done:,} ok, {failed:,} failed"
                            )
                        if mode in TRACK_MODES and store.has_rows(
                            account_id, track_statuses
                        ):
                            done, failed = await _run_stage(
                                store, mode, "track", account_id, sess, assigned, login_sem
                            )
                            push_log(
                                f"[{account_id}] track stage done: "
                                f"{done:,} ok, {failed:,} failed"
                            )
                        store.set_account_state(account_id, "done")
                        push_log(f"[{account_id}] ✅ Finished {assigned:,} rows")
                    except Exception as exc:
                        store.set_account_state(account_id, "interrupted")
                        push_log(f"[{account_id}] ❌ Stopped: {exc}")
                    finally:
                        if sess is not None:
                            await sess.close()
                        set_progress(force=True)

            try:
                await asyncio.gather(*(account_worker(a) for a in accounts))
            except Exception as exc:
                run_state = "failed"
                push_log(f"[system] ❌ Run interrupted: {exc}")
            finally:
                await browser_pool.stop()

        final = store.finish(run_state)
        remaining = (
            (final.get("pending") or 0)
            + (final.get("running") or 0)
            + (final.get("added") or 0)
        )
        push_log(
            f"[system] ✅ Run finished - {final.get('ok', 0):,} ok, "
            f"{final.get('fail', 0):,} failed, {final.get('skip', 0):,} skipped, "
            f"{remaining:,} still pending."
        )
        set_progress(force=True)
        set_done(_build_snapshot())
    except Exception as exc:
        push_log(f"[system] ❌ Fatal error: {exc}")
        try:
            store.finish("failed")
            set_progress(force=True)
            set_done(_build_snapshot())
        except Exception:
            pass
    finally:
        with _state_lock:
            worker_live = False
        set_progress(force=True)  # final snapshot: live=False, real state


# NOTE: the __main__ server block lives at the END of this file - the
# auto-resume hook must run BEFORE socketio.run() blocks forever.


# ---------------------------------------------------------------------------
# Auto-resume (hosted deployments): start/continue the job as soon as the
# process comes up so a run keeps going with every browser closed - the
# dashboard is only a viewer; the worker lives inside this process.
# Enable with AUTO_RESUME=1 (and FLASK_DEBUG=0 in production).
# ---------------------------------------------------------------------------
def _auto_resume_if_configured() -> None:
    global worker_live
    if os.getenv("AUTO_RESUME", "0").lower() not in ("1", "true", "yes"):
        print(
            f"[auto-resume] skipped: AUTO_RESUME={os.getenv('AUTO_RESUME')!r} "
            f"WERKZEUG_RUN_MAIN={os.getenv('WERKZEUG_RUN_MAIN')!r}",
            flush=True,
        )
        return
    # Under the debug reloader only the child process may start a worker.
    if (
        os.getenv("FLASK_DEBUG", "1") == "1"
        and os.getenv("WERKZEUG_RUN_MAIN") is None
    ):
        print("[auto-resume] skipped: debug reloader parent", flush=True)
        return
    store = JobStore()
    with _state_lock:
        if worker_live:
            return
    job = store.get_job()
    summary = store.summary()
    unfinished = _unfinished(summary) if summary else 0
    if job is None or unfinished == 0:
        push_log("[system] AUTO_RESUME: nothing to do (no job or all rows terminal).")
        return
    mode = job["mode"]  # the job's own mode always wins (add / track / add_then_track)
    # A re-uploaded accounts file may carry new quotas - honour them on this
    # path too (it bypasses POST /run).
    try:
        _sync_staged_quotas(store)
    except JobError as exc:
        push_log(f"[system] ⚠ Quota re-sync skipped: {exc}")
    with _state_lock:
        if worker_live:
            return
        worker_live = True
    ensure_dispatcher()
    push_log(
        f"[system] AUTO_RESUME: resuming {unfinished:,} unfinished rows [{mode}] "
        "- no browser needed."
    )
    set_progress(force=True)
    threading.Thread(
        target=start_async_run, args=(mode,), daemon=True, name="soul-worker"
    ).start()


# Hosted servers (Passenger/gunicorn) import this module instead of running
# __main__, so the auto-start must happen at import time.
_auto_resume_if_configured()


if __name__ == "__main__":
    socketio.run(
        app,
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "5000")),
        debug=os.getenv("FLASK_DEBUG", "1") == "1",
        # systemd / Passenger have no TTY; flask-socketio refuses werkzeug
        # in that case unless we explicitly allow it (single-user, auth'd app).
        allow_unsafe_werkzeug=True,
    )





