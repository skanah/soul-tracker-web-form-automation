# Deploying to cPanel Shared Hosting — Step-by-Step Guide

What you get after these steps: the dashboard runs **on the server 24/7**, the
job auto-starts and auto-resumes **with every browser closed**, and a cron
watcher starts the run by itself the moment the SoulTracker API is restored.

> **Two things to know first**
> 1. **Passenger stops idle apps** (~5 min without requests). Step 7's
>    keep-alive cron is **required** — it keeps the process (and the worker
>    thread inside it) alive.
> 2. **The dashboard is behind an admin login.** Anyone with the URL is asked
>    for a password (`ADMIN_PASSWORD`); without it a random one is generated
>    and saved to `data/admin_password.txt` on first boot. `data/accounts.csv`
>    contains account passwords — set `ADMIN_PASSWORD` (Step 5) before
>    sharing the address anywhere.

---

## 0. Host checklist (2 minutes)

In cPanel, confirm you have all of these. If any is missing, shared hosting
cannot run this app (a ~$5–7/mo VPS is the fallback — same zip, same steps):

- [ ] **Setup Python App** (Passenger) icon exists
- [ ] **RAM** ≥ 2 GB for your account (Chromium ≈ 150 MB per browser context)
- [ ] **Disk quota** ≥ 1.5 GB free (browser binaries ~400 MB + 50 MB job data)
- [ ] **Process/thread limit** ≥ ~100 (check `ulimit -u` in Terminal)
- [ ] Terminal or SSH access (to run pip + playwright)

## 1. Upload & extract

1. cPanel → **File Manager** → your home directory.
2. New folder: `soultracker` → open it → **Upload** → select
   `soultracker-cpanel.zip` → when done → right-click zip → **Extract**.
3. You should see: `app.py`, `passenger_wsgi.py`, `config.py`,
   `requirements.txt`, `automation/`, `templates/`, `static/`, `data/`, etc.

## 2. Create the Python application

cPanel → **Setup Python App** → **Create Application**:

| Field | Value |
|---|---|
| Python version | 3.11 / 3.12 / 3.13 (highest offered) |
| Application root | `soultracker` (ends with `/soultracker`) |
| Application URL | e.g. `https://yourdomain.com/soultracker` or a subdomain |
| Startup file | `passenger_wsgi.py` — if asked for a module target: `passenger_wsgi:application` |
| Auto-start | on |

Click **Create**. A first-start error is normal (deps not installed yet).

## 3. Install Python dependencies

cPanel → **Terminal**:

```bash
cd ~/soultracker
~/venv/envs/<appname>/bin/pip install -r requirements.txt
```

(`<appname>` = the name from Step 2. On hosts without `~/venv/envs`, use the
pip shown on the Setup Python App page, or
`python3 -m pip install --user -r requirements.txt`.)

Expect: flask, flask-socketio, eventlet, playwright, pandas, openpyxl,
python-dotenv — all from wheels, no compilers needed.

## 4. Install the Chromium browser — the critical step

```bash
~/venv/envs/<appname>/bin/python -m playwright install chromium
```

- Downloads ~400 MB into `~/ms-playwright` (check quota first).
- **Smoke test** (must print `OK <version>`):

```bash
~/venv/envs/<appname>/bin/python -c "
from playwright.sync_api import sync_playwright
p = sync_playwright().start()
b = p.chromium.launch(args=['--no-sandbox','--disable-dev-shm-usage','--disable-gpu'])
print('OK', b.version)
b.close(); p.stop()"
```

- ❌ Missing `.so` library errors → host lacks Chromium's system libs and
  shared hosting can't add them → VPS fallback (Section 9).

## 5. Environment variables (critical)

cPanel → **Setup Python App** → your app → **Environment variables** (or
"Custom environment variables"). Add these, then **Restart** the app:

| Variable | Value | Why |
|---|---|---|
| `AUTO_RESUME` | `1` | Worker starts by itself on boot — runs with every browser closed |
| `FLASK_DEBUG` | `0` | Production mode (no reloader, no debugger) |
| `SOCKETIO_ASYNC_MODE` | `threading` | Plain WSGI under Passenger (no eventlet worker) |
| `SECRET_KEY` | `<long random string>` | Session signing — generate one, don't use the default |
| `ADMIN_PASSWORD` | `<your password>` | Dashboard login. Without it a random password is generated into `data/admin_password.txt` (it is also printed to the log) — set your own instead |
| `MAX_CONCURRENT_ACCOUNTS` | `10` | ⚠️ Start here. Shared hosting cannot do 100 (each browser context ≈ 150 MB RAM). Raise to 15–25 only if RAM allows; watch memory |
| `MAX_CONCURRENT_LOGINS` | `3` | Site rate-limits logins per IP — do not raise |
| `LOGIN_STAGGER_SECONDS` | `3` | Spacing between logins — do not lower |
| `NETWORK_LOGIN_COOLDOWN` | `300` | Global pause after "Too many login attempts" |
| `MAX_CONCURRENT_VALIDATIONS` | `6` | Site's validation API hangs under load — do not raise first |

`passenger_wsgi.py` already defaults `FLASK_DEBUG=0` and
`SOCKETIO_ASYNC_MODE=threading`, but setting them explicitly in cPanel wins
over any host defaults — do both.

## 6. Restart & verify

1. **Setup Python App → Restart** the application.
2. Open the **Application URL** — the dashboard should load (HTTP 200).
3. Check `/status` (append `/status` to the URL): it should show your job
   totals (`ok`, `pending`, …) carried over in `data/job.db`.
4. If you see `❌ Pre-flight failed: API unreachable: HTTP Error 403` in the
   log panel — that is **expected right now** (see Section 8). The worker
   refuses to start a futile run; nothing is lost.

## 7. Cron jobs — keep-alive + site watcher (required)

cPanel → **Cron Jobs** → add these two (every 5 minutes, offset by 2–3 min):

**a) Keep-alive ping** — Passenger stops idle apps (~5 min without requests),
which would kill the worker thread inside the process. This keeps it alive.
Replace the URL with your Application URL:

```bash
*/5 * * * * curl -s -o /dev/null --max-time 60 https://yourdomain.com/soultracker/
```

**b) Site watcher** — single-pass script that triggers the run by itself the
moment the SoulTracker API is restored (no browser needed):

```bash
*/5 * * * * DASH_URL=https://yourdomain.com/soultracker bash $HOME/soultracker/site_watcher_once.sh
```

(`site_watcher_once.sh` is included in this zip. `site_watcher.sh` is the
infinite-loop variant for local PCs — do **not** use it on cPanel.)

**c) Lock the dashboard down.** The dashboard now has its own admin login
(see `ADMIN_PASSWORD` in Step 5): every page, upload and the live log feed
require the password, failed logins are rate-limited per IP, and sessions
expire after 12 hours. `data/accounts.csv` still contains real account
passwords, so as defence in depth you can also add: cPanel → **Directory
Privacy** → password-protect the app URL, or restrict by IP if yours is
static.

## 8. Current site status — read before expecting results

As of packaging: the SoulTracker **frontend is restored** but the **API is
still down** (`api.christembassysoultracker.org` → HTTP 403 from a hijack
server). Every run attempt will be refused by the pre-flight gate until the
site owner repoints the API's DNS. **This is not an app bug.** The cron
watcher (Step 7b) will start the job automatically within ~5 minutes of the
API returning — check `data/site_watcher.log` (File Manager) for
`api=403 (waiting)` lines in the meantime.

## 9. If cPanel can't run it — VPS fallback (same zip, same steps)

Symptoms: no "Setup Python App", Chromium `.so` errors, or the host kills the
process (OOM) even at `MAX_CONCURRENT_ACCOUNTS=10`. A ~$5–7/mo VPS (Ubuntu,
2 GB RAM) runs this zip unchanged:

```bash
sudo apt install python3 python3-venv -y
cd /opt && unzip soultracker-cpanel.zip -d soultracker && cd soultracker
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m playwright install chromium --with-deps
export AUTO_RESUME=1 FLASK_DEBUG=0 SOCKETIO_ASYNC_MODE=threading \
  SECRET_KEY='<random>' MAX_CONCURRENT_ACCOUNTS=25
nohup .venv/bin/python app.py > app.log 2>&1 &
```

(Prefer systemd for a permanent service; the nohup line is enough to verify.)

## 10. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Dashboard 500 on first start | Deps not installed yet (Step 3) — check the app's `stderr.log` in cPanel |
| `Executable doesn't exist` (playwright) | Step 4 not done — run the chromium install + smoke test |
| Process killed / 503 under load | Out of RAM — lower `MAX_CONCURRENT_ACCOUNTS` to 5–10, restart |
| Run stops after ~5 idle minutes | Keep-alive cron missing (Step 7a) — Passenger idled the app |
| `Pre-flight failed: API ... 403` | Site API still down (Section 8) — wait; watcher auto-starts |
| Logins fail "Too many login attempts" | Site per-IP limit — the 300s global cooldown handles it; don't press Resume repeatedly |
| Job restarts from scratch? | It doesn't — state is `data/job.db`. Never delete `data/`; the zip ships your current job (7,531 added / 47,286 pending) |

