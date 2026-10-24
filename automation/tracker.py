"""
Track Souls automation.
Handles the side-panel form that appears when clicking "Track" on a soul card.
"""
import asyncio
from config import Config


# Class progression values matching the form's <option> values
FOUNDATION_CLASSES = {
    "not started": "0",
    "class 1":     "1",
    "class 2":     "2",
    "class 3":     "3",
    "class 4":     "4",
    "class 5":     "5",
    "class 6":     "6",
    "class 7":     "7",
    "completed":   "8",
}

CHURCH_SERVICES = {
    "not attended any church service yet": "1",
    "attended 1 church service":           "2",
    "attended 2 church services":          "3",
    "attended 3 church services":          "4",
    "attended 4 church services":          "5",
}


class SoulTracker:
    """Tracks a single soul through the side-panel form."""

    def __init__(self, session, log=print):
        self.session = session
        self.log = log

    # ------------------------------------------------------------------
    # PUBLIC API
    # ------------------------------------------------------------------
    async def track(
        self,
        soul_name: str,                    # e.g. "Olu Sheun"
        foundation_class: str = "Class 1",
        church_service: str = "Attended 1 church service",
        kingschat_id: str = "",
        phone: str = "",
        email: str = "",
    ) -> dict:
        """
        Opens the Track panel for the given soul and submits the form.
        Returns {"status": "success"|"failed", "error": ...}
        """
        acc = self.session.account_id
        result = {"account": acc, "soul": soul_name, "class": foundation_class,
                  "status": "failed", "error": None}

        try:
            page = self.session.page

            # 1. Ensure we're on the souls dashboard
            if "/dashboard/souls" not in page.url:
                self.log(f"[{acc}] Navigating to Souls dashboard...")
                await page.goto(
                    "https://www.christembassysoultracker.org/dashboard/souls",
                    wait_until="networkidle",
                )

            # 2. Find the soul card by name and click its "Track" button
            self.log(f"[{acc}] Opening Track panel for '{soul_name}'...")
            await self._click_track_button(soul_name)

            # 3. Wait for the side panel to appear
            await page.wait_for_selector(
                'div.fixed.right-0.top-0 form',
                state="visible",
                timeout=10000,
            )
            self.log(f"[{acc}] Track panel opened.")

            # 4. Fill Church Service dropdown
            church_val = self._resolve_church_value(church_service)
            await page.select_option(
                'div.fixed.right-0.top-0 form select >> nth=0',
                value=church_val,
            )
            self.log(f"[{acc}] Church Service set → {church_service}")

            # 5. Fill Foundation School dropdown
            class_val = self._resolve_class_value(foundation_class)
            await page.select_option(
                'div.fixed.right-0.top-0 form select >> nth=1',
                value=class_val,
            )
            self.log(f"[{acc}] Foundation School set → {foundation_class}")

            # 6. Optional fields
            if kingschat_id:
                await page.fill(
                    'div.fixed.right-0.top-0 form input[placeholder="@username"]',
                    kingschat_id,
                )
            if phone:
                await page.fill(
                    'div.fixed.right-0.top-0 form input[type="tel"]',
                    phone,
                )
            if email:
                await page.fill(
                    'div.fixed.right-0.top-0 form input[type="email"]',
                    email,
                )

            # 7. Click Save Changes
            self.log(f"[{acc}] Submitting Save Changes...")
            await page.click(
                'div.fixed.right-0.top-0 button:has-text("Save Changes")'
            )

            # 8. Wait for the panel to close (success signal)
            try:
                await page.wait_for_selector(
                    'div.fixed.right-0.top-0 form',
                    state="hidden",
                    timeout=15000,
                )
                result["status"] = "success"
                self.log(
                    f"[{acc}] ✅ Tracked '{soul_name}' → {foundation_class}"
                )
            except Exception:
                # Panel didn't close — likely a validation error
                result["error"] = "Panel did not close after Save Changes"
                self.log(f"[{acc}] ⚠️ Panel still open — check validation")

        except Exception as e:
            result["error"] = str(e)
            self.log(f"[{acc}] ❌ Track failed for '{soul_name}': {e}")

        return result

    # ------------------------------------------------------------------
    # PROGRESSION — track a soul through all classes
    # ------------------------------------------------------------------
    async def track_progression(
        self,
        soul_name: str,
        church_service: str = "Attended 1 church service",
        kingschat_id: str = "",
        phone: str = "",
        email: str = "",
        stop_at: str = "Completed",
    ) -> list[dict]:
        """
        Advances a soul through Foundation School one class at a time
        (Class 1 → Class 2 → ... → Completed), saving after each step.
        """
        order = ["Class 1", "Class 2", "Class 3", "Class 4",
                 "Class 5", "Class 6", "Class 7", "Completed"]

        if stop_at not in order:
            stop_at = "Completed"

        results = []
        for cls in order:
            if order.index(cls) > order.index(stop_at):
                break

            res = await self.track(
                soul_name=soul_name,
                foundation_class=cls,
                church_service=church_service,
                kingschat_id=kingschat_id,
                phone=phone,
                email=email,
            )
            results.append(res)

            if res["status"] != "success":
                self.log(f"[{self.session.account_id}] Stopping progression — {cls} failed")
                break

            # Small delay between class transitions
            await asyncio.sleep(1.5)

        return results

    # ------------------------------------------------------------------
    # HELPERS
    # ------------------------------------------------------------------
    async def _click_track_button(self, soul_name: str):
        """
        Locate the soul card by name and click its Track button.
        Uses a resilient selector chain since the HTML is Vue-scoped.
        """
        page = self.session.page

        # Strategy 1: locate the card containing the h3 with exact name
        card = page.locator(
            f'div.group:has(h3:text-is("{soul_name}"))'
        ).first

        if await card.count() == 0:
            # Fallback: partial text match
            card = page.locator(
                f'div.group:has(h3:has-text("{soul_name}"))'
            ).first

        if await card.count() == 0:
            raise ValueError(f"Soul card not found: '{soul_name}'")

        # Scroll into view and click Track
        await card.scroll_into_view_if_needed()
        await card.locator('button:has-text("Track")').click()

    @staticmethod
    def _resolve_class_value(label: str) -> str:
        key = str(label).strip().lower()
        return FOUNDATION_CLASSES.get(key, "0")

    @staticmethod
    def _resolve_church_value(label: str) -> str:
        key = str(label).strip().lower()
        return CHURCH_SERVICES.get(key, "1")