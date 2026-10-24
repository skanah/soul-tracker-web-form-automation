import asyncio
import time

from playwright.async_api import BrowserContext, Page

from config import Config

# The site limits logins PER IP ADDRESS ("Too many login attempts from this
# network"). When any account sees that message, the limit applies to every
# account, so all logins - initial and session-recovery - pause until this
# shared cooldown expires instead of each account burning its retries into it.
NETWORK_LIMIT_MARKER = "too many login attempts from this network"
_network_cooldown_until = 0.0


def _network_remaining() -> float:
    return max(0.0, _network_cooldown_until - time.time())


def _trip_network_cooldown() -> None:
    global _network_cooldown_until
    _network_cooldown_until = time.time() + Config.NETWORK_LOGIN_COOLDOWN


class AccountSession:
    """Represents one logged-in SoulTracker account.

    The site (christembassysoultracker.org) is a Vue SPA: its login inputs
    carry only ``id=`` attributes (no ``name=``), so selectors here target
    ``input#email`` / ``input#password``.

    Auth model (probed 2026-10-01): login returns a 60-min JWT
    (``localStorage.token``) plus a rotating ``refresh_token``; the SPA renews
    both via ``POST /auth/refresh``. We refresh proactively so sessions never
    age into the site's ~30-min death window and full re-logins (which are
    rate-limited per IP) stay rare.
    """

    def __init__(self, account_id: str, email: str, password: str):
        self.account_id = account_id
        self.email = email
        self.password = password
        self.context: BrowserContext | None = None
        self.page: Page | None = None
        self.logged_in = False
        self.last_refresh_at = 0.0

    async def login(self, browser_pool, log=print):
        """Open /login and retry transient network/login failures.

        A fresh context is used for every attempt. Credential rejections and
        mandatory password changes stop immediately; navigation errors and a
        missing dashboard redirect use bounded exponential backoff.
        """
        last_failure = "login failed"
        for attempt in range(1, Config.LOGIN_MAX_ATTEMPTS + 1):
            # Respect the shared per-IP cooldown set by any account that hit
            # the site's "Too many login attempts from this network" limit.
            cool = _network_remaining()
            if cool > 0:
                log(
                    f"[{self.account_id}] Network login cooldown active - "
                    f"waiting {cool:.0f}s before attempting login..."
                )
                await asyncio.sleep(cool)
            if self.context:
                try:
                    await self.context.close()
                except Exception:
                    pass
                self.context = None
                self.page = None

            try:
                self.context = await browser_pool.new_context()
                self.page = await self.context.new_page()
                self.page.set_default_timeout(Config.NAVIGATION_TIMEOUT)
                log(
                    f"[{self.account_id}] Navigating to login "
                    f"(attempt {attempt}/{Config.LOGIN_MAX_ATTEMPTS})..."
                )
                await self.page.goto(
                    Config.SOULTRACKER_LOGIN_URL,
                    wait_until="domcontentloaded",
                    timeout=Config.LOGIN_NAVIGATION_TIMEOUT,
                )
                try:
                    await self.page.wait_for_selector("input#email", state="visible")
                except Exception as exc:
                    raise RuntimeError(
                        "the login page did not render an email field "
                        f"(page is at {self.page.url})"
                    ) from exc

                await self.page.fill("input#email", self.email)
                await self.page.fill("input#password", self.password)
                submit = self.page.locator('form button[type="submit"]')
                if not await submit.count():
                    submit = self.page.locator('button[type="submit"]')
                if not await submit.count():
                    raise RuntimeError(
                        f"login submit button not found (page is at {self.page.url})"
                    )
                try:
                    async with self.page.expect_response(
                        lambda response: (
                            "api.christembassysoultracker.org/login" in response.url
                            and response.request.method == "POST"
                        ),
                        timeout=Config.LOGIN_API_TIMEOUT,
                    ) as response_info:
                        await submit.first.click()
                    response = await response_info.value
                    payload = await response.json()
                except Exception as exc:
                    raise RuntimeError(
                        f"the login API did not return a response: {exc}"
                    ) from exc

                if response.ok and payload.get("status") is True and payload.get("token"):
                    user = payload.get("user") or {}
                    route = self._dashboard_route(user)
                    if "/login" in self.page.url:
                        await self.page.goto(
                            f"https://www.christembassysoultracker.org{route}",
                            wait_until="domcontentloaded",
                            timeout=Config.LOGIN_NAVIGATION_TIMEOUT,
                        )
                    self.logged_in = True
                    self.last_refresh_at = time.time()
                    log(f"[{self.account_id}] ✅ Logged in as {self.email} ({self.page.url})")
                    return True

                failure_type, failure = self._classify_login_response(
                    response.status, payload
                )
                last_failure = failure
                if NETWORK_LIMIT_MARKER in failure.lower():
                    _trip_network_cooldown()
                    log(
                        f"[{self.account_id}] ⛔ Site rate-limited logins from this "
                        f"network - pausing ALL logins for "
                        f"{Config.NETWORK_LOGIN_COOLDOWN}s."
                    )
                if failure_type == "permanent":
                    log(f"[{self.account_id}] ❌ Login failed: {failure}")
                    self.logged_in = False
                    return False
            except Exception as exc:
                last_failure = str(exc)
                log(f"[{self.account_id}] ⚠ Login attempt {attempt} failed: {exc}")

            self.logged_in = False
            if attempt < Config.LOGIN_MAX_ATTEMPTS:
                if NETWORK_LIMIT_MARKER in (last_failure or "").lower():
                    # The loop-top wait above handles the shared cooldown;
                    # a 5/10s backoff would only burn the next attempt into
                    # the same per-IP limit.
                    log(
                        f"[{self.account_id}] Retrying after the network "
                        f"cooldown (attempt {attempt + 1}/{Config.LOGIN_MAX_ATTEMPTS})..."
                    )
                    continue
                delay = Config.RETRY_BASE_WAIT * (2 ** (attempt - 1))
                log(
                    f"[{self.account_id}] Retrying login in {delay}s "
                    f"(attempt {attempt + 1}/{Config.LOGIN_MAX_ATTEMPTS})..."
                )
                await asyncio.sleep(delay)

        log(
            f"[{self.account_id}] ❌ Login failed after "
            f"{Config.LOGIN_MAX_ATTEMPTS} attempts: {last_failure}"
        )
        return False

    @staticmethod
    def _dashboard_route(user: dict) -> str:
        """Mirror the site's default route selection for its access levels."""
        access = str(user.get("access_level", user.get("access", "0"))).strip()
        routes = {
            "3": "/dashboard/org",
            "4": "/dashboard/church",
            "5": "/dashboard/org",
            "7": "/dashboard/admin",
            "8": "/dashboard/icl",
        }
        if access in routes:
            return routes[access]
        church_id = int(user.get("church_id") or 0)
        zone_id = int(user.get("zone_id") or 0)
        mc_id = int(user.get("mc_id") or 0)
        if access == "0" and (church_id == 0 or (zone_id == 0 and mc_id == 0)):
            return "/dashboard/ministry-profile?newUser=1"
        return "/dashboard/user"

    @staticmethod
    def _classify_login_response(status: int, payload: dict) -> tuple[str, str]:
        """Turn the login API response into retryable or permanent failure."""
        message = (
            payload.get("message")
            or payload.get("code")
            or "Login failed"
        ) if isinstance(payload, dict) else "Login failed"
        text = str(message).strip() or "Login failed"
        lower = text.lower()
        permanent_markers = (
            "invalid email",
            "invalid password",
            "incorrect password",
            "wrong password",
            "credentials",
            "account not found",
            "user not found",
            "does not exist",
            "account is locked",
            "unauthorized",
        )
        if status in (401, 403) or any(marker in lower for marker in permanent_markers):
            return "permanent", text
        return "transient", text

    async def _wait_for_dashboard(self, log=print) -> tuple[str | None, str | None]:
        """Poll after submitting the login form.

        Returns None once a /dashboard page is reached, otherwise a
        human-readable reason the login did not succeed.
        """
        page = self.page
        deadline = time.monotonic() + Config.LOGIN_DASHBOARD_TIMEOUT
        while time.monotonic() < deadline:
            url = page.url

            # Success: router moved into the dashboard (incl. role redirects
            # such as /dashboard/ministry-profile?newUser=1).
            if "/login" not in url and (
                "/dashboard/" in url or url.rstrip("/").endswith("/dashboard")
            ):
                return None, None

            # The account was flagged to rotate its password.
            if "/change-password" in url:
                return "permanent", (
                    "this account must change its password first - log in once via "
                    "https://www.christembassysoultracker.org/login, set a new "
                    "password, then press Resume."
                )

            # Credentials rejected -> red banner inside the login form.
            banner = page.locator('div[class*="bg-red-50"]')
            try:
                if await banner.count() and await banner.first.is_visible():
                    text = (await banner.first.inner_text()).strip()
                    if text:
                        permanent_markers = (
                            "invalid email",
                            "invalid password",
                            "incorrect password",
                            "wrong password",
                            "credentials",
                            "account not found",
                            "user not found",
                            "does not exist",
                            "account is locked",
                        )
                        failure_type = (
                            "permanent"
                            if any(marker in text.lower() for marker in permanent_markers)
                            else "transient"
                        )
                        return failure_type, text
            except Exception:
                pass

            # Mobile-app prompt modal can block the redirect to /dashboard;
            # clicking its backdrop dismisses it and continues the redirect.
            backdrop = page.locator('div[class*="bg-slate-900/50"]')
            try:
                if await backdrop.count() and await backdrop.first.is_visible():
                    log(f"[{self.account_id}] Dismissing mobile-app prompt...")
                    await backdrop.first.click()
                    await page.wait_for_timeout(500)
                    continue
            except Exception:
                pass

            await page.wait_for_timeout(250)

        return "transient", f"still on {page.url} after submitting the login form"

    async def refresh_session(self, log=print) -> bool:
        """Renew token + refresh_token via POST /auth/refresh (no re-login).

        Mirrors the SPA's own refreshAccessToken(): plain JSON body with the
        stored refresh_token; on success the rotated values are written back
        to localStorage so the next page load hydrates them. The refresh is
        NOT rate-limited like /login, so this keeps sessions alive without
        feeding the per-IP login limit.
        """
        if not self.page:
            return False
        self.last_refresh_at = time.time()  # never hammer, even on failure
        try:
            out = await self.page.evaluate(
                """() => fetch('https://api.christembassysoultracker.org/auth/refresh',
                       {method:'POST', headers:{'Content-Type':'application/json'},
                        body: JSON.stringify({refresh_token: localStorage.getItem('refresh_token')})})
                       .then(async r => ({http: r.status, body: await r.json()}))
                       .catch(e => ({http: 0, error: String(e)}))"""
            )
        except Exception as exc:
            log(f"[{self.account_id}] Session refresh failed: {exc}")
            return False
        body = (out or {}).get("body")
        if isinstance(body, dict) and body.get("status") and body.get("token"):
            await self.page.evaluate(
                """(t) => {
                     localStorage.setItem('token', t.token);
                     if (t.refresh) localStorage.setItem('refresh_token', t.refresh);
                   }""",
                {"token": body["token"], "refresh": body.get("refresh_token")},
            )
            log(f"[{self.account_id}] Session renewed (no re-login)")
            return True
        log(
            f"[{self.account_id}] Session refresh rejected "
            f"(http {(out or {}).get('http')}) - will re-login if needed"
        )
        return False

    async def close(self):
        if self.context:
            await self.context.close()
        self.logged_in = False
