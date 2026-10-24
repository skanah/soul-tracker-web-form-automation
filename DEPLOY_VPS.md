# Updating st.skanah.com (Ubuntu VPS) — step-by-step guide

This guide replaces the code on the VPS with the current fixes and puts the
dashboard under **systemd**, so the site stops going down and runs keep going
with every browser closed.

**What this update ships**

| Fix | Why it matters |
|---|---|
| Quota column is now observed (case-insensitive: `Quota` == `quota`) | accounts were silently split evenly before |
| Re-uploaded quotas re-slice pending rows (on Resume **and** on service restart) | quotas can be applied to a job that is already running |
| `AUTO_RESUME` ordering fix (it was dead code under `python app.py`) | **this is why st.skanah.com "stops" and stays stopped** |
| Failed logins retry with a cooldown (`LOGIN_RETRY_*`) | a flaky site no longer stalls the run |
| `deploy/soultracker.service` + RAM-capped concurrency | crash/reboot auto-recovery; OOM is the #1 "site stops" cause |

**What you need:** SSH access to the VPS (sudo), ~15 minutes, and one short
run interruption — nothing is lost, the run resumes by itself.

> Commands below use `$APP` = the folder on the server that contains `app.py`
> (find it in Step 0). **`$APP` is just shorthand — set it in your shell first:**
> `export APP=/opt/soultracker` (use your real path). Run them **on the VPS**
> unless marked "on your PC".

---

## 0. See how it runs today (2 min)

```bash
ssh user@st.skanah.com          # or: ssh user@<vps-ip>

# where is the app?
sudo find /opt /var/www /home -maxdepth 4 -name app.py -path '*soul*' 2>/dev/null
ps aux | grep -E 'app.py|gunicorn|passenger' | grep -v grep
systemctl list-units --type=service | grep -i -E 'soul|flask'
crontab -l; screen -ls; tmux ls   # any of these may hold the old process
```

Note down: the app folder (`$APP`) and how it currently starts
(`python app.py` in screen/nohup? a service? Passenger?). If there is **no**
service and you see `python ... app.py` in `ps` — that is exactly why it dies
on every disconnect/crash; Step 5 fixes that permanently.

## 1. Back up the live job (30 s — do not skip)

`data/job.db` is the run's memory (all progress, millions of rows). Back it up
before touching anything:

```bash
cd $APP
tar czf ~/soul-backup-$(date +%F-%H%M).tar.gz data/
ls -lh ~/soul-backup-*     # keep this file until the update is verified
```

## 2. Build & upload the new code

On your PC (repo folder):

```powershell
python build_zip.py                    # rebuilds soultracker-cpanel.zip (code only - no data/)
scp soultracker-cpanel.zip user@st.skanah.com:/tmp/
```

On the VPS — update code **without ever touching `data/`**:

```bash
cd $APP
unzip -o /tmp/soultracker-cpanel.zip -d "$APP" -x 'data/*'
ls deploy/soultracker.service test_quota_sync.py   # both must exist now
```

(`build_zip.py` no longer packs `data/`, and the `-x 'data/*'` guard is a second
layer: your server-side job state can never be overwritten by an update.)

## 3. Stop whatever runs the app now

```bash
sudo systemctl stop soultracker 2>/dev/null    # if already a service
pkill -f 'python.*app.py'                      # screen/nohup leftovers
screen -S soul -X quit 2>/dev/null; tmux kill-session -t soul 2>/dev/null
curl -s -o /dev/null -w '%{http_code}\n' --max-time 5 http://127.0.0.1:5000/login
# expect 000 = stopped. 200 = something still runs -> find it with:
sudo ss -ltnp | grep 5000
```

## 4. Dependencies (only if the app folder is new or Python was upgraded)

```bash
cd $APP
[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/playwright install chromium
.venv/bin/python test_quota_sync.py      # 13 checks - validates the update on the server
```

If `playwright install chromium` fails on missing `.so` libraries, run
`sudo .venv/bin/playwright install-deps chromium` (needs apt).

## 5. Install the systemd service (the fix that stops the "stopping")

**Already have a `soultracker.service`?** (`systemctl status soultracker`) —
back it up and merge, don't replace blindly:

```bash
systemctl cat soultracker | tee /root/soultracker.service.bak   # backup + shows the file path
grep -E 'Environment|User|ExecStart|WorkingDirectory' /root/soultracker.service.bak
# note your ADMIN_PASSWORD line (if any) and the User= before continuing -
# keep them in the new unit below, or your dashboard login/permissions change
```

Fresh install (or after the backup above):

```bash
sudo cp deploy/soultracker.service /etc/systemd/system/soultracker.service
sudo nano /etc/systemd/system/soultracker.service
```

Edit these lines to match your box:

```ini
User=<your ssh user>                 # NOT www-data unless it owns $APP
WorkingDirectory=/opt/soultracker-maas    # -> your $APP
ExecStart=/opt/soultracker-maas/.venv/bin/python app.py   # -> $APP/.venv/bin/python
Environment=MAX_CONCURRENT_ACCOUNTS=10    # ~RAM_GB x 5 (2 GB box -> 10!)
Environment=ADMIN_PASSWORD=<choose one>   # dashboard login (else see data/admin_password.txt)
Environment=SECRET_KEY=<long random>      # e.g. openssl rand -hex 24
```

Leave the rest as-is: `FLASK_DEBUG=0`, `AUTO_RESUME=1`, `HOST=127.0.0.1`,
`PORT=5000`, `SOCKETIO_ASYNC_MODE=eventlet`, `Restart=always`. (`python-dotenv`
is installed but unused by the app — env vars must live in this unit or an
`EnvironmentFile=` you add.)

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now soultracker
```

## 6. nginx checks (5 min — skip if uploads & live log already work)

```bash
sudo nginx -T | grep -n -A10 'st.skanah.com'
```

Inside the `server { ... }` block for st.skanah.com confirm (add if missing):

```nginx
client_max_body_size 600m;        # souls files are huge -> else 413 on upload
location / {
    proxy_pass http://127.0.0.1:5000;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_read_timeout 3600s;     # long runs / streaming
    proxy_http_version 1.1;                       # socket.io live log
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
}
```

Then `sudo nginx -t && sudo systemctl reload nginx`. Your TLS/certbot setup
stays exactly as it is.

## 7. Start & verify

```bash
systemctl status soultracker --no-pager        # must be: active (running)
ls -t $APP/data/run-*.log | head -1 | xargs tail -f
```

Expected lines within ~1 minute:

```
[system] AUTO_RESUME: resuming N unfinished rows [add] - no browser needed.
[system] Worker started [add] - ... souls, ... accounts, ... concurrent
[<account>] ✅ Logged in as ...
```

Then in a browser: open `https://st.skanah.com` → login (Step 5's
`ADMIN_PASSWORD`) → the Progress card shows per-account counts and the live
log moves. `curl -sI https://st.skanah.com/login` must answer `200`.

## 8. Apply the quotas to the current job

> ⚠ Upload the accounts file only **after** the new code is live (Step 7). An
> upload handled by the old process is parsed by the old buggy parser, which
> silently dropped quota columns — then the restart finds an empty `quota`
> column and (correctly) re-slices nothing.

1. Put your numbers in the accounts file's `quota` column
   (`50000`, `50,000` or `50k`; blank = even share of the rest).
2. Dashboard → upload the accounts file again.
3. Either press **Resume run** (when the run is idle), or simply
   `sudo systemctl restart soultracker` (works even mid-run — the run
   continues automatically).
4. The run log must show:
   `[system] Quota plan updated - N pending rows re-assigned to the uploaded
   quotas (finished rows kept).` — the per-account "assigned" numbers now match
   your quotas. Finished rows stay with the account that processed them, so a
   quota is matched exactly wherever the finished rows allow.

**Verify the quotas really landed** (the word "quota" only appears in the
header, so `grep quota` proves nothing — check the values and the plan):

```bash
cut -d',' -f1,4 $APP/data/accounts.csv | head -30      # staged file: id + quota values
cd $APP && .venv/bin/python - <<'EOF'
import sqlite3
con = sqlite3.connect("data/job.db")
print(f"{'account_id':30} {'quota':>8} {'assigned':>9}")
for r in con.execute("SELECT account_id, quota, assigned FROM accounts ORDER BY seq"):
    print(f"{r[0]:30} {str(r[1]):>8} {r[2]:>9}")
EOF
```

- `quota` column shows numbers **and** `assigned` matches them → done ✅
- every `quota` is `NULL` → the accounts file has no quota values (or was never
  re-uploaded after the update — old uploads had their quotas silently dropped)
- no `Quota plan updated` line in the run log → the staged file is identical to
  the job's stored plan, so nothing needed re-slicing

**Why some accounts show `assigned` ABOVE their quota:** finished rows always
keep their credit — an account that already processed more rows than its (new)
quota keeps them and simply never receives another row. To see it clearly:

```bash
cd $APP && .venv/bin/python - <<'EOF'
import sqlite3
con = sqlite3.connect("data/job.db")
print(f"{'account_id':28} {'quota':>6} {'done':>6} {'pend':>6} {'total':>6}  verdict")
for a in con.execute("SELECT account_id, quota FROM accounts ORDER BY seq"):
    d = con.execute(
        "SELECT SUM(status!='pending'), SUM(status='pending')"
        " FROM souls WHERE account_id=?", (a[0],)).fetchone()
    done, pend = int(d[0] or 0), int(d[1] or 0)
    q = a[1]
    if q is None:
        verdict = "blank -> even share of the rest"
    elif done > q:
        verdict = f"over quota: {done} done before the quota existed (kept)"
    elif done + pend == q:
        verdict = "exact"
    else:
        verdict = f"target {q}"
    print(f"{a[0]:28} {str(q):>6} {done:>6} {pend:>6} {done+pend:>6}  {verdict}")
EOF
grep -n '⚠' $(ls -t $APP/data/run-*.log | head -1)   # quota warnings from the re-slice
```

Healthy output: over-quota accounts have `pend 0`, everyone else's `total`
equals the quota, and the `grep` shows the `finished rows already exceed`
warnings for exactly those accounts.

> `ls: cannot access 'data/run-*.log'` means you are not in `$APP` — always use
> the full path (`/opt/soultracker/data/run-*.log`) or `cd $APP` first. All
> `data/...` paths in this guide are relative to `$APP`.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| nginx 502 / 504 | app not running | `systemctl status soultracker`; `journalctl -u soultracker -n 50` |
| `413` on file upload | nginx body limit | Step 6: `client_max_body_size 600m` |
| Site "stops" under load, journal shows OOM/kill | too many Chromiums (≈150 MB each) | lower `MAX_CONCURRENT_ACCOUNTS` (Step 5) |
| No `AUTO_RESUME: resuming` line after restart | env missing | `systemctl show soultracker \| grep ENVIRONMENT`; ensure `AUTO_RESUME=1` |
| Rows stuck as `running` | hard kill mid-row | harmless — next start requeues them (`begin()`) |
| Dashboard asks for a password you don't know | random one generated | `cat $APP/data/admin_password.txt` (or set `ADMIN_PASSWORD` in the unit) |
| Live log / progress frozen | socket.io blocked by proxy | Step 6: `Upgrade`/`Connection` headers |
| "Quota plan updated" never appears | accounts file unchanged | quotas re-slice only when the file's quotas/accounts actually differ |

## Rollback (if something is broken)

```bash
sudo systemctl stop soultracker
# restore the previous code folder copy (or re-upload the old zip)
tar xzf ~/soul-backup-<stamp>.tar.gz -C $APP     # restores data/ exactly
sudo systemctl start soultracker
```

> Pro tip: keep the previous code as `$APP-old` (`cp -a $APP $APP-old` before
> Step 2) so rollback never depends on finding the old zip.