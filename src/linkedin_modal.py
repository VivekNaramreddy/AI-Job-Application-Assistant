"""Shared, visible application-dialog detection for LinkedIn flows."""

from playwright.async_api import Page, TimeoutError


# Pair generic containers with application-specific signals; unrelated dialogs
# (cookies, sign-in, etc.) must not count as a started application.
CONTAINERS = ":is([role='dialog'], dialog, [aria-modal='true'], .artdeco-modal)"
MODAL_SELECTOR = (
    ".jobs-easy-apply-modal:visible, "
    f"{CONTAINERS}[aria-label*='Easy Apply' i]:visible, "
    f"{CONTAINERS}:has(:is(h1, h2, h3):has-text('Easy Apply')):visible, "
    f"{CONTAINERS}:has-text('Apply to'):has(:is(input, select, textarea, "
    "[role='progressbar'], progress, button)):visible, "
    f"{CONTAINERS}:has(:is([role='progressbar'], progress))"
    ":has(button:is(:has-text('Next'), :has-text('Review'), :has-text('Submit'))):visible"
)


async def wait_for_linkedin_modal(page: Page, timeout: int = 10_000):
    modal = page.locator(MODAL_SELECTOR).first
    try:
        await modal.wait_for(state="visible", timeout=timeout)
        return modal
    except TimeoutError:
        return None
