import os, zipfile
BASE = r"c:\Users\snikb\OneDrive\Desktop\soulteacker-maas"
OUT = os.path.join(BASE, "soultracker-cpanel.zip")
SKIP_DIRS = {"__pycache__", ".venv", ".git", "data"}
SKIP_FILES = {"soultracker-cpanel.zip", "soultracker-code.zip", "soultracker-cpanel-old.zip"}
SKIP_EXT = {".pyc", ".pyo", ".log", ".shm", ".wal", ".png"}
# bulky dev / local-only artefacts never needed on the host. data/ is skipped
# entirely: job state + uploads live on the server and must never be clobbered
# by a code update. test_quota_sync.py ships on purpose (verify on the host).
SKIP_NAMES = {"probe_site.py","probe_refresh.py","probe_refresh2.py","probe_nsan.py",
 "probe_longevity.py","probe_nsan.png","probe_kcsync.png","rebalance.py","requeue.py",
 "app_prev.py","_app_out.txt","nul","souls.xlsx","track_targets.xlsx",
 "build_zip.py","server_out.txt","souls_retry.csv","souls_dropped_review.csv"}
count = 0
with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
    for root, dirs, files in os.walk(BASE):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for f in files:
            if f in SKIP_FILES or f in SKIP_NAMES:
                continue
            if os.path.splitext(f)[1].lower() in SKIP_EXT:
                continue
            full = os.path.join(root, f)
            arc = os.path.relpath(full, BASE)
            z.write(full, arc)
            count += 1
print("entries:", count, "size:", os.path.getsize(OUT))
