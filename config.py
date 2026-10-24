import os
import secrets


def _resolve_admin_password() -> str:
    """Dashboard admin password for the built-in login screen.

    Set ADMIN_PASSWORD in the environment to choose it explicitly. Without
    the env var a strong random password is generated on first boot, saved
    to data/admin_password.txt and printed to the log - so the dashboard is
    never left without a password and never runs on a guessable default.
    """
    env_pw = os.getenv("ADMIN_PASSWORD", "").strip()
    if env_pw:
        return env_pw
    path = os.path.join("data", "admin_password.txt")
    try:
        with open(path, encoding="utf-8") as fh:
            saved = fh.read().strip()
        if saved:
            return saved
    except OSError:
        pass
    generated = secrets.token_urlsafe(12)
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(generated + "\n")
    except OSError:
        pass
    print(
        f"[auth] ADMIN_PASSWORD is not set - generated a dashboard password, "
        f"saved to {path}: {generated}"
    )
    return generated


class Config:
    SECRET_KEY = os.getenv("SECRET_KEY", "change-me-in-prod")
    ADMIN_PASSWORD = _resolve_admin_password()  # dashboard login (see above)
    UPLOAD_FOLDER = "data"
    MAX_CONTENT_LENGTH = 50 * 1024 * 1024  # 50MB
    
    # Form target (SoulTracker runs on christembassysoultracker.org)
    SOULTRACKER_FORM_URL = "https://www.christembassysoultracker.org/dashboard/souls/add"
    SOULTRACKER_LOGIN_URL = "https://www.christembassysoultracker.org/login"

    # The Add Soul form requires a Country before it allows State / phone
    # validation. Used when the souls file has no country column.
    SOULTRACKER_DEFAULT_COUNTRY = "Nigeria"

    # Track Souls page (souls are tracked after they have been added).
    # The automation normally reaches this page by clicking the "Track Souls"
    # nav item after login; the URL below is only the fallback, so adjust it if
    # your route differs.
    SOULTRACKER_TRACK_URL = "https://www.christembassysoultracker.org/dashboard/track-souls"
    TRACK_NAV_TEXT = "Track Souls"      # nav item / link text
    TRACK_PAGE_TITLE = "Track Souls"    # page heading, used to confirm we arrived
    TRACK_STATUS_FILTER = "All"       # status pill to activate: All|Saved|Pending|Won
    TRACK_NAV_TIMEOUT = 10000           # ms - nav link lookup
    TRACK_PANEL_TIMEOUT = 15000         # ms - drawer (pop-up form) open / close
    TRACK_SAVE_TIMEOUT = 20000          # ms - wait for the drawer to close after Save
    TRACK_SEARCH_WAIT = 800             # ms - settle time after typing in the search box
    
    # Automation tuning
    MAX_CONCURRENT_ACCOUNTS = int(os.getenv("MAX_CONCURRENT_ACCOUNTS", 100))  # Parallel browser contexts
    MAX_CONCURRENT_LOGINS = int(os.getenv("MAX_CONCURRENT_LOGINS", 3))  # Simultaneous browser logins (per-IP limit on the site)
    LOGIN_STAGGER_SECONDS = float(os.getenv("LOGIN_STAGGER_SECONDS", 3))  # Spacing between login starts (site enforces a per-IP login limit)
    NETWORK_LOGIN_COOLDOWN = int(os.getenv("NETWORK_LOGIN_COOLDOWN", 300))  # Seconds ALL logins pause after "Too many login attempts from this network"
    SESSION_RETRY_LIMIT = int(os.getenv("SESSION_RETRY_LIMIT", 2))  # Re-logins when a session dies mid-row
    SESSION_REFRESH_MINUTES = int(os.getenv("SESSION_REFRESH_MINUTES", 20))  # Proactively renew tokens every N minutes (avoids rate-limited re-logins)
    RENDER_FAILURE_LIMIT = int(os.getenv("RENDER_FAILURE_LIMIT", 3))  # Consecutive form-render failures before an account is paused instead of burning rows
    # Global cap on simultaneous phone/email validation calls, shared by all
    # accounts. Probed 2026-09-30: the site's /validate-* API answers in ~3s
    # solo but hangs past 60s under ~30+ concurrent checks, burning rows.
    MAX_CONCURRENT_VALIDATIONS = int(os.getenv("MAX_CONCURRENT_VALIDATIONS", 10))
    PHONE_VALIDATION_MIN_WAIT = 10   # seconds - pacing budget per soul (phone step)
    PHONE_VALIDATION_MAX_WAIT = 20
    VERIFICATION_TIMEOUT = 120       # seconds - each phone/email verification attempt (site degrades under load; 120s gives slow validations room instead of burning the row)
    PHONE_VERIFICATION_ATTEMPTS = 3  # total attempts per phone (timeouts only)
    RETRY_BASE_WAIT = 5              # seconds - exponential retry backoff base
    LOCATION_LOAD_TIMEOUT = 30       # seconds - wait for country/state dropdown data
    LOCATION_OPTION_TIMEOUT = 20     # seconds - wait for filtered state/city options
    LOGIN_MAX_ATTEMPTS = 3           # total login attempts per account
    LOGIN_NAVIGATION_TIMEOUT = 60000 # ms - slow initial login-page request
    LOGIN_DASHBOARD_TIMEOUT = 30     # seconds - wait for post-submit redirect
    LOGIN_API_TIMEOUT = 30000        # ms - wait for the site's login API response
    SOUL_SUBMIT_TIMEOUT = 60000      # ms - wait for the Add Soul API response (site degrades under load)
    SESSION_TIMEOUT = 300            # 5 min
    NAVIGATION_TIMEOUT = 30000       # ms
    HEADLESS = True                  # Set False for debugging

    # Large runs (1M-row jobs): assignment + progress tracking
    MAX_CONTENT_LENGTH = 600 * 1024 * 1024   # 600MB - a 1M-row CSV is ~150MB
    JOB_DB = "data/job.db"                   # SQLite job store (resume state)
    BATCH_SIZE = 25                          # rows claimed per account at once
    PROGRESS_EMIT_MS = 1000                  # throttle for UI progress events
    LOG_EVERY = 1000                         # aggregate success log every N souls/account
    PACE_BETWEEN_SOULS = 1                   # seconds between souls (rate-limit lever)
    LOGIN_RETRY_ATTEMPTS = int(os.getenv("LOGIN_RETRY_ATTEMPTS", 2))  # extra tries when a login fails (site flaps / bad network)
    LOGIN_RETRY_COOLDOWN = float(os.getenv("LOGIN_RETRY_COOLDOWN", 120))  # seconds between those login retries