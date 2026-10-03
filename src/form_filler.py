"""Reliable LinkedIn Easy Apply form analysis and completion."""

import asyncio
import re
from typing import Any, Awaitable, Callable, Dict, List, Optional

from playwright.async_api import Locator, Page

from src.linkedin_modal import MODAL_SELECTOR, wait_for_linkedin_modal
from src.application import AnswerResolution, ApplicationField, ApplicationState
from src.answer_resolver import AnswerResolver
from src.config import config_manager
from src.database import db_manager
from src.llm import create_llm_provider
from src.resume import ResumeManager


LogCallback = Optional[Callable[[str], Awaitable[None]]]
ApprovalCallback = Optional[Callable[[Dict[str, Any]], Awaitable[Dict[str, Any]]]]
StateCallback = Optional[Callable[[ApplicationState, Dict[str, Any]], Awaitable[None]]]


class FormFiller:
    """Analyze and complete deterministic Easy Apply fields without guessing."""

    MAX_FORM_STEPS = 25
    MODAL_SELECTOR = MODAL_SELECTOR

    def __init__(self, page: Page):
        self.page = page
        self._resolution_sources: Dict[str, AnswerResolution] = {}
        settings = config_manager.profile.get("ai_settings", {})
        self.answer_resolver = AnswerResolver(
            llm_provider=create_llm_provider(config_manager.profile),
            minimum_confidence=float(settings.get("minimum_confidence", 0.85)),
        )

    async def fill_easy_apply_form(
        self,
        job_title: str,
        company: str,
        job_url: str,
        location: str = "LinkedIn",
        job_type: str = "",
        log_callback: LogCallback = None,
        approval_callback: ApprovalCallback = None,
        state_callback: StateCallback = None,
    ) -> Dict[str, Any]:
        async def log(message: str) -> None:
            print(f"[FORM] {message}")
            if log_callback:
                await log_callback(message)

        async def set_state(state: ApplicationState, **details: Any) -> None:
            if state_callback:
                await state_callback(state, details)

        profile = config_manager.profile
        personal_info = profile.get("personal_info", {})
        default_answers = profile.get("default_answers", {})
        manual_wait_seconds = max(
            0,
            int(
                profile.get("work_preferences", {}).get(
                    "approval_timeout_minutes", 0
                )
            ),
        ) * 60
        resume_used = ""
        self._resolution_sources.clear()
        await log(f"Completing Easy Apply form for {job_title} at {company}.")

        security_reason = await self._security_interruption()
        if security_reason:
            question = self._question(
                security_reason, "LinkedIn security verification", "security_check"
            )
            await set_state(
                ApplicationState.NEEDS_USER_INPUT,
                reason=security_reason,
                questions=[question],
            )
            await log(
                f"Paused for manual LinkedIn verification: {security_reason}. "
                "Complete it in the live browser. The worker will resume when it clears."
            )
            if not await self._wait_for_manual_security_completion(
                manual_wait_seconds
            ):
                return {
                    "status": "flagged",
                    "reason": "security_verification_required",
                    "questions": [question],
                    "preserve_modal": True,
                }
            await log("LinkedIn verification cleared; resuming the application.")

        modal = await self._wait_for_easy_apply_modal()
        if modal is None:
            reason = "Easy Apply dialog did not open"
            await set_state(ApplicationState.FAILED, reason=reason)
            return {"status": "incomplete", "reason": "modal_not_found"}
        await set_state(ApplicationState.APPLICATION_STARTED)

        previous_signature = ""
        repeated_signature_count = 0
        for step in range(1, self.MAX_FORM_STEPS + 1):
            modal = await self._wait_for_easy_apply_modal(timeout=5_000)
            if modal is None:
                reason = "Easy Apply dialog disappeared before completion"
                await set_state(ApplicationState.FAILED, reason=reason, step=step)
                await log(reason + ".")
                return {"status": "incomplete", "reason": "modal_closed"}

            security_reason = await self._security_interruption()
            if security_reason:
                question = self._question(
                    security_reason, "LinkedIn security verification", "security_check"
                )
                await set_state(
                    ApplicationState.NEEDS_USER_INPUT,
                    reason=security_reason,
                    questions=[question],
                    step=step,
                )
                await log(
                    f"Paused for manual LinkedIn verification: {security_reason}. "
                    "Complete it in the live browser. The worker will resume when it clears."
                )
                if await self._wait_for_manual_security_completion(
                    manual_wait_seconds
                ):
                    await log("LinkedIn verification cleared; resuming the application.")
                    continue
                return {
                    "status": "flagged",
                    "reason": "security_verification_required",
                    "questions": [question],
                    "preserve_modal": True,
                }

            fields = await self.analyze_fields(modal, personal_info, default_answers)
            await set_state(
                ApplicationState.FORM_ANALYZED,
                step=step,
                field_count=len(fields),
                fields=[field.as_dict() for field in fields],
            )
            await set_state(ApplicationState.FILLING, step=step)

            unanswered: List[Dict[str, Any]] = []
            await self._fill_inputs(modal, personal_info, default_answers, unanswered, log)
            await self._fill_selects(modal, personal_info, default_answers, unanswered, log)
            await self._fill_custom_comboboxes(
                modal, personal_info, default_answers, unanswered, log
            )
            await self._fill_radio_groups(
                modal, personal_info, default_answers, unanswered, log
            )
            await self._fill_required_checkboxes(modal, unanswered, log)
            uploaded = await self._handle_resume(modal, personal_info, unanswered, log)
            resume_used = uploaded or resume_used

            refreshed_fields = await self.analyze_fields(modal, personal_info, default_answers)
            unanswered.extend(self._unresolved_required_fields(refreshed_fields))
            unanswered = self._deduplicate_questions(unanswered)

            if unanswered:
                reason = f"Requires user input on {len(unanswered)} required question(s)"
                db_manager.log_flagged_job(
                    job_title=job_title,
                    company=company,
                    job_url=job_url,
                    reason=reason,
                    unhandled_questions=unanswered,
                )
                await set_state(
                    ApplicationState.NEEDS_USER_INPUT,
                    reason=reason,
                    questions=unanswered,
                    step=step,
                    resume_used=resume_used,
                )
                await log(
                    f"Paused because {len(unanswered)} required question(s) need "
                    "a confirmed answer. The live application remains open."
                )
                return {
                    "status": "flagged",
                    "reason": "unanswered_required_questions",
                    "questions": unanswered,
                    "preserve_modal": True,
                    "resume_used": resume_used,
                }

            submit_button = await self._find_submit_button(modal)
            if submit_button is not None:
                ready_details = {
                    "job_title": job_title,
                    "company": company,
                    "job_url": job_url,
                    "location": location,
                    "job_type": job_type,
                    "required_fields_complete": True,
                    "resume_uploaded": bool(resume_used),
                    "resume_used": resume_used,
                    "final_submit_detected": True,
                    "field_count": len(refreshed_fields),
                    "step_count": step,
                }
                await set_state(ApplicationState.READY_FOR_REVIEW, **ready_details)
                await log(
                    "READY FOR REVIEW | "
                    f"Job: {job_title} | Company: {company} | "
                    "Required fields complete: Yes | "
                    f"Resume uploaded: {'Yes' if resume_used else 'No/not requested'} | "
                    "Final submit button detected: Yes"
                )
                if approval_callback is None:
                    return {
                        "status": "ready_for_review",
                        "preserve_modal": True,
                        **ready_details,
                    }

                approval = await approval_callback(ready_details)
                review_id = approval.get("review_id")
                decision = approval.get("decision")
                if decision != "approved":
                    if decision == "rejected":
                        await self._cancel_modal(modal)
                        await set_state(
                            ApplicationState.SKIPPED,
                            reason="User rejected the prepared application",
                        )
                        await log(
                            f"You rejected the application for {job_title}; it was not submitted."
                        )
                        return {"status": "rejected", "review_id": review_id}
                    await set_state(
                        ApplicationState.FAILED,
                        reason="Dashboard approval timed out",
                    )
                    await log(
                        f"Approval for {job_title} expired; the application was not submitted."
                    )
                    return {
                        "status": "incomplete",
                        "reason": "approval_timeout",
                        "review_id": review_id,
                        "preserve_modal": True,
                    }

                if not await submit_button.is_visible():
                    reason = "Submit button disappeared while approval was pending"
                    await set_state(ApplicationState.FAILED, reason=reason)
                    await log(reason + ".")
                    return {
                        "status": "incomplete",
                        "reason": "submit_button_disappeared",
                        "review_id": review_id,
                    }
                # Explicit dashboard approval is the only path to this click.
                await submit_button.click()
                confirmed = await self._wait_for_submission_confirmation(modal)
                if not confirmed:
                    reason = "Submission click was not confirmed by LinkedIn"
                    await set_state(ApplicationState.FAILED, reason=reason)
                    await log(reason + ".")
                    return {
                        "status": "incomplete",
                        "reason": "submission_unconfirmed",
                        "review_id": review_id,
                    }
                await log(f"Successfully submitted the application for {job_title}.")
                return {
                    "status": "submitted",
                    "review_id": review_id,
                    "resume_used": resume_used,
                }

            advance_button = await self._find_advance_button(modal)
            if advance_button is None:
                errors = await self._validation_errors(modal)
                if errors:
                    return await self._pause_for_validation(
                        errors, job_title, company, job_url, step, resume_used,
                        log, set_state,
                    )
                reason = f"No Next, Review, or Submit control found on step {step}"
                await set_state(ApplicationState.FAILED, reason=reason, step=step)
                await log(reason + ".")
                return {"status": "incomplete", "reason": "no_advance_button"}

            signature = await self._step_signature(modal)
            if signature == previous_signature:
                repeated_signature_count += 1
            else:
                repeated_signature_count = 0
            previous_signature = signature
            if repeated_signature_count >= 2:
                reason = f"Easy Apply did not advance beyond step {step}"
                await set_state(ApplicationState.FAILED, reason=reason, step=step)
                await log(reason + ".")
                return {"status": "incomplete", "reason": "step_did_not_advance"}

            await advance_button.click(timeout=5_000)
            changed = await self._wait_for_step_change(modal, signature)
            errors = await self._validation_errors(modal)
            if errors:
                return await self._pause_for_validation(
                    errors, job_title, company, job_url, step, resume_used,
                    log, set_state,
                )
            if not changed:
                await log(
                    f"LinkedIn did not visibly change step {step}; re-analyzing before retry."
                )
            else:
                await log(f"Completed Easy Apply step {step}; advancing.")

        reason = f"Easy Apply exceeded the {self.MAX_FORM_STEPS}-step safety limit"
        await set_state(ApplicationState.FAILED, reason=reason)
        await log(reason + ".")
        return {"status": "incomplete", "reason": "too_many_steps"}

    async def analyze_fields(
        self,
        modal: Locator,
        personal_info: Optional[Dict[str, Any]] = None,
        default_answers: Optional[Dict[str, Any]] = None,
    ) -> List[ApplicationField]:
        """Return normalized descriptions of all actionable controls on a step."""
        personal_info = personal_info or {}
        default_answers = default_answers or {}
        records: List[ApplicationField] = []
        controls = modal.locator(
            "input:not([type='hidden']):not([type='submit']):not([type='button']), "
            "textarea, select, [role='combobox']:not(select):not(input)"
        )
        seen_radio_names = set()
        for index in range(await controls.count()):
            control = controls.nth(index)
            tag = await control.evaluate("element => element.tagName.toLowerCase()")
            input_type = (await control.get_attribute("type") or "").lower()
            if input_type != "file" and not await control.is_visible():
                continue
            if await control.is_disabled():
                continue
            label = await self._get_associated_label(control)
            name = (await control.get_attribute("name") or "").strip()
            element_id = (await control.get_attribute("id") or "").strip()
            control_type = self._field_type(
                tag, input_type, await control.get_attribute("role")
            )
            options: List[str] = []
            current = ""
            if control_type == "radio":
                label = await self._radio_group_label(control) or label
                group_key = name or label
                if group_key in seen_radio_names:
                    continue
                seen_radio_names.add(group_key)
                selector = (
                    f"input[type='radio'][name={self._css_string(name)}]"
                    if name else "input[type='radio']"
                )
                group = modal.locator(selector)
                for radio_index in range(await group.count()):
                    radio = group.nth(radio_index)
                    radio_label = await self._get_associated_label(radio)
                    options.append(
                        radio_label or await radio.get_attribute("value") or ""
                    )
                    if await radio.is_checked():
                        current = (
                            radio_label
                            or await radio.get_attribute("value")
                            or "selected"
                        )
            elif control_type == "checkbox":
                current = "checked" if await control.is_checked() else ""
            elif control_type == "select":
                option_nodes = control.locator("option")
                options = [
                    (await option_nodes.nth(option_index).inner_text()).strip()
                    for option_index in range(await option_nodes.count())
                ]
                current = (await control.input_value()).strip()
                if current and not await self._select_has_value(control):
                    current = ""
            elif control_type == "combobox":
                current = (
                    await control.get_attribute("value")
                    or await control.get_attribute("aria-valuetext")
                    or (await control.inner_text()).strip()
                )
                if self._looks_like_placeholder(current):
                    current = ""
            else:
                try:
                    current = (await control.input_value()).strip()
                except Exception:
                    current = ""
            resolution = self.resolve_answer(label, personal_info, default_answers)
            recorded = self._resolution_sources.get(self._answer_key(label))
            records.append(
                ApplicationField(
                    key=name or element_id or f"field_{index + 1}",
                    label=label or f"Field {index + 1}",
                    field_type=self._semantic_field_type(label, control_type),
                    required=await self._is_required(control, label),
                    options=[option for option in options if option],
                    current_value=current,
                    resolved_value=current or resolution.value,
                    source=(recorded.source if current and recorded else "form") if current else resolution.source,
                    confidence=(recorded.confidence if current and recorded else 1.0) if current else resolution.confidence,
                )
            )
        return records

    async def _fill_inputs(
        self,
        modal: Locator,
        personal_info: Dict[str, Any],
        default_answers: Dict[str, Any],
        unanswered: List[Dict[str, Any]],
        log,
    ) -> None:
        inputs = modal.locator(
            "input[type='text']:not([role='combobox']), input[type='number'], "
            "input[type='email'], input[type='tel'], input[type='url'], "
            "input[type='date'], textarea"
        )
        for index in range(await inputs.count()):
            field = inputs.nth(index)
            if not await field.is_visible() or await field.is_disabled():
                continue
            label = await self._get_associated_label(field)
            value = (await field.input_value()).strip()
            if not value:
                resolution = await self.resolve_answer_async(
                    label, personal_info, default_answers
                )
                if resolution.value:
                    answer = await self._normalise_input_answer(
                        field, resolution.value
                    )
                    if answer:
                        await field.fill(answer)
                        value = (await field.input_value()).strip()
                        if value:
                            self._remember_resolution(label, resolution)
                        await log(
                            f"Filled '{self._short_label(label)}' from {resolution.source}."
                        )
            if not value and await self._is_required(field, label):
                unanswered.append(
                    self._question(
                        label,
                        f"Input field {index + 1}",
                        self._semantic_field_type(
                            label,
                            self._field_type(
                                "input", await field.get_attribute("type") or "text", ""
                            ),
                        ),
                    )
                )

    async def _fill_selects(
        self,
        modal: Locator,
        personal_info: Dict[str, Any],
        default_answers: Dict[str, Any],
        unanswered: List[Dict[str, Any]],
        log,
    ) -> None:
        selects = modal.locator("select")
        for index in range(await selects.count()):
            field = selects.nth(index)
            if not await field.is_visible() or await field.is_disabled():
                continue
            label = await self._get_associated_label(field)
            if await self._select_has_value(field):
                continue
            option_nodes = field.locator("option")
            option_labels = [
                (await option_nodes.nth(i).inner_text()).strip()
                for i in range(await option_nodes.count())
            ]
            resolution = await self.resolve_answer_async(
                label, personal_info, default_answers, option_labels
            )
            if resolution.value and await self._select_matching_option(
                field, resolution.value
            ):
                self._remember_resolution(label, resolution)
                await log(
                    f"Selected '{self._short_label(label)}' from {resolution.source}."
                )
            elif await self._is_required(field, label):
                unanswered.append(
                    self._question(label, f"Dropdown field {index + 1}", "select")
                )

    async def _fill_custom_comboboxes(
        self,
        modal: Locator,
        personal_info: Dict[str, Any],
        default_answers: Dict[str, Any],
        unanswered: List[Dict[str, Any]],
        log,
    ) -> None:
        controls = modal.locator("[role='combobox']:not(select)")
        for index in range(await controls.count()):
            control = controls.nth(index)
            if not await control.is_visible() or await control.is_disabled():
                continue
            label = await self._get_associated_label(control)
            current = (
                await control.get_attribute("value")
                or await control.get_attribute("aria-valuetext")
                or (await control.inner_text()).strip()
            )
            if current and not self._looks_like_placeholder(current):
                continue
            resolution = await self.resolve_answer_async(
                label, personal_info, default_answers
            )
            matched = False
            if resolution.value:
                try:
                    await control.click()
                    options = self.page.locator("[role='option']:visible")
                    for option_index in range(await options.count()):
                        option = options.nth(option_index)
                        text = (await option.inner_text()).strip()
                        if self._answers_match(resolution.value, text):
                            await option.click()
                            matched = True
                            self._remember_resolution(label, resolution)
                            await log(
                                f"Selected '{self._short_label(label)}' from {resolution.source}."
                            )
                            break
                except Exception:
                    matched = False
            if not matched and await self._is_required(control, label):
                unanswered.append(
                    self._question(label, f"Dropdown field {index + 1}", "combobox")
                )

    async def _fill_radio_groups(
        self,
        modal: Locator,
        personal_info: Dict[str, Any],
        default_answers: Dict[str, Any],
        unanswered: List[Dict[str, Any]],
        log,
    ) -> None:
        radios = modal.locator("input[type='radio']")
        groups: Dict[str, List[Locator]] = {}
        for index in range(await radios.count()):
            radio = radios.nth(index)
            if not await radio.is_visible() or await radio.is_disabled():
                continue
            name = await radio.get_attribute("name") or f"radio_{index}"
            groups.setdefault(name, []).append(radio)
        for group_index, group in enumerate(groups.values(), start=1):
            if any([await radio.is_checked() for radio in group]):
                continue
            question = await self._radio_group_label(group[0])
            option_labels = []
            for radio in group:
                option_labels.append(
                    (await self._get_associated_label(radio))
                    or (await radio.get_attribute("value") or "")
                )
            resolution = await self.resolve_answer_async(
                question, personal_info, default_answers, option_labels
            )
            matched = False
            if resolution.value:
                for radio in group:
                    radio_label = await self._get_associated_label(radio)
                    radio_value = await radio.get_attribute("value") or ""
                    if self._answers_match(
                        resolution.value, f"{radio_label} {radio_value}"
                    ):
                        await radio.check()
                        matched = True
                        self._remember_resolution(question, resolution)
                        await log(
                            f"Answered '{self._short_label(question)}' from {resolution.source}."
                        )
                        break
            required = any(
                [await self._is_required(radio, question) for radio in group]
            )
            if not matched and required:
                unanswered.append(
                    self._question(question, f"Question {group_index}", "radio")
                )

    async def _fill_required_checkboxes(
        self, modal: Locator, unanswered: List[Dict[str, Any]], log
    ) -> None:
        checkboxes = modal.locator("input[type='checkbox']")
        for index in range(await checkboxes.count()):
            checkbox = checkboxes.nth(index)
            if not await checkbox.is_visible() or await checkbox.is_checked():
                continue
            label = await self._get_associated_label(checkbox)
            if not await self._is_required(checkbox, label):
                continue
            normalised = label.lower()
            if any(
                word in normalised
                for word in ("privacy", "terms", "acknowledge", "agree")
            ):
                await checkbox.check()
                await log(
                    f"Accepted required acknowledgement '{self._short_label(label)}'."
                )
            else:
                unanswered.append(
                    self._question(label, f"Checkbox {index + 1}", "checkbox")
                )

    async def _handle_resume(
        self,
        modal: Locator,
        personal_info: Dict[str, Any],
        unanswered: List[Dict[str, Any]],
        log,
    ) -> str:
        uploads = modal.locator("input[type='file']")
        uploaded_filename = ""
        for index in range(await uploads.count()):
            upload = uploads.nth(index)
            if (await upload.input_value()).strip():
                uploaded_filename = (
                    await upload.input_value()
                ).replace("\\", "/").rsplit("/", 1)[-1]
                continue
            label = await self._get_associated_label(upload)
            is_resume = (
                any(token in label.lower() for token in ("resume", "résumé", "cv"))
                or await uploads.count() == 1
            )
            if not is_resume:
                if await self._is_required(upload, label):
                    unanswered.append(
                        self._question(label, "Required document upload", "file_upload")
                    )
                continue
            try:
                selection = ResumeManager.configured_resume(personal_info)
                uploaded_filename = await ResumeManager.upload(upload, selection)
                await log(f"Uploaded configured resume '{uploaded_filename}'.")
            except (ValueError, RuntimeError) as exc:
                if await self._is_required(upload, label):
                    unanswered.append(
                        {
                            **self._question(label, "Resume upload", "file_upload"),
                            "reason": str(exc),
                        }
                    )
        return uploaded_filename

    def resolve_answer(
        self,
        label_text: str,
        personal_info: Dict[str, Any],
        default_answers: Dict[str, Any],
    ) -> AnswerResolution:
        """Compatibility-safe deterministic resolution for tests and extensions."""
        if not label_text:
            return AnswerResolution()
        profile_data = {
            "personal_info": dict(personal_info or {}),
            "default_answers": dict(default_answers or {}),
            "work_preferences": {},
        }
        return self.answer_resolver.resolve_deterministic(label_text, profile_data)

    async def resolve_answer_async(
        self,
        label_text: str,
        personal_info: Dict[str, Any],
        default_answers: Dict[str, Any],
        options: Optional[List[str]] = None,
    ) -> AnswerResolution:
        """Use the centralized profile/memory, with optional bounded AI last."""
        current = config_manager.profile
        if (
            personal_info is current.get("personal_info")
            and default_answers is current.get("default_answers")
        ):
            profile_data = current
        else:
            profile_data = {
                "personal_info": dict(personal_info or {}),
                "default_answers": dict(default_answers or {}),
                "work_preferences": {},
            }
        return await self.answer_resolver.resolve(label_text, profile_data, options)

    @staticmethod
    def _answer_key(label_text: str) -> str:
        return re.sub(r"[^a-z0-9]+", " ", str(label_text).lower()).strip()

    def _remember_resolution(
        self, label_text: str, resolution: AnswerResolution
    ) -> None:
        self._resolution_sources[self._answer_key(label_text)] = resolution

    @staticmethod
    def _label_contains(label: str, phrase: str) -> bool:
        return bool(
            re.search(
                rf"(?<![a-z0-9]){re.escape(phrase.lower())}(?![a-z0-9])",
                label,
            )
        )

    def _determine_answer(
        self,
        label_text: str,
        personal_info: Dict[str, Any],
        default_answers: Dict[str, Any],
    ) -> str:
        """Compatibility wrapper used by older tests and extensions."""
        return self.resolve_answer(label_text, personal_info, default_answers).value

    async def _get_associated_label(self, element: Locator) -> str:
        try:
            text = await element.evaluate(
                """element => {
                    const clean = value => (value || '').replace(/\\s+/g, ' ').trim();
                    const aria = clean(element.getAttribute('aria-label'));
                    if (aria) return aria;
                    const ids = clean(element.getAttribute('aria-labelledby')).split(' ').filter(Boolean);
                    if (ids.length) {
                        const labelled = ids.map(id => document.getElementById(id))
                            .filter(Boolean).map(node => clean(node.innerText || node.textContent)).join(' ');
                        if (labelled) return labelled;
                    }
                    if (element.labels && element.labels.length) {
                        const labels = Array.from(element.labels)
                            .map(node => clean(node.innerText || node.textContent)).filter(Boolean);
                        if (labels.length) return labels.join(' ');
                    }
                    const container = element.closest(
                        'fieldset, [data-test-form-element], .jobs-easy-apply-form-element, .fb-dash-form-element, div'
                    );
                    if (container) {
                        const node = container.querySelector('legend, label, [data-test-form-element-label]');
                        const nearby = clean(node && (node.innerText || node.textContent));
                        if (nearby) return nearby;
                    }
                    return clean(element.getAttribute('placeholder')) ||
                        clean(element.getAttribute('name')) || clean(element.id);
                }"""
            )
            return str(text or "").strip()
        except Exception:
            return ""

    async def _radio_group_label(self, radio: Locator) -> str:
        try:
            return str(
                await radio.evaluate(
                    """element => {
                        const root = element.closest('fieldset, [data-test-form-element], div');
                        if (!root) return '';
                        const label = root.querySelector('legend, [data-test-form-element-label]');
                        return ((label && (label.innerText || label.textContent)) || '').trim();
                    }"""
                )
                or await self._get_associated_label(radio)
            ).strip()
        except Exception:
            return await self._get_associated_label(radio)

    async def _is_required(self, element: Locator, label: str = "") -> bool:
        if (
            await element.get_attribute("required") is not None
            or (await element.get_attribute("aria-required") or "").lower() == "true"
            or label.strip().endswith("*")
        ):
            return True
        try:
            return bool(
                await element.evaluate(
                    """element => {
                        const root = element.closest(
                            'fieldset, [data-test-form-element], .jobs-easy-apply-form-element, .fb-dash-form-element'
                        );
                        if (!root) return false;
                        if (root.querySelector('[aria-required="true"], .required, [data-required="true"]')) return true;
                        const text = (root.innerText || '').trim();
                        return /(^|\\s)\\*(\\s|$)/.test(text);
                    }"""
                )
            )
        except Exception:
            return False

    async def _select_has_value(self, select: Locator) -> bool:
        value = (await select.input_value()).strip()
        if not value:
            return False
        selected = select.locator("option:checked")
        text = (
            (await selected.inner_text()).strip().lower()
            if await selected.count()
            else ""
        )
        return not self._looks_like_placeholder(text)

    async def _select_matching_option(self, select: Locator, answer: str) -> bool:
        options = select.locator("option")
        for index in range(await options.count()):
            option = options.nth(index)
            text = (await option.inner_text()).strip()
            value = await option.get_attribute("value") or ""
            if self._answers_match(answer, f"{text} {value}"):
                await select.select_option(index=index)
                return True
        return False

    async def _wait_for_easy_apply_modal(
        self, timeout: int = 10_000
    ) -> Optional[Locator]:
        return await wait_for_linkedin_modal(self.page, timeout)

    async def _find_submit_button(self, modal: Locator) -> Optional[Locator]:
        return await self._first_visible(
            modal.locator(
                "button[aria-label*='Submit application' i], "
                "button[data-control-name*='submit' i], "
                "button:has-text('Submit application')"
            )
        )

    async def _find_advance_button(self, modal: Locator) -> Optional[Locator]:
        return await self._first_visible(
            modal.locator(
                "button[aria-label*='Review' i], button:has-text('Review your application'), "
                "button[aria-label*='Continue to next step' i], "
                "button[data-easy-apply-next-button], button:has-text('Continue'), "
                "button:has-text('Next'), button:has-text('Review')"
            )
        )

    async def _security_interruption(self) -> str:
        url = self.page.url.lower()
        if any(part in url for part in ("/checkpoint/", "/challenge/", "/authwall")):
            return "LinkedIn security checkpoint detected"
        visible = await self._first_visible(
            self.page.locator(
                "iframe[src*='captcha' i], [id*='captcha' i], "
                "[class*='captcha' i], form[action*='checkpoint' i]"
            )
        )
        return "CAPTCHA or account verification detected" if visible else ""

    async def _wait_for_manual_security_completion(self, timeout_seconds: int) -> bool:
        """Observe a manual challenge; never solve, bypass, or interact with it."""
        if timeout_seconds <= 0:
            return False
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        while asyncio.get_running_loop().time() < deadline:
            if not await self._security_interruption():
                if await self._wait_for_easy_apply_modal(timeout=1_000) is not None:
                    return True
                easy_apply = await self._first_visible(
                    self.page.locator(
                        "button.jobs-apply-button, "
                        "button[aria-label*='Easy Apply' i], "
                        "button:has-text('Easy Apply')"
                    )
                )
                if easy_apply is not None:
                    await easy_apply.click()
                    if await self._wait_for_easy_apply_modal(timeout=5_000) is not None:
                        return True
            await asyncio.sleep(1)
        return False

    async def _validation_errors(self, modal: Locator) -> List[str]:
        return await self._visible_texts(
            modal.locator(
                "[role='alert'], .artdeco-inline-feedback__message, "
                "[data-test-form-element-error-messages]"
            )
        )

    async def _pause_for_validation(
        self,
        errors: List[str],
        job_title: str,
        company: str,
        job_url: str,
        step: int,
        resume_used: str,
        log,
        set_state,
    ) -> Dict[str, Any]:
        questions = [
            self._question(text, "LinkedIn validation error", "validation_error")
            for text in errors
        ]
        db_manager.log_flagged_job(
            job_title,
            company,
            job_url,
            "LinkedIn form validation prevented progress",
            questions,
        )
        await set_state(
            ApplicationState.NEEDS_USER_INPUT,
            reason="LinkedIn form validation prevented progress",
            questions=questions,
            step=step,
            resume_used=resume_used,
        )
        await log(
            "LinkedIn displayed a validation error. The live application remains open for review."
        )
        return {
            "status": "flagged",
            "reason": "validation_error",
            "questions": questions,
            "preserve_modal": True,
            "resume_used": resume_used,
        }

    async def _step_signature(self, modal: Locator) -> str:
        try:
            return str(
                await modal.evaluate(
                    """root => {
                        const visible = element => !!(element.offsetWidth || element.offsetHeight || element.getClientRects().length);
                        const fields = Array.from(root.querySelectorAll(
                            'input:not([type=hidden]), textarea, select, [role=combobox], button'
                        )).filter(visible).map(element => [
                            element.tagName, element.type || '', element.name || '', element.id || '',
                            element.getAttribute('aria-label') || '', (element.innerText || '').trim()
                        ].join(':'));
                        const progress = root.querySelector('[role=progressbar]');
                        return (progress ? progress.getAttribute('aria-valuenow') || '' : '') + '|' + fields.join('|');
                    }"""
                )
            )
        except Exception:
            return ""

    async def _wait_for_step_change(self, modal: Locator, old_signature: str) -> bool:
        for _ in range(34):
            await asyncio.sleep(0.15)
            try:
                if not await modal.is_visible():
                    return True
                if await self._step_signature(modal) != old_signature:
                    return True
            except Exception:
                return True
        return False

    @staticmethod
    def _field_type(tag: str, input_type: str, role: Optional[str]) -> str:
        if role == "combobox" and tag != "select":
            return "combobox"
        if tag == "textarea":
            return "textarea"
        if tag == "select":
            return "select"
        return input_type or "text"

    @classmethod
    def _semantic_field_type(cls, label: str, control_type: str) -> str:
        normalized = " ".join(label.lower().split())
        if control_type == "file" and any(
            cls._label_contains(normalized, token) for token in ("resume", "résumé", "cv")
        ):
            return "resume_upload"
        for semantic, phrases in (
            ("postal_code", ("postal", "zip")),
            ("street_address", ("street address", "address line")),
            ("city", ("city",)),
            ("state", ("state", "province")),
        ):
            if any(cls._label_contains(normalized, phrase) for phrase in phrases):
                return semantic
        return control_type

    @staticmethod
    def _looks_like_placeholder(value: str) -> bool:
        normalized = " ".join(str(value).lower().split())
        return not normalized or normalized in {
            "select an option",
            "please select",
            "choose an option",
            "select",
            "choose",
        }

    @classmethod
    def _answers_match(cls, answer: str, candidate: str) -> bool:
        answer_clean = cls._normalise_comparison(answer)
        candidate_clean = cls._normalise_comparison(candidate)
        return bool(answer_clean) and (
            answer_clean == candidate_clean
            or answer_clean in candidate_clean
            or candidate_clean in answer_clean
        )

    @staticmethod
    def _normalise_comparison(value: str) -> str:
        return re.sub(r"[^a-z0-9]+", " ", str(value).lower()).strip()

    @staticmethod
    async def _normalise_input_answer(field: Locator, answer: str) -> str:
        field_type = (await field.get_attribute("type") or "").lower()
        if field_type == "number":
            match = re.search(r"\d+(?:\.\d+)?", answer.replace(",", ""))
            return match.group(0) if match else ""
        if field_type == "date":
            match = re.fullmatch(r"\d{4}-\d{2}-\d{2}", answer.strip())
            return match.group(0) if match else ""
        return answer

    async def _cancel_modal(self, modal: Locator) -> None:
        dismiss = await self._first_visible(
            modal.locator(
                "button[aria-label='Dismiss'], button.artdeco-modal__dismiss"
            )
        )
        if dismiss is None:
            return
        await dismiss.click()
        confirm = await self._first_visible(
            self.page.locator(
                "button[data-control-name='discard_application_confirm_btn'], "
                "button:has-text('Discard')"
            )
        )
        if confirm is not None:
            await confirm.click()

    async def _wait_for_submission_confirmation(self, original_modal: Locator) -> bool:
        confirmation_pattern = re.compile(
            r"application (?:was )?(?:sent|submitted)|your application was sent",
            re.IGNORECASE,
        )
        for _ in range(16):
            confirmation = await self._first_visible(
                self.page.get_by_text(confirmation_pattern)
            )
            if confirmation is not None:
                return True
            applied_button = await self._first_visible(
                self.page.locator(
                    "button[aria-label*='Applied'], button:has-text('Applied')"
                )
            )
            if applied_button is not None:
                text = (
                    f"{await applied_button.inner_text()} "
                    f"{await applied_button.get_attribute('aria-label') or ''}"
                ).lower()
                if "applied" in text and "easy apply" not in text:
                    return True
            await asyncio.sleep(0.5)
        return False

    @staticmethod
    def _unresolved_required_fields(
        fields: List[ApplicationField],
    ) -> List[Dict[str, Any]]:
        return [
            FormFiller._question(field.label, field.key, field.field_type)
            for field in fields
            if field.required and not field.current_value
        ]

    @staticmethod
    def _question(label: str, fallback: str, field_type: str) -> Dict[str, Any]:
        return {
            "question": label.strip() or fallback,
            "field_type": field_type,
            "required": True,
        }

    @staticmethod
    def _deduplicate_questions(
        questions: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        result = []
        seen = set()
        for question in questions:
            key = (
                question.get("question", "").strip().lower(),
                question.get("field_type"),
            )
            if key not in seen:
                seen.add(key)
                result.append(question)
        return result

    @staticmethod
    def _short_label(label: str) -> str:
        cleaned = " ".join(label.split())
        return cleaned[:80] + ("..." if len(cleaned) > 80 else "")

    @staticmethod
    def _css_string(value: str) -> str:
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'

    @staticmethod
    async def _first_visible(locator: Locator) -> Optional[Locator]:
        for index in range(await locator.count()):
            candidate = locator.nth(index)
            if await candidate.is_visible():
                return candidate
        return None

    @classmethod
    async def _visible_texts(cls, locator: Locator) -> List[str]:
        result = []
        for index in range(await locator.count()):
            candidate = locator.nth(index)
            if await candidate.is_visible():
                text = (await candidate.inner_text()).strip()
                if text and text not in result:
                    result.append(text)
        return result
