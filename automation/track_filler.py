"""Track Souls automation.

Souls are tracked *after* they have been added. On the "Track Souls" page every
soul card carries a ``Track`` button; clicking it opens a right-hand drawer (the
pop-up form) holding:

* ``Church Service`` <select>     -> how many church services were attended
* ``Foundation School`` <select>  -> Class 1 .. Class 7 / Completed
* ``KingsChat ID`` text input
* ``Phone`` (tel) and ``Email`` inputs
* a single ``Save Changes`` button (it lives *outside* the <form> element)

This module maps the tracking values found in the souls file (csv/xlsx) to those
option labels, locates the right card for a soul, fills the drawer and saves it.
"""

import re
import time

from config import Config

from .validator import is_valid_email, is_valid_phone

# ---------------------------------------------------------------------------
# Option labels of the drawer's <select> elements.
# The <option value="N"> codes are 1-based, 0 being the "-- Select --" /
# "Not yet started" placeholder, which means "leave the field untouched".
# ---------------------------------------------------------------------------

CHURCH_SERVICE_LABELS = {
    1: "Not attended any church service yet",
    2: "Attended 1 church service",
    3: "Attended 2 church services",
    4: "Attended 3 church services",
    5: "Attended 4 church services",
}

FOUNDATION_CLASS_LABELS = {
    1: "Class 1",
    2: "Class 2",
    3: "Class 3",
    4: "Class 4",
    5: "Class 5",
    6: "Class 6",
    7: "Class 7",
    8: "Completed",
}

# Souls-file column aliases (compared case / space / underscore-insensitively).
FIELD_ALIASES = {
    "church_service": (
        "churchservice",
        "church",
        "churchattendance",
        "churchservicesattended",
        "serviceattended",
        "servicesattended",
    ),
    "foundation_class": (
        "foundationclass",
        "foundationschool",
        "foundationschoolclass",
        "class",
        "classname",
        "classno",
        "classnumber",
    ),
    "kingschat_id": ("kingschatid", "kingschat", "kingschatusername"),
}


# ---------------------------------------------------------------------------
# Pure helpers (no browser required -> easy to test)
# ---------------------------------------------------------------------------

def _key(value) -> str:
    """Normalise a column name or label for comparison."""
    return re.sub(r"[^a-z0-9]", "", str(value).lower())


def _text(value) -> str | None:
    """Return a stripped string, or None for empty/NaN-ish values."""
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in ("nan", "none", "nat"):
        return None
    return text


def _as_code(text: str) -> int | None:
    """Interpret a plain number ("3", 3, "3.0") as an option value."""
    if re.fullmatch(r"\d+", text):
        return int(text)
    if re.fullmatch(r"\d+\.0+", text):
        return int(float(text))
    return None


def _range_error(field: str, code: int, labels: dict) -> str:
    return (
        f"{field}: value {code} is out of range. Use 0 (leave unchanged) or "
        f"1-{max(labels)}, or one of the labels: {sorted(labels.values())}"
    )


def get_field(row: dict, field: str):
    """Return ``row``'s value for ``field``, tolerating column-name variants."""
    if not row:
        return None
    keys = {_key(column): column for column in row.keys()}
    for alias in FIELD_ALIASES[field]:
        if alias in keys:
            return _text(row[keys[alias]])
    return None


def normalize_church_service(value) -> str | None:
    """Map a file value to the "Church Service" option label (None = untouched).

    Accepts the option code (1-5), the exact option label, "not attended" or
    "attended N church services".
    """
    text = _text(value)
    if text is None:
        return None

    code = _as_code(text)
    if code is not None:
        if code == 0:
            return None
        if code in CHURCH_SERVICE_LABELS:
            return CHURCH_SERVICE_LABELS[code]
        raise ValueError(_range_error("churchService", code, CHURCH_SERVICE_LABELS))

    key = _key(text)
    for label in CHURCH_SERVICE_LABELS.values():
        if _key(label) == key:
            return label
    for synonym in ("notattended", "neverattended", "none", "nil", "noservice"):
        if synonym == key:
            return CHURCH_SERVICE_LABELS[1]

    match = re.search(r"(\d+)\s*(?:church\s*)?services?", text, re.IGNORECASE)
    if match and "attend" in key:
        attended = int(match.group(1))
        if attended + 1 in CHURCH_SERVICE_LABELS:
            return CHURCH_SERVICE_LABELS[attended + 1]

    raise ValueError(
        f"churchService: cannot interpret {text!r}. Use 0-{max(CHURCH_SERVICE_LABELS)} "
        f"or one of {sorted(CHURCH_SERVICE_LABELS.values())}"
    )


def normalize_foundation_class(value) -> str | None:
    """Map a file value to the "Foundation School" option label (None = untouched).

    Accepts the option code (1-8), the exact label ("Class 3", "Completed") or
    synonyms such as "graduated".
    """
    text = _text(value)
    if text is None:
        return None

    code = _as_code(text)
    if code is not None:
        if code == 0:
            return None
        if code in FOUNDATION_CLASS_LABELS:
            return FOUNDATION_CLASS_LABELS[code]
        raise ValueError(_range_error("foundationClass", code, FOUNDATION_CLASS_LABELS))

    key = _key(text)
    for label in FOUNDATION_CLASS_LABELS.values():
        if _key(label) == key:
            return label
    for synonym in ("notyetstarted", "notstarted", "none", "nil", "new"):
        if synonym == key:
            return None
    for synonym in ("completed", "complete", "done", "finished", "graduated", "graduation"):
        if synonym == key:
            return FOUNDATION_CLASS_LABELS[8]

    match = re.search(r"class\s*(\d+)", text, re.IGNORECASE)
    if match:
        number = int(match.group(1))
        if number in FOUNDATION_CLASS_LABELS:
            return FOUNDATION_CLASS_LABELS[number]

    raise ValueError(
        f"foundationClass: cannot interpret {text!r}. Use 0-{max(FOUNDATION_CLASS_LABELS)} "
        f"or one of {sorted(FOUNDATION_CLASS_LABELS.values())}"
    )


def extract_tracking(soul: dict) -> dict:
    """Normalise the tracking values of one souls-file row.

    Raises ValueError when a present value cannot be mapped to a drawer option.
    """
    kingschat = get_field(soul, "kingschat_id")
    if kingschat:
        kingschat = "@" + kingschat.lstrip("@").strip()

    phone = _text(soul.get("phone"))
    email = _text(soul.get("email"))

    return {
        "church_service": normalize_church_service(get_field(soul, "church_service")),
        "foundation_class": normalize_foundation_class(get_field(soul, "foundation_class")),
        "kingschat_id": kingschat,
        "phone": phone if is_valid_phone(phone) else None,
        "email": email if is_valid_email(email) else None,
    }


def has_tracking_data(soul: dict) -> bool:
    """True when the row carries anything the Track drawer can set."""
    return any(get_field(soul, field) for field in ("church_service", "foundation_class", "kingschat_id"))


def tracking_columns(columns) -> list:
    """Names of ``columns`` recognised as Track drawer inputs."""
    known = {alias for aliases in FIELD_ALIASES.values() for alias in aliases}
    if columns is None:
        return []
    return [str(column) for column in columns if _key(column) in known]


# ---------------------------------------------------------------------------
# Card matching (pure)
# ---------------------------------------------------------------------------

_NAME_JUNK = re.compile(r"[^a-z0-9\s]+")
_EMAIL_RE = re.compile(r"[^\s@]+@[^\s@]+\.[^\s@]+")
_PHONE_RE = re.compile(r"\+?\d[\d\s()\-]{6,}\d")


def soul_name(soul: dict) -> str:
    """Full name of a soul row, e.g. "Olu Sheun"."""
    raw = f"{soul.get('firstName', '')} {soul.get('lastName', '')}"
    return re.sub(r"\s+", " ", raw).strip()


def soul_display(soul: dict) -> str:
    """Human label used in logs and result rows."""
    return soul_name(soul) or _text(soul.get("email")) or _text(soul.get("phone")) or "unknown soul"


def _name_key(value) -> str:
    return re.sub(r"\s+", " ", _NAME_JUNK.sub(" ", str(value or "").lower())).strip()


def _digits(value) -> str:
    return re.sub(r"\D", "", str(value or ""))


def match_soul(soul: dict, card_name: str, card_text: str) -> str | None:
    """Return how ``soul`` matches a card: 'name', 'phone', 'email' or None."""
    name = soul_name(soul)
    if name and _name_key(name) == _name_key(card_name):
        return "name"

    phone = _digits(soul.get("phone"))
    if len(phone) >= 7:
        tail = phone[-10:]
        for candidate in _PHONE_RE.findall(card_text or ""):
            digits = _digits(candidate)
            if len(digits) >= 7 and digits[-10:] == tail:
                return "phone"

    email = (_text(soul.get("email")) or "").lower()
    if email:
        for found in _EMAIL_RE.findall(card_text or ""):
            if found.lower() == email:
                return "email"
    return None


def _summarize(applied: dict) -> str:
    return ", ".join(f"{key}={value}" for key, value in applied.items()) or "nothing"


# ---------------------------------------------------------------------------
# Browser automation
# ---------------------------------------------------------------------------

class TrackFiller:
    """Finds one soul on the Track Souls page and saves its tracking drawer.

    Usage::

        tracker = TrackFiller(session, log=push_log)
        await tracker.open()                 # once per session
        result = await tracker.track(soul)   # once per soul row
    """

    TRACK_BUTTON = "Track"
    SAVE_BUTTON = "Save Changes"
    SEARCH_INPUT = 'input[placeholder^="Search by name"]'
    KINGSCHAT_INPUT = 'input[placeholder="@username"]'
    PHONE_INPUT = 'input[type="tel"]'
    EMAIL_INPUT = 'input[type="email"]'
    # The drawer is the fixed element holding the "Save Changes" button; the
    # Tailwind class list is only used as a secondary, structural fallback.
    PANEL_CANDIDATES = (
        'div.fixed:has(button:text-is("Save Changes"))',
        "div.fixed.z-50",
    )

    def __init__(self, session, log=print):
        self.session = session
        self.log = log

    # ---------------------------- session guard ----------------------------
    def _raise_if_logged_out(self) -> None:
        """Raise the "session expired" error that ``_run_stage``'s re-login
        retry matches, when the page has bounced to the login screen.

        Without this a dead session burned track rows as "failed" instead of
        re-logging in and retrying (the add stage already detects it).
        """
        url = (getattr(self.session.page, "url", "") or "").lower()
        if "/login" in url:
            raise RuntimeError("redirected to the login page - the session expired")

    # ---------------------------- navigation ----------------------------
    async def open(self):
        """Open the Track Souls page (idempotent) and wait for the soul cards."""
        page = self.session.page
        if not await self._click_nav():
            self.log(
                f"[track] Nav item {Config.TRACK_NAV_TEXT!r} not found - opening "
                f"{Config.SOULTRACKER_TRACK_URL}"
            )
            await page.goto(
                Config.SOULTRACKER_TRACK_URL,
                wait_until="domcontentloaded",
                timeout=Config.NAVIGATION_TIMEOUT,
            )

        self._raise_if_logged_out()
        try:
            await page.get_by_role("heading", name=Config.TRACK_PAGE_TITLE).first.wait_for(
                state="visible", timeout=Config.TRACK_PANEL_TIMEOUT
            )
        except Exception:
            self._raise_if_logged_out()  # the SPA may redirect to /login late
            raise

        pill = page.get_by_role("button", name=Config.TRACK_STATUS_FILTER, exact=True)
        try:
            if await pill.count():
                await pill.first.click(timeout=5000)
        except Exception as exc:
            self.log(
                f"[track] Could not activate the {Config.TRACK_STATUS_FILTER!r} filter: {exc}"
            )

        await self._cards().first.wait_for(state="visible", timeout=Config.TRACK_PANEL_TIMEOUT)

    async def _click_nav(self) -> bool:
        page = self.session.page
        candidates = (
            page.get_by_role("link", name=Config.TRACK_NAV_TEXT, exact=True),
            page.locator(f'a:has-text("{Config.TRACK_NAV_TEXT}")'),
            page.locator(f'button:has-text("{Config.TRACK_NAV_TEXT}")'),
        )
        for candidate in candidates:
            try:
                if not await candidate.count():
                    continue
                await candidate.first.click(timeout=Config.TRACK_NAV_TIMEOUT)
                try:
                    await page.wait_for_load_state("networkidle", timeout=5000)
                except Exception:
                    pass
                return True
            except Exception:
                continue
        return False

    def _cards(self):
        """Every soul card: the elements carrying a Track button."""
        page = self.session.page
        return page.locator("div.group").filter(
            has=page.get_by_role("button", name=self.TRACK_BUTTON, exact=True)
        )

    async def _settle(self):
        try:
            await self.session.page.wait_for_load_state("networkidle", timeout=3000)
        except Exception:
            pass
        await self.session.page.wait_for_timeout(Config.TRACK_SEARCH_WAIT)

    async def _card_name(self, card) -> str:
        """The soul name rendered on a card (its <h3>)."""
        try:
            if await card.locator("h3").count():
                return (await card.locator("h3").first.inner_text()).strip()
        except Exception:
            pass
        return ""

    async def _match_visible_cards(self, soul: dict):
        """``(card, reason)`` for the first matching card, else None.

        ``reason`` is "name", "phone" or "email" - matching by phone/email is
        normal when the souls file spells a name differently from the dashboard.
        """
        for card in await self._cards().all():
            try:
                if not await card.is_visible():
                    continue
                name = await self._card_name(card)
                text = await card.inner_text()
            except Exception:
                continue
            reason = match_soul(soul, name, text)
            if reason:
                return card, reason
        return None

    async def _clear_search(self):
        search = self.session.page.locator(self.SEARCH_INPUT)
        if await search.count():
            try:
                await search.first.fill("")
            except Exception:
                pass
            await self.session.page.wait_for_timeout(Config.TRACK_SEARCH_WAIT)

    async def _try_query(self, soul: dict, query: str):
        """Search for one query and wait briefly for a matching card."""
        if not query:
            return None
        page = self.session.page
        search = page.locator(self.SEARCH_INPUT)
        if not await search.count():
            return None
        try:
            await search.first.fill(query)
        except Exception:
            return None

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            found = await self._match_visible_cards(soul)
            if found is not None:
                return found
            # A query that returns no cards at all is usually a server-side
            # miss; give it a moment, then stop waiting for that variant.
            try:
                if await self._cards().count() == 0 and time.monotonic() > deadline - 3.5:
                    break
            except Exception:
                pass
            await page.wait_for_timeout(250)
        return None

    async def _find_card(self, soul: dict):
        """Locate the soul's card, trying several query forms before scanning."""
        page = self.session.page
        search = page.locator(self.SEARCH_INPUT)
        if not await search.count():
            return await self._match_visible_cards(soul)

        name = soul_name(soul)
        phone = _text(soul.get("phone"))
        phone_digits = _digits(phone)
        email = _text(soul.get("email"))
        queries = []
        for query in (
            phone,
            phone_digits[-10:] if len(phone_digits) >= 10 else "",
            name,
            email,
        ):
            if query and query not in queries:
                queries.append(query)

        for query in queries:
            found = await self._try_query(soul, query)
            if found is not None:
                return found
            await self._clear_search()

        # Finally clear the search and scan every loaded card once more.
        await self._clear_search()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            found = await self._match_visible_cards(soul)
            if found is not None:
                return found
            await page.wait_for_timeout(250)
        return None

    # ------------------------- the tracking flow -------------------------
    async def track(self, soul: dict) -> dict:
        """Track one soul: find its card, fill the drawer, click Save Changes."""
        account = self.session.account_id
        label = soul_display(soul)
        result = {
            "account": account,
            "soul": label,
            "stage": "track",
            "status": "failed",
            "error": None,
            "applied": {},
            "matchedBy": None,
            "card": {},
        }

        try:
            values = extract_tracking(soul)
        except ValueError as exc:
            result["error"] = str(exc)
            self.log(f"[{account}] ❌ Track {label}: {exc}")
            return result

        if not any(values[key] for key in ("church_service", "foundation_class", "kingschat_id")):
            result["status"] = "skipped"
            result["error"] = (
                "no tracking values for this row "
                "(churchService / foundationClass / kingsChatId)"
            )
            self.log(f"[{account}] ⏭ Skipped {label}: {result['error']}")
            return result

        try:
            self._raise_if_logged_out()
            found = await self._find_card(soul)
            if found is None:
                self._raise_if_logged_out()
                raise LookupError(
                    f"no soul card on the Track Souls page matched {label!r} "
                    "(tried name, phone and email)"
                )
            card, matched_by = found
            result["matchedBy"] = matched_by
            panel = await self._open_panel(card)
            try:
                result["applied"] = await self._fill_panel(panel, values)
                await self._save_panel(panel)
            finally:
                await self._close_panel(panel)
            result["card"] = await self._read_card_progress(card)
            result["status"] = "success"
            self.log(
                f"[{account}] ✅ Tracked {label} (matched by {matched_by}) → "
                f"{_summarize(result['applied'])}"
            )
        except Exception as exc:
            result["error"] = str(exc)
            try:
                await self._close_panel()  # cleanup must never mask the error
            except Exception:
                pass
            self.log(f"[{account}] ❌ Track failed for {label}: {exc}")
        return result

    async def _panel_candidate(self):
        """The visible drawer, if any (text-anchored selector first)."""
        page = self.session.page
        for selector in self.PANEL_CANDIDATES:
            try:
                locator = page.locator(selector).last
                if await locator.count() and await locator.is_visible():
                    return locator
            except Exception:
                continue
        return None

    async def _wait_panel(self):
        deadline = time.monotonic() + Config.TRACK_PANEL_TIMEOUT / 1000
        while time.monotonic() < deadline:
            panel = await self._panel_candidate()
            if panel is not None:
                return panel
            await self.session.page.wait_for_timeout(150)
        return None

    async def _open_panel(self, card):
        """Click the card's Track button and return the drawer that opened.

        The drawer title is compared with the *card's* name: matching may
        legitimately be by phone/email, so the row name can differ from the
        card name, but a drawer opened for a different soul must never be
        filled in.
        """
        card_name = await self._card_name(card)
        await card.get_by_role("button", name=self.TRACK_BUTTON, exact=True).click()
        panel = await self._wait_panel()
        if panel is None:
            self._raise_if_logged_out()
            raise TimeoutError("the Track drawer (pop-up form) did not open")

        title = ""
        try:
            title = (await panel.locator("h3").first.inner_text(timeout=3000)).strip()
        except Exception:
            pass
        if title and card_name and _name_key(title) != _name_key(card_name):
            raise RuntimeError(
                f"drawer opened for {title!r} but the matched card is {card_name!r}"
            )
        return panel

    def _select(self, panel, label: str):
        """The <select> that follows the given field label inside the drawer."""
        return panel.locator(
            f'xpath=.//label[normalize-space(.)="{label}"]/following-sibling::select[1]'
        ).first

    async def _select_option(self, panel, label: str, option: str):
        """Select ``option`` and verify it stuck (Vue may re-render the drawer)."""
        select = self._select(panel, label)
        current = ""
        for attempt in (1, 2):
            await select.select_option(label=option)
            current = await select.evaluate(
                "el => el.options[el.selectedIndex] ? el.options[el.selectedIndex].text : ''"
            )
            if _key(current) == _key(option):
                return
            self.log(f"[track] {label} showed {current!r} after attempt {attempt} - retrying")
        raise RuntimeError(f"could not set {label} to {option!r} (the select shows {current!r})")

    async def _fill_panel(self, panel, values: dict) -> dict:
        """Fill the drawer from ``values``, returning what was actually set."""
        applied = {}
        if values["church_service"]:
            await self._select_option(panel, "Church Service", values["church_service"])
            applied["churchService"] = values["church_service"]
        if values["foundation_class"]:
            await self._select_option(panel, "Foundation School", values["foundation_class"])
            applied["foundationClass"] = values["foundation_class"]
        if values["kingschat_id"]:
            await panel.locator(self.KINGSCHAT_INPUT).first.fill(values["kingschat_id"])
            applied["kingsChatId"] = values["kingschat_id"]
        if values["phone"]:
            field = panel.locator(self.PHONE_INPUT)
            if await field.count():
                await field.first.fill(values["phone"])
                applied["phone"] = values["phone"]
        if values["email"]:
            field = panel.locator(self.EMAIL_INPUT)
            if await field.count():
                await field.first.fill(values["email"])
                applied["email"] = values["email"]
        return applied

    async def _save_panel(self, panel):
        await panel.get_by_role("button", name=self.SAVE_BUTTON, exact=True).click()
        try:
            await panel.wait_for(state="hidden", timeout=Config.TRACK_SAVE_TIMEOUT)
        except Exception:
            if await panel.is_visible():
                raise TimeoutError(
                    f"the drawer was still open {Config.TRACK_SAVE_TIMEOUT} ms after "
                    f"clicking {self.SAVE_BUTTON!r}"
                )

    async def _close_panel(self, panel=None):
        """Close the drawer without saving (cleanup / failure path)."""
        if panel is None:
            panel = await self._panel_candidate()
        if panel is None:
            return
        try:
            if not await panel.is_visible():
                return
            close = panel.locator("button.w-9.h-9")
            if not await close.count():
                close = panel.locator("button:has(svg)")
            if await close.count():
                await close.first.click(timeout=3000)
            else:
                await self.session.page.keyboard.press("Escape")
            await panel.wait_for(state="hidden", timeout=5000)
        except Exception:
            try:
                await self.session.page.keyboard.press("Escape")
                await panel.wait_for(state="hidden", timeout=3000)
            except Exception:
                pass

    async def _read_card_progress(self, card) -> dict:
        """Read back the card's progress rows (proof the save landed)."""
        progress = {}
        for key, label in (
            ("foundationSchool", "Foundation School"),
            ("churchAttendance", "Church Attendance"),
        ):
            locator = card.locator(
                f'xpath=.//span[normalize-space(.)="{label}"]/parent::div/span[last()]'
            )
            try:
                if await locator.count():
                    progress[key] = (await locator.first.inner_text()).strip()
            except Exception:
                continue
        return progress

