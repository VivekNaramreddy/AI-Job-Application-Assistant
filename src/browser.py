"""
src/browser.py
=============================================================================
Playwright Browser Controller

PYTHON LESSON:
- `asyncio` allows Python to run asynchronous code non-blockingly using `async` and `await`.
- Playwright's `async_api` lets Python drive Chrome browsers asynchronously.
- We support two modes:
  1. Connecting to an existing Chrome browser via CDP (Chrome DevTools Protocol).
  2. Launching a browser with a persistent user profile context (saving login cookies).
=============================================================================
"""

import asyncio
import sys
from pathlib import Path
from typing import Optional
import logging

from playwright.async_api import Browser, BrowserContext, Page, async_playwright

from src.paths import DATA_DIR
USER_DATA_DIR = DATA_DIR / "browser_profile"


class BrowserManager:
    """Manages Playwright browser lifecycle and page interaction."""

    def __init__(self):
        self.playwright = None
        self.browser: Optional[Browser] = None
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None
        self.is_connected = False
        self._owns_context = False

    async def initialize(self, cdp_url: str = "http://localhost:9222", headless: bool = False, log_callback=None) -> Page:
        """
        Initializes Playwright browser session safely.
        First attempts to connect to an open Chrome instance via CDP.
        If unavailable, launches a persistent Chromium context.
        """
        async def log(msg: str):
            print(f"[BROWSER] {msg}")
            if log_callback:
                await log_callback(msg)

        # Playwright communicates with its browser driver through a subprocess.
        # On Windows, asyncio's SelectorEventLoop cannot create subprocesses.
        # Checking here produces a useful dashboard error instead of Playwright's
        # otherwise blank NotImplementedError traceback.
        if sys.platform == "win32":
            running_loop = asyncio.get_running_loop()
            if not isinstance(running_loop, asyncio.ProactorEventLoop):
                raise RuntimeError(
                    "Playwright requires the Windows ProactorEventLoop. "
                    "Start the application with 'python run.py' and keep "
                    "Uvicorn reload mode disabled."
                )

        if not self.playwright:
            if sys.platform == "win32":
                asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
            self.playwright = await async_playwright().start()

        # Check if active page already exists and is open
        if self.page and not self.page.is_closed():
            return self.page
        if self.context:
            try:
                self.page = await self.context.new_page()
                return self.page
            except Exception:
                await self.close()
                self.playwright = await async_playwright().start()

        # Try connecting via Chrome DevTools Protocol (CDP) first
        if cdp_url:
            try:
                await log(f"🌐 Attempting connection to open Chrome session via CDP ({cdp_url})...")
                self.browser = await self.playwright.chromium.connect_over_cdp(cdp_url, timeout=5_000)
                self.context = self.browser.contexts[0]
                if self.context.pages:
                    self.page = await self.context.new_page()
                else:
                    self.page = await self.context.new_page()
                self.is_connected = True
                await log("✅ Successfully connected to existing Chrome browser via CDP!")
                return self.page
            except Exception as e:
                if self.browser:
                    await self.browser.close()
                self.browser = self.context = self.page = None
                error_text = str(e).strip() or type(e).__name__
                await log(f"ℹ️ Could not connect via CDP: {error_text}. Launching browser window...")

        # Fallback: Launch persistent user context (remembers cookies/login state)
        USER_DATA_DIR.mkdir(parents=True, exist_ok=True)
        await log(f"🚀 Launching Chromium with persistent profile at: {USER_DATA_DIR}")
        try:
            self.context = await self.playwright.chromium.launch_persistent_context(
                user_data_dir=str(USER_DATA_DIR),
                headless=headless,
                viewport={"width": 1280, "height": 800},
            )
            self._owns_context = True
            self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()
            self.is_connected = True
            await log("✅ Persistent Chromium browser window launched!")
            return self.page
        except Exception as e:
            err_details = str(e).strip() or type(e).__name__
            await log(f"❌ Failed to launch browser profile: {err_details}")
            await self.close()
            raise RuntimeError(f"Browser launch error: {err_details}. Close other sessions using this profile or install Chromium with 'python -m playwright install chromium'.") from e

    async def get_active_page(self, log_callback=None) -> Page:
        """Returns active browser page, initializing if necessary."""
        if not self.page or self.page.is_closed():
            await self.initialize(log_callback=log_callback)
        return self.page

    async def close(self) -> None:
        """Gracefully closes browser and Playwright engine."""
        resources = []
        if self.context and self._owns_context:
            resources.append((self.context, "close"))
        elif self.page and not self.page.is_closed():
            resources.append((self.page, "close"))
        if self.browser:
            resources.append((self.browser, "close"))
        if self.playwright:
            resources.append((self.playwright, "stop"))
        for resource, method in resources:
            try:
                await getattr(resource, method)()
            except Exception:
                logging.getLogger(__name__).exception("Browser cleanup failed at %s", method)
        self.page = self.context = self.browser = self.playwright = None
        self._owns_context = False
        self.is_connected = False
        print("🛑 Browser session closed.")


browser_manager = BrowserManager()
