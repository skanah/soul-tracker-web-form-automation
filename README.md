# SoulTracker — Multi-Account Web Form Automation (MaaS)

A Flask + Socket.IO dashboard that automates the **Christ Embassy SoulTracker**
website (`christembassysoultracker.org`) across **many accounts in parallel**.
Upload an accounts file and a souls file, pick a mode, and the app drives one
shared Chromium instance (Playwright, isolated browser contexts per account) to:

- **Add souls** — fill the *New Soul* form for every assigned row
- **Track souls** — open each soul's *Track* drawer and save church service,
  Foundation School class, KingsChat ID, phone, email
- **Add + Track** — add first (`added`), then track — crash-safe across stages

Built for very large runs: a **1-million-row CSV (~70 MB)** streams straight
into a SQLite job store, and every row is resumable — restart the server,
close your browser, reboot the VPS — the run continues where it left off.

---

## Table of Contents

- [Features](#features)
- [Architecture](#architecture)
- [Project Structure](#project-structure)
- [Requirements](#requirements)
- [Quick Start](#quick-start)
- [Files You Upload](#files-you-upload)
- [Run Modes](#run-modes)
- [The Dashboard](#the-dashboard)
- [Resume & Job Lifecycle](#resume--job-lifecycle)
- [HTTP API](#http-api)
- [Configuration Reference](#configuration-reference)
- [Deployment](#deployment)
- [Ops & Maintenance Scripts](#ops--maintenance-scripts)
- [Tests](#tests)
- [Security Notes](#security-notes)
- [Troubleshooting](#troubleshooting)

---

## Features

| Feature | Detail |
|---|---|
| **Multi-account parallelism** | One Chromium instance, one isolated browser context per account — up to `MAX_CONCURRENT_ACCOUNTS` (default 100) accounts at once |
| **Quota-aware assignment** | Optional `quota` column gives each account an exact number of souls; blank = even share of the remainder. Quotas can be re-applied mid-job by re-uploading the accounts file |
| **Million-row jobs** | Souls CSVs are streamed and copied byte-for-byte (leading zeros in phones survive); job state lives in SQLite (`data/job.db`), never in the browser |
| **Crash-safe resume** | Row states `pending → running → success\|failed\|skipped` (plus `added` between stages). Only unfinished rows are ever re-submitted. `AUTO_RESUME=1` continues a run at every process start — no browser open needed |
| **Live progress UI** | Socket.IO dashboard with overall bar, ETA, per-account table (assigned / done / ok / fail / state / mini-bar), and a `Failed rows.csv` download for retry passes |
| **Login resilience** | Staggered logins, per-IP login-cooldown detection, login retries with cooldown, proactive session refresh, re-login when a session dies mid-row |
| **Rate-limit friendly** | Global cap on simultaneous phone/email validations (the site's `/validate-*` API hangs under ~30+ concurrent calls), pacing between souls, exponential retry backoff |
| **Admin login** | Dashboard is password-protected (`ADMIN_PASSWORD`); a strong random password is generated to `data/admin_password.txt` on first boot if unset |
| **Deploy anywhere** | Same code runs on a PC (eventlet), cPanel/Passenger (`passenger_wsgi.py`) and a systemd-managed VPS (`deploy/soultracker.service`) |

## Architecture

```
                 ┌──────────────────────────────────────────────┐
   Browser ────▶ │  Flask + Socket.IO dashboard (app.py)        │
   (viewer only) │  login · upload · run · status · progress    │
                 └───────────────┬──────────────────────────────┘
                                 │ staging + job control
                 ┌───────────────▼──────────────────────────────┐
                 │  SQLite job store (automation/job_store.py)  │
                 │  data/job.db — one row per soul, resumable    │
                 └───────────────┬──────────────────────────────┘
                                 │ batch claims (BATCH_SIZE = 25)
                 ┌───────────────▼──────────────────────────────┐
                 │  Worker (asyncio, in-process)                 │
                 │  one task per account                         │
                 └───────────────┬──────────────────────────────┘
                                 │
                 ┌───────────────▼──────────────────────────────┐
                 │  Playwright: one Chromium, many contexts      │
                 │  automation/session_manager.py  — logins      │
                 │  automation/form_filler.py      — Add Soul    │
                 │  automation/track_filler.py     — Track souls │
                 │  automation/browser.py          — pool        │
                 └──────────────────────────────────────────────┘
```

Your browser is **only a viewer**. The worker lives inside the Flask process,
so closing the tab, the browser or your laptop does not stop a run — state is
reloaded from `data/job.db` on restart and Socket.IO resynchronises the UI.


---

## Project Structure

```
.
├── app.py                  # Flask dashboard + Socket.IO + in-process worker
├── config.py               # all tuning knobs + target URLs (env-overridable)
├── passenger_wsgi.py       # cPanel/Passenger entrypoint (WSGI: application)
├── requirements.txt        # pinned deps (Python 3.11–3.13)
├── automation/
│   ├── browser.py          # shared Chromium pool (one browser, many contexts)
│   ├── session_manager.py  # login, session refresh, retry/cooldown logic
│   ├── form_filler.py      # Add Soul form automation
│   ├── track_filler.py     # Track Souls drawer automation + column mapping
│   ├── tracker.py          # site navigation helpers
│   ├── validator.py        # phone/email sanity checks
│   └── job_store.py        # SQLite job store: rows, quotas, assignment, resume
├── templates/              # index.html (dashboard), login.html
├── static/                 # stylesheet
├── deploy/
│   └── soultracker.service # systemd unit for a VPS
├── data/                   # created at runtime — uploads, job.db, logs (never commit)
├── requeue.py              # ops: requeue transient failures to pending
├── rebalance.py            # ops: redistribute pending rows across accounts
├── filter_done.py          # ops: strip already-processed souls from a CSV
├── export_retry.py         # ops: build a retry CSV from the job store
├── site_watcher.sh         # ops: monitor the target site / auto-restart run
├── test_quota_sync.py      # quota parsing + mid-job re-slice checks
├── test_track_flow.py      # track / add_then_track state-machine checks
├── DEPLOY_CPANEL.md        # full cPanel deployment walkthrough
└── DEPLOY_VPS.md           # full VPS (systemd) deployment walkthrough
```

## Requirements

- **Python 3.11 – 3.13** (pins in `requirements.txt` require cp313/py3 wheels)
- A desktop OS or Linux server with **Chromium** (~150 MB RAM per concurrent
  account context — size `MAX_CONCURRENT_ACCOUNTS` to your RAM)
- Internet access to `christembassysoultracker.org`

## Quick Start

```bash
git clone https://github.com/skanah/soul-tracker-web-form-automation.git
cd soul-tracker-web-form-automation

python -m venv .venv
source .venv/bin/activate            # Windows: .\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m playwright install chromium
python app.py                        # http://127.0.0.1:5000
```

On first boot the app prints a generated admin password (also saved to
`data/admin_password.txt`) — set `ADMIN_PASSWORD` to choose your own.

## Files You Upload

### Accounts file (`.csv` or `.xlsx`)

Columns `account_id, email, password`, plus an optional quota column named
exactly `quota` (matched case-insensitively, `Quota` == `quota`):

| account_id | email | password | quota |
|---|---|---|---|
| acc1 | a1@x.com | pw | 50000 |
| acc2 | a2@x.com | pw | 100000 |
| acc3 | a3@x.com | pw |  |
| acc4 | a4@x.com | pw |  |

`quota` = exactly how many souls that account takes (accepts `50000`, `50,000`
or `50k`). **Blank = even share of whatever is left.** Rows are assigned in
file order; if quotas exceed the row count the tail accounts are truncated
with a warning, and if every account has a quota and rows remain, the
remainder is reported as *unassigned* (never silently exceeded).

Re-uploading an accounts file with different quotas **mid-job re-slices the
still-pending rows** to the new quotas — on Resume, or automatically at the
next restart with `AUTO_RESUME=1`. Finished rows always keep their credit.

### Souls file (`.csv` or `.xlsx`)

Required: `firstName`, `lastName`, and `phone` **or** `email`.
Optional: `gender`, `city`, `state`, `title`, plus tracking columns
`churchService`, `foundationClass`, `kingsChatId`.

- CSV is preferred and is **copied byte-for-byte** (leading zeros in phones
  survive); a 1M-row CSV (~70 MB) streams straight to disk.
- `.xlsx` is fine for small files and is re-serialised to `data/souls.csv`.
- Rows with neither phone nor email are auto-`skipped` (un-submittable).


## Run Modes

| Mode | What it does |
|---|---|
| **add** | Fills the New Soul form for every assigned row |
| **track** | Opens each soul's Track drawer and saves church service / Foundation class / KingsChat ID / phone / email |
| **add_then_track** | Adds every row first (status `added`), then tracks it. A crash between stages resumes at the track stage — never re-adding |

The mode sticks to the job: `AUTO_RESUME` continues in the same mode.

## The Dashboard

1. **Login** — admin password (see above).
2. **Accounts** — upload the accounts file (staged, quota-checked, live totals).
3. **Souls** — upload the souls file (streamed; big files keep the tab busy but
   never block the worker).
4. **Run** — choose add / track / add + track and start.
5. **Progress** — appears when a run starts: overall bar with
   `processed / total`, ✅/❌/⏭/➕/🔄/⏳ counters, **ETA**, and a
   **per-account table** (assigned / done / ok / fail / state / mini-bar).
   The badge cycles `running → done` (green, with timestamp and totals);
   warnings (unassigned rows, truncated quotas) show under the table.
   Everything survives a page reload via `GET /status`.
6. **Failed rows** — a **⬇ Failed rows.csv** button streams failures back
   (`GET /failures.csv`) so you can fix and re-upload for a retry pass.

## Resume & Job Lifecycle

State lives in SQLite (`data/job.db`), one row per soul:

```
pending ──▶ running ──▶ success | failed | skipped
                │ (add_then_track)
                └──▶ added ──▶ (track stage) ──▶ success | failed
```

- The Run button becomes **Resume run** while a job has unfinished rows
  (crash, restart, login failures) — only unfinished rows are re-submitted.
- When everything is terminal it becomes **Start new run** (asks for
  confirmation, then `POST /job/reset`).
- With `AUTO_RESUME=1` (recommended on servers) the worker starts with the
  process and resumes unfinished rows at **every** boot — no browser open.
- Full audit log per run: `data/run-<timestamp>.log`.

## HTTP API

| Method | Endpoint | Purpose |
|---|---|---|
| GET | `/` | Dashboard (login required) |
| GET/POST | `/login`, `/logout` | Admin auth |
| POST | `/upload/accounts` | Stage an accounts CSV/XLSX |
| POST | `/upload/souls` | Stage a souls CSV/XLSX |
| POST | `/run` | Start/resume a run (`mode=add\|track\|add_then_track`) |
| GET | `/status` | JSON snapshot (job totals, per-account rows, state) |
| GET | `/failures.csv` | Download failed rows as CSV |
| POST | `/job/reset` | Clear job state for a fresh run |


## Configuration Reference

### Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `ADMIN_PASSWORD` | generated → `data/admin_password.txt` | Dashboard login password |
| `SECRET_KEY` | `change-me-in-prod` | Flask session key — **set in production** |
| `HOST` / `PORT` | `0.0.0.0` / `5000` | Bind address for `python app.py` |
| `FLASK_DEBUG` | `1` local / `0` via `passenger_wsgi.py` | Debug mode — keep `0` in production |
| `AUTO_RESUME` | off | `1` = start/resume the worker with the process (recommended on servers) |
| `SOCKETIO_ASYNC_MODE` | `eventlet` | `threading` is set automatically under cPanel/Passenger |
| `MAX_CONCURRENT_ACCOUNTS` | `100` | Parallel browser contexts — **size to RAM (~150 MB each)** |
| `MAX_CONCURRENT_LOGINS` | `3` | Simultaneous logins (the site enforces a per-IP login limit) |
| `LOGIN_STAGGER_SECONDS` | `3` | Spacing between login starts |
| `NETWORK_LOGIN_COOLDOWN` | `300` | Seconds ALL logins pause after "Too many login attempts from this network" |
| `LOGIN_RETRY_ATTEMPTS` | `2` | Extra tries when a login fails (flaky site / bad network) |
| `LOGIN_RETRY_COOLDOWN` | `120` | Seconds between login retries |
| `MAX_CONCURRENT_VALIDATIONS` | `10` | Global cap on simultaneous phone/email checks (site API hangs past ~30) |
| `SESSION_RETRY_LIMIT` | `2` | Re-logins when a session dies mid-row |
| `SESSION_REFRESH_MINUTES` | `20` | Proactive token renewal (avoids rate-limited re-logins) |
| `RENDER_FAILURE_LIMIT` | `3` | Consecutive form-render failures before an account is paused |

### config.py knobs

Beyond the env-overridable values above, `config.py` also holds the target
URLs (form / login / track pages), timeouts (`VERIFICATION_TIMEOUT=120s`,
`SOUL_SUBMIT_TIMEOUT=60s`, `LOCATION_LOAD_TIMEOUT=30s`, …), retry pacing
(`RETRY_BASE_WAIT`, `PHONE_VERIFICATION_ATTEMPTS=3`), the Track page selectors,
and `HEADLESS` (set `False` to watch the browser work — great for debugging).

## Deployment

### 1. Run it on your own PC

Follow [Quick Start](#quick-start), disable sleep, and use `AUTO_RESUME=1`
so a reboot resumes the run automatically. Fine for everything except 24/7
availability.

### 2. cPanel / shared hosting (Passenger)

Full walkthrough: **[DEPLOY_CPANEL.md](DEPLOY_CPANEL.md)**. Summary:

1. Upload the project (exclude `.venv/`, `__pycache__/`; keep `data/` to
   preserve job state) and extract it.
2. *Setup Python App*: Python 3.11–3.13, app root = project folder,
   **startup file `passenger_wsgi.py`** (WSGI entry `application`).
3. Install deps: `python -m pip install -r requirements.txt`
4. Install the browser once: `python -m playwright install chromium`
5. Set env vars on the app: `AUTO_RESUME=1`, `FLASK_DEBUG=0`,
   `ADMIN_PASSWORD=...` (plus tuning knobs). `SOCKETIO_ASYNC_MODE=threading`
   is applied automatically by `passenger_wsgi.py`.
6. Keep-alive cron (Passenger idles apps after ~5 min without requests):
   `*/5 * * * * curl -s http://127.0.0.1:$PORT/status > /dev/null`

**Shared-host checklist (all must be true — Playwright is heavy):**

| Requirement | Why |
|---|---|
| "Setup Python App" (Passenger) exists | no Python → no app |
| ≥ 2 GB RAM (more for big concurrency) | Chromium ≈ 150 MB per context — start with `MAX_CONCURRENT_ACCOUNTS=10–25` |
| Process/thread limit ≥ ~100 | one Chromium = many threads |
| ≥ 1.5 GB free disk | browser binaries ~400 MB + job data |
| `playwright install chromium` completes | needs common system libs; if it fails on missing `.so` files and your host can't install them, shared hosting can't run this |
| Long-running processes allowed | Passenger apps are exempt from entry-process limits |

If any box fails, the reliable alternative is a ~$5–7/mo VPS — same code,
zero changes.

### 3. VPS (systemd + nginx) — recommended

Full walkthrough: **[DEPLOY_VPS.md](DEPLOY_VPS.md)**; ready-made unit in
**[deploy/soultracker.service](deploy/soultracker.service)**. The unit sets
`FLASK_DEBUG=0`, `AUTO_RESUME=1` (unfinished runs continue on boot), binds
`127.0.0.1:5000` behind nginx, and `Restart=always` brings the app back after
any crash or reboot.

```bash
sudo cp deploy/soultracker.service /etc/systemd/system/
# edit User, WorkingDirectory and the venv path inside the unit
sudo systemctl daemon-reload
sudo systemctl enable --now soultracker
journalctl -u soultracker -f        # live logs
```

Size `MAX_CONCURRENT_ACCOUNTS` to the box's RAM (~150 MB per account, roughly
5 accounts per GB) — running out of memory is the #1 reason a deployed
dashboard "stops" mid-run.


## Ops & Maintenance Scripts

Run from the project root. The job-store scripts are safe **only while no
worker is live** (stop the service / don't press Resume first).

| Script | What it does |
|---|---|
| `requeue.py` | Moves transiently-failed rows (timeouts etc.) and orphaned `running` rows back to `pending` for a retry. Terminal errors (duplicate phone, invalid gender/name) stay `failed`. |
| `rebalance.py` | Redistributes `pending` rows so all accounts progress in parallel, updating assigned counts so dashboard totals stay exact. |
| `filter_done.py` | Streams a souls CSV and writes a copy **without** souls the current job already finished (`--done-statuses` configurable) — a fresh job never re-submits done people. Stdlib-only. |
| `export_retry.py` | Builds a retry CSV from the job store: keeps pending/running + transient failures, drops successes and permanent failures, and writes a review CSV of every dropped row with the reason. Stdlib-only. |
| `site_watcher.sh` / `site_watcher_once.sh` | Monitor the target site and re-kick the run when it comes back / goes down. |
| `build_zip.py` | Local packaging helper — builds the cPanel upload zip, excluding venv, `data/`, caches and local-only artefacts. |

## Tests

```bash
python test_quota_sync.py    # quota parsing + mid-job quota re-slicing (15 checks)
python test_track_flow.py    # track / add_then_track state machines + mapping (51+ checks)
```

Both suites run against throwaway temp sandboxes with `AUTO_RESUME=0` — your
`data/` folder is never touched by tests.

## Security Notes

- **`data/` is never committed** (see `.gitignore`): it contains account
  passwords (`accounts.csv`), the admin password, the job DB and uploaded
  soul data (PII). Treat every copy of it as sensitive.
- Set `ADMIN_PASSWORD` and a strong `SECRET_KEY` before exposing the dashboard
  on any network; keep it behind a reverse proxy or firewall where possible.
- Full per-row outcomes and login attempts are audit-logged to
  `data/run-<timestamp>.log`.
- Use a **private** repository if you fork this with your own account data.

## Troubleshooting

| Symptom | Fix |
|---|---|
| Every login/submit fails | Check the target site is actually up and serving the real app (not a maintenance/spam page). Deployments can be prepared, but runs only work against the live site. |
| Run "stops" on a server | Usually OOM or an idle-killed process. Lower `MAX_CONCURRENT_ACCOUNTS` (~5 accounts per GB RAM), use the systemd unit (`Restart=always`) or the cPanel keep-alive cron. |
| Logins rejected en masse | The site rate-limits logins per IP — lower `MAX_CONCURRENT_LOGINS`, raise `LOGIN_STAGGER_SECONDS`; `NETWORK_LOGIN_COOLDOWN` handles the "too many attempts" banner automatically. |
| Many rows fail phone/email validation | The site's `/validate-*` API degrades under load — lower `MAX_CONCURRENT_VALIDATIONS`, raise `VERIFICATION_TIMEOUT`. |
| A big upload seems stuck | 1M-row CSVs stream to disk server-side; watch the console — the worker is never blocked by an upload. |
| Want to see what the browser does | Set `HEADLESS = False` in `config.py`, restart, watch Chromium work. |
| Retry a partially failed run | `GET /failures.csv` → fix the data → reset the job → re-upload, or use `requeue.py` / `export_retry.py`. |

---

**Responsible use:** this tool automates data entry on a website you are
authorised to use. Keep concurrency and pacing civil, respect the target
site's resources, and make sure your account usage complies with the site's
terms of service.



