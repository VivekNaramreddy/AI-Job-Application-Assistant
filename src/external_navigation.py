"""Capture external applications without sacrificing the LinkedIn page."""

import asyncio
import logging
from contextlib import suppress
from dataclasses import dataclass, field
from typing import List, Optional
from src.linkedin_modal import MODAL_SELECTOR, wait_for_linkedin_modal
from src.urls import canonical_url

from playwright.async_api import BrowserContext, Locator, Page


@dataclass
class ExternalPageSession:
    linkedin_page: Page
    application_page: Page
    auxiliary_pages: List[Page] = field(default_factory=list)
    linkedin_application: bool = False

    async def close(self) -> None:
        for page in [self.application_page, *self.auxiliary_pages]:
            if page is self.linkedin_page or page.is_closed():
                continue
            try:
                await page.close()
            except Exception:
                logging.getLogger(__name__).exception("Could not close external application page")


class ExternalApplicationLauncher:
    """Click Apply, prefer a LinkedIn dialog, then capture external flows."""

    APPLY_SELECTORS = (
        "button.jobs-apply-button, a.jobs-apply-button",
        "button[aria-label*='Apply' i], a[aria-label*='Apply' i]",
        "main button:has-text('Apply'), main a:has-text('Apply')",
    )
    APPLY_SELECTOR = ", ".join(APPLY_SELECTORS)

    @classmethod
    async def open(
        cls, linkedin_page: Page, apply_control: Locator
    ) -> ExternalPageSession:
        owned = []
        try:
            return await cls._open(linkedin_page, apply_control, owned)
        except BaseException:
            for page in owned:
                if page is not linkedin_page and not page.is_closed():
                    with suppress(Exception):
                        await page.close()
            raise

    @classmethod
    async def _open(cls, linkedin_page: Page, apply_control: Locator, owned: list) -> ExternalPageSession:
        before_url = linkedin_page.url
        application_page = await cls._click_and_capture(linkedin_page.context, linkedin_page, apply_control)
        if application_page is linkedin_page and await wait_for_linkedin_modal(linkedin_page, timeout=500):
            return ExternalPageSession(linkedin_page, linkedin_page, linkedin_application=True)
        if application_page is linkedin_page:
            await cls._wait_for_redirects(application_page)
            destination = application_page.url
            application_page = await linkedin_page.context.new_page()
            owned.append(application_page)
            await application_page.goto(destination, wait_until="domcontentloaded", timeout=30_000)
            await linkedin_page.goto(before_url, wait_until="domcontentloaded", timeout=30_000)
        else:
            owned.append(application_page)
        await cls._wait_for_redirects(application_page)
        return ExternalPageSession(linkedin_page, application_page)

    @classmethod
    async def _click_and_capture(cls, context: BrowserContext, click_page: Page, control: Locator) -> Page:
        before_url = click_page.url
        tasks = [
            asyncio.create_task(click_page.wait_for_event("popup", timeout=10_000)),
            asyncio.create_task(context.wait_for_event("page", timeout=10_000)),
            asyncio.create_task(click_page.wait_for_url(
                lambda url: url != before_url, timeout=10_000, wait_until="domcontentloaded"
            )),
        ]
        try:
            await control.click(timeout=7_000)
            if await wait_for_linkedin_modal(click_page, timeout=1_500):
                return click_page
            modal_task = asyncio.create_task(wait_for_linkedin_modal(click_page, timeout=8_500))
            tasks.append(modal_task)
            pending = set(tasks)
            while pending:
                done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                if modal_task in done and modal_task.result() is not None:
                    return click_page
                for task in done:
                    if task is modal_task or task.cancelled() or task.exception():
                        continue
                    if await click_page.locator(MODAL_SELECTOR).count():
                        return click_page
                    return task.result() if task in tasks[:2] else click_page
            raise RuntimeError("Apply did not open a LinkedIn application modal, new tab, or application page.")
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    @classmethod
    async def _find_external_apply(cls, page: Page) -> Optional[Locator]:
        for selector in cls.APPLY_SELECTORS:
            controls = page.locator(selector)
            for index in range(await controls.count()):
                control = controls.nth(index)
                if not await control.is_visible() or await control.is_disabled():
                    continue
                text = (
                    f"{await control.inner_text()} "
                    f"{await control.get_attribute('aria-label') or ''}"
                ).lower()
                if "easy apply" not in text:
                    return control
        return None

    @staticmethod
    async def _wait_for_redirects(page: Page) -> None:
        with suppress(Exception):
            await page.wait_for_load_state("domcontentloaded", timeout=15_000)
        stable_url = ""
        stable_passes = 0
        for _ in range(40):
            current = page.url
            stable_passes = stable_passes + 1 if current == stable_url else 0
            stable_url = current
            if stable_passes >= 3 and not current.startswith("about:"):
                return
            await asyncio.sleep(0.15)

    canonical_url = staticmethod(canonical_url)
