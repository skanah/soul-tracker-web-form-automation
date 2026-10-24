from playwright.async_api import async_playwright, Browser, BrowserContext
from config import Config

class BrowserPool:
    """Manages a single Chromium instance with multiple isolated contexts."""
    
    def __init__(self):
        self.playwright = None
        self.browser: Browser | None = None
        self._lock = None

    async def start(self):
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(
            headless=Config.HEADLESS,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                # Shared-hosting safety (Linux/cPanel): Chromium would otherwise
                # crash when /dev/shm is tiny; harmless elsewhere.
                "--disable-dev-shm-usage",
                "--disable-gpu",
            ],
        )

    async def new_context(self) -> BrowserContext:
        if not self.browser:
            await self.start()
        return await self.browser.new_context(
            viewport={"width": 1366, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        )

    async def stop(self):
        if self.browser:
            await self.browser.close()
        if self.playwright:
            await self.playwright.stop()

# Singleton
browser_pool = BrowserPool()