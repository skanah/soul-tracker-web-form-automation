"""Fill the real SoulTracker "Add Soul" form (Vue SPA).

Field facts, verified against https://www.christembassysoultracker.org/dashboard/souls/add:

* No input carries ``id=``/``name=`` attributes - fields are addressed by
  their placeholder text or by label -> sibling relationships.
* ``Country`` is a required custom dropdown; ``State / Province`` stays
  disabled until a country is picked, and picking a country clears state +
  city - so country must be chosen first.
* Phone and email run a real server-side check on blur; the submit handler
  refuses to post unless both reached the "valid" state ("... is valid ✓").
* City and the consent checkbox are required.
* Success shows a "Soul added successfully!" toast and redirects (after
  ~1.5s) to ``/dashboard/souls``; failures show a red banner (or a red
  message under a field).
"""
import asyncio
import random
import re
import time

from config import Config

from .validator import is_valid_phone, is_valid_email

# The <option> labels of the Title <select> (the only native select on the page).
TITLE_LABELS = ("Brother", "Sister", "Mr", "Mrs", "Miss", "Dr", "Professor")

# Global cap on simultaneous phone/email validation calls, shared by every
# account in the run. The site's /validate-* API answers in ~3s solo but
# hangs past 60s under ~30+ concurrent checks (probed 2026-09-30), so all
# sessions queue for a few validation slots while everything else - page
# loads, form filling, submit - stays fully parallel. Created lazily inside
# the worker's event loop.
_validation_sem: "asyncio.Semaphore | None" = None


def _get_validation_sem() -> asyncio.Semaphore:
    global _validation_sem
    if _validation_sem is None:
        _validation_sem = asyncio.Semaphore(Config.MAX_CONCURRENT_VALIDATIONS)
    return _validation_sem


def _text(value) -> str:
    """Strip a CSV cell down to a usable string ('' when empty/NaN-ish)."""
    if value is None:
        return ""
    text = str(value).strip()
    if not text or text.lower() in ("nan", "none", "nat"):
        return ""
    return text


def normalize_phone(raw: str) -> str:
    """Format a phone number the way the Add Soul form expects it.

    The page builds the number as ``dialCode + localDigits`` when the value
    does not start with ``+``. A bare international number such as
    ``2347032634372`` would therefore get the dial code prepended twice, so
    we return either an explicit international form (``+234...``) or the
    local form (``0...``).
    """
    raw = _text(raw)
    if not raw:
        return ""
    digits = re.sub(r"\D", "", raw)
    if not digits:
        return ""
    if raw.startswith("+"):
        return "+" + digits
    if digits.startswith("0"):
        return digits
    return "+" + digits


def normalize_title(raw: str) -> str:
    """Map a CSV title onto one of the form's <option> labels ('' = skip).

    Raises ValueError for a non-empty title the form does not offer.
    """
    text = _text(raw)
    if not text:
        return ""
    labels = {label.lower(): label for label in TITLE_LABELS}
    match = labels.get(text.lower())
    if match is None:
        raise ValueError(
            f"Invalid title: {raw!r}. Use one of: {', '.join(TITLE_LABELS)} "
            "(or leave the cell empty)"
        )
    return match



class FormFiller:
    """Fills the SoulTracker form for one session given one soul row."""

    SEL = {
        "title": 'xpath=//label[normalize-space(.)="Title"]/following-sibling::select[1]',
        "first_name": 'input[placeholder="e.g. Paul"]',
        "last_name": 'input[placeholder="e.g. Silas"]',
        "gender_male": 'form input[type="radio"][value="Male"]',
        "gender_female": 'form input[type="radio"][value="Female"]',
        "country_trigger": (
            'xpath=//label[contains(normalize-space(.), "Country")]'
            "/following-sibling::button[1]"
        ),
        "country_search": 'input[placeholder="Search country or dial code…"]',
        "phone": 'input[type="tel"]',
        "email": 'input[type="email"]',
        "state_trigger": (
            'xpath=//label[contains(normalize-space(.), "State / Province")]'
            "/following-sibling::button[1]"
        ),
        "state_search": 'input[placeholder="Search state…"]',
        "city": 'input[placeholder="e.g. Lagos, London…"]',
        "consent": 'form input[type="checkbox"]',
        "submit": 'form button[type="submit"]',
    }

    # Post-submit signals. Every one of these is rendered conditionally - the
    # only static red elements on the page are text-red-400 asterisks, which
    # these selectors deliberately do not match.
    ERROR_SELECTOR = "div.bg-red-50.border-red-200, .text-red-600, .text-amber-600"
    SUCCESS_TEXT = "Soul added successfully"

    def __init__(self, session, log=print):
        self.session = session
        self.log = log

    async def fill(self, soul: dict) -> dict:
        page = self.session.page
        acc = self.session.account_id
        result = {
            "account": acc,
            "soul": soul.get("email") or soul.get("phone"),
            "stage": "add",
            "status": "failed",
            "error": None,
        }

        # Fail fast on rows the real form can never accept.
        city = _text(soul.get("city"))
        if not city:
            result["error"] = "the Add Soul form requires a city (add a city column)"
            self.log(f"[{acc}] ❌ {result['error']}")
            return result

        first = _text(soul.get("firstName"))
        last = _text(soul.get("lastName"))
        if not first or not last:
            result["error"] = "firstName and lastName are required"
            self.log(f"[{acc}] ❌ {result['error']}")
            return result

        gender = _text(soul.get("gender")).lower()
        if gender in ("male", "m"):
            gender_sel = self.SEL["gender_male"]
        elif gender in ("female", "f"):
            gender_sel = self.SEL["gender_female"]
        else:
            result["error"] = f"Invalid gender: {soul.get('gender')}"
            self.log(f"[{acc}] ❌ {result['error']}")
            return result

        phone = _text(soul.get("phone"))
        email = _text(soul.get("email"))
        if not phone and not email:
            result["error"] = "Either phone or email required"
            self.log(f"[{acc}] ❌ {result['error']}")
            return result

        try:
            self.log(f"[{acc}] Opening form...")
            last_url = ""
            for open_try in range(3):
                await page.goto(
                    Config.SOULTRACKER_FORM_URL, wait_until="domcontentloaded"
                )
                try:
                    await page.locator(self.SEL["first_name"]).wait_for(
                        state="visible"
                    )
                    break
                except Exception:
                    last_url = page.url
                    if "/login" in last_url:
                        raise RuntimeError(
                            "redirected to the login page - the session expired"
                        )
                    if "/kc-sync" in last_url:
                        # New accounts are forced through a "Connect Kingschat"
                        # interstitial; "Skip for now" dismisses it. Never burn
                        # a soul row on this.
                        self.log(f"[{acc}] Dismissing the KingsChat sync prompt...")
                        try:
                            await page.get_by_role(
                                "button", name="Skip for now"
                            ).first.click(timeout=5000)
                            await page.wait_for_timeout(1500)
                        except Exception:
                            pass
                        continue
                    if "ministry-profile" in last_url:
                        # Profile-completion redirect seen while the SPA is
                        # still booting; give the router a moment and retry.
                        self.log(
                            f"[{acc}] Caught on profile setup "
                            f"({last_url}); retrying the form..."
                        )
                        await page.wait_for_timeout(2000)
                        continue
                    # Any other URL: fall through to the retry below once,
                    # then give up with the page's address in the error.
                    if open_try < 2:
                        await page.wait_for_timeout(2000)
                        continue
                    raise RuntimeError(
                        f"the Add Soul form did not render (page is at {last_url})"
                    )
            else:
                raise RuntimeError(
                    f"the Add Soul form did not render (page is at {last_url})"
                )

            # ---------- Personal Details ----------
            title = normalize_title(soul.get("title"))
            if title:
                await page.select_option(self.SEL["title"], label=title)

            await page.fill(self.SEL["first_name"], first)
            await page.fill(self.SEL["last_name"], last)

            # ---------- Gender ----------
            await self._select_gender(page, gender_sel, gender)

            # ---------- Country (required; unlocks the State dropdown) ----------
            country = _text(soul.get("country")) or Config.SOULTRACKER_DEFAULT_COUNTRY
            await self._pick_country(page, country)

            # ---------- Contact + the site's own verification ----------
            pacing_started = time.monotonic()
            if phone:
                if not is_valid_phone(phone):
                    raise ValueError(f"Invalid phone: {phone}")
                self.log(
                    f"[{acc}] Phone submitted - waiting for validation "
                    f"(attempt 1/{Config.PHONE_VERIFICATION_ATTEMPTS})..."
                )
                await self._wait_phone_verified(page, phone, acc)
                # Keep the human pacing the run spec asks for (10-20s at this
                # step) - but only after the check actually passed, so bad
                # numbers fail fast instead of sleeping first.
                budget = random.uniform(
                    Config.PHONE_VALIDATION_MIN_WAIT, Config.PHONE_VALIDATION_MAX_WAIT
                )
                remaining = budget - (time.monotonic() - pacing_started)
                if remaining > 0:
                    await page.wait_for_timeout(int(remaining * 1000))

            if email:
                if not is_valid_email(email):
                    raise ValueError(f"Invalid email: {email}")
                async with _get_validation_sem():
                    await page.fill(self.SEL["email"], email)
                    await page.locator(self.SEL["email"]).blur()
                    self.log(f"[{acc}] Email submitted - waiting for validation...")
                    await self._wait_verified(page, "Email address is valid", "email")

            # ---------- State / City (the site clears these when country changes) ----------
            state = _text(soul.get("state"))
            if state:
                await self._pick_state(page, state)

            await self._fill_city(page, city)

            # ---------- Consent ----------
            await page.check(self.SEL["consent"])

            # ---------- Submit ----------
            self.log(f"[{acc}] Submitting form...")
            await self._submit_and_confirm(page)

            result["status"] = "success"
            self.log(f"[{acc}] ✅ Saved soul for {first} {last}")

        except Exception as e:
            result["error"] = str(e)
            self.log(f"[{acc}] ❌ {e}")

        return result

    # ------------------------- custom dropdowns -------------------------

    async def _select_gender(self, page, selector: str, gender: str):
        """Select a radio whose input is visually hidden by its styled label.

        Playwright's normal ``check()`` performs a hit test against the tiny
        ``sr-only`` input and the sibling/ancestor label intercepts the click.
        Click the associated label (the site's intended interaction), then
        require the real input's checked state before moving on.
        """
        radio = page.locator(selector)
        if not await radio.count():
            raise RuntimeError(f"gender option {gender!r} was not found on the form")

        if not await radio.first.is_checked():
            # Vue usually wraps the input and its text in the label. Support the
            # site's markup and a detached label as a fallback.
            label = radio.first.locator("xpath=ancestor-or-self::label[1]")
            if await label.count():
                await label.first.click()
            else:
                label = radio.first.locator("xpath=following::label[1]")
                if await label.count():
                    await label.first.click()
                else:
                    # Bypasses only hit testing, not the browser's radio event.
                    await radio.first.check(force=True)

            try:
                await radio.first.wait_for(state="attached", timeout=3000)
                for _ in range(20):
                    if await radio.first.is_checked():
                        return
                    await page.wait_for_timeout(100)
            except Exception as exc:
                raise RuntimeError(
                    f"could not confirm the {gender!r} gender option was selected"
                ) from exc
            raise RuntimeError(
                f"the {gender!r} gender option was clicked but remained unchecked"
            )

    async def _open_picker(self, page, trigger_sel: str, search_sel: str, label: str):
        """Open a custom dropdown, retrying while its data is still loading.

        The State trigger silently ignores clicks while the state list of the
        just-selected country is still being fetched, so one click is not
        enough - we poll until the search box shows up.
        """
        deadline = time.monotonic() + Config.LOCATION_LOAD_TIMEOUT
        search = page.locator(search_sel)
        while time.monotonic() < deadline:
            if await search.count() and await search.first.is_visible():
                return
            trigger = page.locator(trigger_sel).first
            if (
                await trigger.count()
                and await trigger.is_visible()
                and await trigger.is_enabled()
            ):
                try:
                    # Keep each attempt short: while API data is loading, Vue may
                    # temporarily rebuild the trigger and intercept the click.
                    await trigger.click(timeout=3000)
                except Exception:
                    pass
            await page.wait_for_timeout(400)
        raise TimeoutError(f"the {label} dropdown did not open")

    async def _pick_option(self, page, search_sel: str, value: str, label: str):
        """Type ``value`` into the open dropdown's search box and click it.

        The option's exact text wins; otherwise the deepest option *inside
        the open dropdown* that starts with ``value`` is used - the state
        list is API-provided and may spell names differently (CSV "Rivers"
        vs site "Rivers State").
        """
        search = page.locator(search_sel)
        await search.first.wait_for(state="visible", timeout=5000)

        # The dropdown root is the ancestor holding both the search box and
        # its option list - scopes the fuzzy fallback away from the page.
        container = None
        match = re.search(r'placeholder="([^"]+)"', search_sel)
        if match:
            container = page.locator(
                f'xpath=//input[@placeholder="{match.group(1)}"]/ancestor::div[2]'
            )

        deadline = time.monotonic() + Config.LOCATION_OPTION_TIMEOUT
        while time.monotonic() < deadline:
            await search.first.fill(value)

            picked = None
            exact = page.get_by_text(value, exact=True)
            if await exact.count() and await exact.first.is_visible():
                picked = exact.first

            if picked is None and container is not None and await container.count():
                fuzzy = container.get_by_text(value, exact=False)
                # Reverse DOM order: the deepest match (the actual option,
                # not an ancestor wrapping the whole list) comes last.
                for i in range(await fuzzy.count() - 1, -1, -1):
                    cand = fuzzy.nth(i)
                    try:
                        text = (await cand.inner_text()).strip()
                    except Exception:
                        continue
                    if text.lower().startswith(value.lower()):
                        picked = cand
                        break

            if picked is not None:
                await picked.click()
                try:
                    # A successful pick closes the dropdown (search box hides).
                    await search.first.wait_for(state="hidden", timeout=3000)
                    return
                except Exception:
                    pass  # click missed - the dropdown stayed open, retry
            await page.wait_for_timeout(300)

        hint = ""
        try:
            if container is not None and await container.count():
                shown = (await container.first.inner_text()).strip()[:150]
                if shown:
                    hint = f" (dropdown shows: {shown!r})"
        except Exception:
            pass
        raise ValueError(f"{label}: {value!r} is not offered by the site{hint}")

    async def _pick_country(self, page, country: str):
        await self._open_picker(
            page, self.SEL["country_trigger"], self.SEL["country_search"], "Country"
        )
        await self._pick_option(page, self.SEL["country_search"], country, "country")
        # Proof the pick landed: only a selected country enables State.
        try:
            trigger = page.locator(self.SEL["state_trigger"])
            await trigger.first.wait_for(state="visible", timeout=3000)
            if not await trigger.first.is_enabled():
                raise ValueError(f"country {country!r} was not accepted by the form")
        except ValueError:
            raise
        except Exception:
            pass  # best-effort check; the submit step verifies it anyway

    async def _wait_state_selected(self, page, state: str):
        """Wait until the State trigger contains the chosen option."""
        trigger = page.locator(self.SEL["state_trigger"]).first
        expected = state.lower()
        deadline = time.monotonic() + Config.LOCATION_LOAD_TIMEOUT
        while time.monotonic() < deadline:
            try:
                text = (await trigger.inner_text()).strip().lower()
                if expected in text or text.startswith(expected):
                    return
            except Exception:
                pass
            await page.wait_for_timeout(250)
        raise TimeoutError(f"State {state!r} was not confirmed by the form")

    async def _fill_city(self, page, city: str):
        """Wait for City to become usable and survive any location re-render."""
        field = page.locator(self.SEL["city"]).first
        deadline = time.monotonic() + Config.LOCATION_LOAD_TIMEOUT
        while time.monotonic() < deadline:
            try:
                await field.wait_for(state="visible", timeout=1000)
                if not await field.is_enabled() or not await field.is_editable():
                    await page.wait_for_timeout(250)
                    continue
                await field.fill(city)
                await page.wait_for_timeout(300)
                if (await field.input_value()).strip() == city:
                    return
            except Exception:
                pass
            await page.wait_for_timeout(250)
        raise TimeoutError(f"City {city!r} did not become ready for entry")

    async def _pick_state(self, page, state: str):
        await self._open_picker(
            page, self.SEL["state_trigger"], self.SEL["state_search"], "State"
        )
        await self._pick_option(page, self.SEL["state_search"], state, "state")
        await self._wait_state_selected(page, state)

    # ------------------------- verification / submit -------------------------

    async def _wait_phone_verified(self, page, phone: str, acc: str):
        """Retry only verification timeouts; site rejections fail immediately."""
        attempts = Config.PHONE_VERIFICATION_ATTEMPTS
        phone_field = page.locator(self.SEL["phone"])
        for attempt in range(1, attempts + 1):
            # Queue for a validation slot; the backoff sleep below releases
            # the slot so other accounts are not blocked while we wait.
            async with _get_validation_sem():
                await phone_field.fill(normalize_phone(phone))
                await phone_field.blur()
                try:
                    await self._wait_verified(page, "Phone number is valid", "phone")
                    return
                except TimeoutError:
                    if attempt >= attempts:
                        raise
            delay = Config.RETRY_BASE_WAIT * (2 ** (attempt - 1))
            self.log(
                f"[{acc}] ⚠ Phone verification timed out on attempt "
                f"{attempt}/{attempts}; retrying in {delay}s..."
            )
            await asyncio.sleep(delay)
            self.log(
                f"[{acc}] Phone submitted - waiting for validation "
                f"(attempt {attempt + 1}/{attempts})..."
            )

    async def _wait_verified(self, page, ok_text: str, label: str):
        """Wait until the site's async check renders ``ok_text`` ("... ✓").

        Raises with the page's own message when the value is rejected
        (invalid, duplicate, verification service down, ...).
        """
        ok = page.get_by_text(ok_text, exact=False)
        bad = page.locator(".text-red-600, .text-amber-600")
        deadline = time.monotonic() + Config.VERIFICATION_TIMEOUT
        while time.monotonic() < deadline:
            if await ok.count() and await ok.first.is_visible():
                return
            if await bad.count():
                for i in range(await bad.count()):
                    try:
                        text = (await bad.nth(i).inner_text()).strip()
                    except Exception:
                        continue
                    if text:
                        raise ValueError(f"{label} verification failed: {text}")
            await page.wait_for_timeout(250)
        raise TimeoutError(
            f"{label} verification did not finish within "
            f"{Config.VERIFICATION_TIMEOUT}s"
        )

    async def _submit_and_confirm(self, page):
        """Submit through the Vue app's API and confirm the server accepted it."""
        submit = page.locator(self.SEL["submit"])
        if not await submit.count():
            submit = page.locator('button[type="submit"]')
        if not await submit.count():
            raise RuntimeError(f"submit button not found (page is at {page.url})")

        def is_soul_response(response) -> bool:
            url = response.url
            return (
                "api.christembassysoultracker.org/soul" in url
                and "/validate-" not in url
                and "/challenge" not in url
                and response.request.method == "POST"
            )

        try:
            async with page.expect_response(
                is_soul_response, timeout=Config.SOUL_SUBMIT_TIMEOUT
            ) as response_info:
                await submit.first.click()
            response = await response_info.value
            payload = await response.json()
        except Exception as exc:
            # Keep the older DOM signals as a fallback for a non-JSON response.
            payload = None
            response = None
            submit_error = exc

        if response is not None and isinstance(payload, dict):
            if response.ok and payload.get("status") is True:
                try:
                    await page.wait_for_url("**/dashboard/souls", timeout=8000)
                except Exception:
                    pass
                return
            message = None
            if isinstance(payload, dict):
                message = payload.get("message")
                messages = payload.get("messages")
                if not message and isinstance(messages, dict):
                    message = messages.get("error")
                if not message and isinstance(messages, str):
                    message = messages
            if message:
                raise ValueError(str(message))
            raise ValueError(
                f"the Add Soul API rejected the submission "
                f"(HTTP {response.status} at {page.url})"
            )

        # Fallback: some proxies/API modes return a non-JSON response, so check
        # for the page's own success/error signals before giving up.
        success = page.get_by_text(self.SUCCESS_TEXT, exact=False)
        errors = page.locator(self.ERROR_SELECTOR)
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if page.url.rstrip("/").endswith("/dashboard/souls"):
                return
            if await success.count() and await success.first.is_visible():
                return
            if await errors.count():
                for i in range(await errors.count()):
                    try:
                        text = (await errors.nth(i).inner_text()).strip()
                    except Exception:
                        continue
                    if text:
                        raise ValueError(text)
            await page.wait_for_timeout(250)
        raise TimeoutError(
            f"no confirmation after submitting the form (page is at {page.url}; "
            f"API error: {submit_error})"
        )

