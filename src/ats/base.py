"""Shared external ATS adapter contract and deterministic form behavior."""

import asyncio
import re
from abc import ABC
from urllib.parse import urlsplit
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence

from playwright.async_api import Locator, Page

from src.application import ApplicationField
from src.form_filler import FormFiller


LogFunction = Callable[[str], Awaitable[None]]


class ATSAdapter(ABC):
    """Adapter interface used by the external-application state machine."""

    name = "Generic HTML"
    host_patterns: Sequence[str] = ()
    page_markers: Sequence[str] = ()
    form_selectors: Sequence[str] = (
        "[data-automation-id='applicationForm']",
        "#application-form",
        "form[action*='apply' i]",
        "form[action*='application' i]",
        "form",
    )
    start_selectors: Sequence[str] = (
        "a:has-text('Apply for this job')",
        "a:has-text('Apply now')",
        "button:has-text('Apply for this job')",
        "button:has-text('Apply now')",
        "button:has-text('Start application')",
    )
    next_selectors: Sequence[str] = (
        "button:has-text('Save and continue')",
        "button:has-text('Continue')",
        "button:has-text('Next')",
        "button:has-text('Review')",
        "input[type='button'][value*='Continue' i]",
        "input[type='button'][value*='Next' i]",
    )
    submit_selectors: Sequence[str] = (
        "button:has-text('Submit application')",
        "button:has-text('Submit Application')",
        "button:has-text('Submit')",
        "input[type='submit'][value*='Submit' i]",
    )
    error_selectors: Sequence[str] = (
        "[role='alert']",
        ".error-message",
        ".field-error",
        "[aria-invalid='true'] + *",
    )

    def __init__(self, page: Page, log: LogFunction):
        self.page = page
        self.log = log
        self.form_filler = FormFiller(page)

    @classmethod
    async def detect(cls, page: Page, url: str) -> bool:
        host = (urlsplit(url).hostname or "").lower()
        if cls.host_patterns and any(host == pattern or host.endswith("." + pattern) for pattern in cls.host_patterns):
            return True
        if cls.page_markers:
            for marker in cls.page_markers:
                if await page.locator(marker).count():
                    return True
        return cls.name == "Generic HTML"

    async def inspect(self) -> Dict[str, Any]:
        """Locate an application form, clicking a safe start/apply control once."""
        manual_reason = await self.manual_intervention_reason()
        if manual_reason:
            return {"ats": self.name, "url": self.page.url,
                    "form_found": False, "manual_reason": manual_reason}
        root = await self._form_root()
        if root is None:
            start = await self._first_visible_selectors(self.start_selectors)
            if start is not None:
                before_url = self.page.url
                await start.click(timeout=7_000)
                await self._wait_for_navigation_or_form(before_url)
                root = await self._form_root()
        return {
            "ats": self.name,
            "url": self.page.url,
            "form_found": root is not None,
            "manual_reason": await self.manual_intervention_reason(),
        }

    async def extract_fields(self) -> List[ApplicationField]:
        root = await self._form_root()
        if root is None:
            return []
        from src.config import config_manager

        profile = config_manager.profile
        return await self.form_filler.analyze_fields(
            root,
            profile.get("personal_info", {}),
            profile.get("default_answers", {}),
        )

    async def fill_fields(self) -> Dict[str, Any]:
        """Fill only deterministic profile or approved-memory answers."""
        root = await self._form_root()
        if root is None:
            return {"fields": [], "unresolved": [], "resume_used": ""}
        from src.config import config_manager

        profile = config_manager.profile
        personal = profile.get("personal_info", {})
        defaults = profile.get("default_answers", {})
        unanswered: List[Dict[str, Any]] = []
        await self.form_filler._fill_inputs(root, personal, defaults, unanswered, self.log)
        await self.form_filler._fill_selects(root, personal, defaults, unanswered, self.log)
        await self.form_filler._fill_custom_comboboxes(
            root, personal, defaults, unanswered, self.log
        )
        await self.form_filler._fill_radio_groups(
            root, personal, defaults, unanswered, self.log
        )
        await self.form_filler._fill_required_checkboxes(root, unanswered, self.log)
        resume_used = await self.form_filler._handle_resume(
            root, personal, unanswered, self.log
        )
        fields = await self.extract_fields()
        unanswered.extend(self.form_filler._unresolved_required_fields(fields))
        return {
            "fields": fields,
            "unresolved": self.form_filler._deduplicate_questions(unanswered),
            "resume_used": resume_used,
            "resolved_count": sum(bool(field.current_value) for field in fields),
        }

    async def validate(self) -> List[str]:
        root = await self._form_root()
        if root is None:
            return []
        errors: List[str] = []
        for selector in self.error_selectors:
            errors.extend(await self.form_filler._visible_texts(root.locator(selector)))
        return list(dict.fromkeys(error for error in errors if error))

    async def next_step(self) -> bool:
        root = await self._form_root()
        if root is None:
            return False
        button = await self._first_visible_in(root, self.next_selectors)
        if button is None:
            return False
        before_url = self.page.url
        before_signature = await self.form_filler._step_signature(root)
        await button.click(timeout=7_000)
        for _ in range(40):
            await asyncio.sleep(0.15)
            if self.page.url != before_url:
                return True
            current_root = await self._form_root()
            if current_root is None:
                return True
            if await self.form_filler._step_signature(current_root) != before_signature:
                return True
            if await self.validate():
                return False
        return False

    async def is_final_step(self) -> bool:
        return await self._submit_button() is not None

    async def submit(self) -> None:
        """Click Submit only when the caller has explicit human approval."""
        button = await self._submit_button()
        if button is None:
            raise RuntimeError("Final submit control is no longer available.")
        await button.click(timeout=7_000)

    async def submission_confirmed(self) -> bool:
        confirmation = re.compile(
            r"thank you for (?:applying|your application)|application (?:has been |was )?(?:received|submitted|sent)|"
            r"we(?:'ve| have) received your application",
            re.IGNORECASE,
        )
        for _ in range(30):
            match = await self.form_filler._first_visible(
                self.page.get_by_text(confirmation)
            )
            if match is not None:
                return True
            await asyncio.sleep(0.25)
        return False

    async def manual_intervention_reason(self) -> str:
        url = self.page.url.lower()
        if any(token in url for token in ("captcha", "checkpoint", "challenge")):
            return "captcha"
        challenge = await self.form_filler._first_visible(
            self.page.locator(
                "iframe[src*='captcha' i], [id*='captcha' i], "
                "[class*='captcha' i]"
            )
        )
        if challenge is not None:
            return "captcha"
        password = await self.form_filler._first_visible(self.page.locator("input[type='password']"))
        if password is not None or any(token in url for token in ("/login", "/signin", "/sign-in")):
            return "login_required"
        return ""

    async def _form_root(self) -> Optional[Locator]:
        candidates: List[Locator] = []
        for selector in self.form_selectors:
            locator = self.page.locator(selector)
            for index in range(await locator.count()):
                candidate = locator.nth(index)
                if await candidate.is_visible():
                    candidates.append(candidate)
        for candidate in candidates:
            if await candidate.locator("input[type='file']").count():
                return candidate
        for candidate in candidates:
            controls = candidate.locator("input:not([type='hidden']), textarea, select")
            if await controls.count() >= 2 and not await candidate.locator("input[type='password']").count():
                return candidate
        return candidates[0] if candidates else None

    async def _submit_button(self) -> Optional[Locator]:
        root = await self._form_root()
        if root is None:
            return None
        return await self._first_visible_in(root, self.submit_selectors)

    async def _first_visible_selectors(
        self, selectors: Sequence[str]
    ) -> Optional[Locator]:
        return await self._first_visible_in(self.page.locator("body"), selectors)

    async def _first_visible_in(
        self, root: Locator, selectors: Sequence[str]
    ) -> Optional[Locator]:
        for selector in selectors:
            candidate = await self.form_filler._first_visible(root.locator(selector))
            if candidate is not None and not await candidate.is_disabled():
                return candidate
        return None

    async def _wait_for_navigation_or_form(self, old_url: str) -> None:
        for _ in range(40):
            await asyncio.sleep(0.15)
            if self.page.url != old_url or await self._form_root() is not None:
                return


class GenericHTMLAdapter(ATSAdapter):
    """Fallback for conventional accessible HTML application forms."""

    name = "Generic HTML"
