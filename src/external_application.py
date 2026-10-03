"""State machine for external ATS applications."""

import asyncio
import json
from typing import Any, Awaitable, Callable, Dict, Optional

from playwright.async_api import Page

from src.application import ApplicationState
from src.ats.base import ATSAdapter
from src.ats.registry import AdapterRegistry
from src.config import config_manager
from src.database import db_manager
from src.models import JobRecord
from src.runtime import runtime_status


ApprovalCallback = Optional[Callable[[Dict[str, Any]], Awaitable[Dict[str, Any]]]]
StateCallback = Optional[
    Callable[[ApplicationState, Dict[str, Any]], Awaitable[None]]
]


class ExternalApplicationRunner:
    MAX_STEPS = 25

    def __init__(self, page: Page):
        self.page = page

    async def run(
        self,
        job: JobRecord,
        log,
        approval_callback: ApprovalCallback = None,
        state_callback: StateCallback = None,
    ) -> Dict[str, Any]:
        if job.source_mode == "search_save":
            raise ValueError("Search & Save jobs cannot enter application automation")
        async def state(value: ApplicationState, **details: Any) -> None:
            if state_callback:
                await state_callback(value, details)

        adapter = await AdapterRegistry.create(self.page, log)
        job.ats_type = adapter.name
        job.application_url = self.page.url
        await self._event(log, job, "adapter_detected")
        await state(
            ApplicationState.APPLICATION_STARTED,
            ats=adapter.name,
            application_url=self.page.url,
        )

        text = (await self.page.locator("body").inner_text()).lower()
        if any(phrase in text for phrase in ("no longer accepting applications", "position has been filled",
                                             "this job has expired", "job is no longer available")):
            await state(ApplicationState.FAILED, reason="application_closed", ats=adapter.name)
            return {"status": "incomplete", "reason": "application_closed", "ats": adapter.name}
        inspection = await adapter.inspect()
        job.application_url = self.page.url
        for _ in range(3):
            detected = await AdapterRegistry.create(self.page, log)
            if detected.name == adapter.name:
                break
            adapter = detected
            job.ats_type = adapter.name
            job.application_url = self.page.url
            await self._event(log, job, "adapter_redirect_detected")
            inspection = await adapter.inspect()
        manual_reason = inspection.get("manual_reason", "")
        if manual_reason:
            await state(
                ApplicationState.NEEDS_USER_INPUT,
                reason=manual_reason,
                ats=adapter.name,
                application_url=self.page.url,
            )
            await self._event(log, job, "manual_security_check", unresolved=1)
            if not await self._wait_for_manual_completion(adapter):
                return {
                    "status": "flagged",
                    "reason": manual_reason,
                    "preserve_page": True,
                    "ats": adapter.name,
                }
            inspection = await adapter.inspect()

        if not inspection.get("form_found"):
            reason = "No supported application form was found on the external page"
            await state(ApplicationState.FAILED, reason=reason, ats=adapter.name)
            await self._event(log, job, "form_not_found", failure=reason)
            return {"status": "incomplete", "reason": "form_not_found", "ats": adapter.name}

        resume_used = ""
        for step in range(1, self.MAX_STEPS + 1):
            manual_reason = await adapter.manual_intervention_reason()
            if manual_reason:
                await state(ApplicationState.NEEDS_USER_INPUT, reason=manual_reason, ats=adapter.name,
                            application_url=self.page.url)
                return {"status": "flagged", "reason": manual_reason,
                        "preserve_page": True, "ats": adapter.name}
            detected = await AdapterRegistry.create(self.page, log)
            if detected.name != adapter.name:
                adapter = detected
                job.ats_type = adapter.name
                await self._event(log, job, "adapter_step_redirect", step=step)
            job.application_url = self.page.url
            fields = await adapter.extract_fields()
            await state(
                ApplicationState.FORM_ANALYZED,
                ats=adapter.name,
                application_url=self.page.url,
                step=step,
                field_count=len(fields),
                fields=[field.as_dict() for field in fields],
            )
            fill_result = await adapter.fill_fields()
            resume_used = fill_result.get("resume_used") or resume_used
            unresolved = fill_result.get("unresolved") or []
            resolved_count = int(fill_result.get("resolved_count", 0))
            await state(
                ApplicationState.FILLING,
                ats=adapter.name,
                application_url=self.page.url,
                step=step,
                field_count=len(fields),
                fields_resolved=resolved_count,
                unresolved_count=len(unresolved),
                resume_used=resume_used,
            )
            await self._event(
                log,
                job,
                "fields_processed",
                step=step,
                detected=len(fields),
                resolved=resolved_count,
                unresolved=len(unresolved),
            )

            if unresolved:
                reason = f"Requires user input on {len(unresolved)} external form question(s)"
                db_manager.log_flagged_job(
                    job.title, job.company, job.job_url, reason, unresolved
                )
                await state(
                    ApplicationState.NEEDS_USER_INPUT,
                    reason=reason,
                    ats=adapter.name,
                    application_url=self.page.url,
                    step=step,
                    questions=unresolved,
                    resume_used=resume_used,
                )
                await self._event(
                    log, job, "needs_user_input", step=step, unresolved=len(unresolved)
                )
                return {
                    "status": "flagged",
                    "reason": "unanswered_required_questions",
                    "questions": unresolved,
                    "resume_used": resume_used,
                    "preserve_page": True,
                    "ats": adapter.name,
                }

            if await adapter.is_final_step():
                details = {
                    "job_title": job.title,
                    "company": job.company,
                    "job_url": job.job_url,
                    "application_url": self.page.url,
                    "location": job.location,
                    "job_type": job.employment_type,
                    "application_system": adapter.name,
                    "ats": adapter.name,
                    "required_fields_complete": True,
                    "resume_uploaded": bool(resume_used),
                    "resume_used": resume_used,
                    "final_submit_detected": True,
                    "field_count": len(fields),
                    "step_count": step,
                }
                await state(ApplicationState.READY_FOR_REVIEW, **details)
                await self._event(log, job, "ready_for_review", step=step)
                if approval_callback is None:
                    return {
                        "status": "ready_for_review",
                        "preserve_page": True,
                        **details,
                    }
                approval = await approval_callback(details)
                review_id = approval.get("review_id")
                decision = approval.get("decision")
                if decision == "rejected":
                    await state(
                        ApplicationState.SKIPPED,
                        reason="User rejected the prepared external application",
                        ats=adapter.name,
                    )
                    return {"status": "rejected", "review_id": review_id, "ats": adapter.name}
                if decision != "approved":
                    return {
                        "status": "incomplete",
                        "reason": "approval_timeout",
                        "review_id": review_id,
                        "preserve_page": True,
                        "ats": adapter.name,
                    }
                await adapter.submit()
                if not await adapter.submission_confirmed():
                    reason = "External ATS did not confirm submission"
                    await state(ApplicationState.FAILED, reason=reason, ats=adapter.name)
                    return {
                        "status": "incomplete",
                        "reason": "submission_unconfirmed",
                        "review_id": review_id,
                        "ats": adapter.name,
                    }
                return {
                    "status": "submitted",
                    "review_id": review_id,
                    "resume_used": resume_used,
                    "ats": adapter.name,
                }

            advanced = await adapter.next_step()
            if not advanced:
                errors = await adapter.validate()
                if errors:
                    questions = [
                        {"question": error, "field_type": "validation_error", "required": True}
                        for error in errors
                    ]
                    reason = "External ATS validation prevented progress"
                    db_manager.log_flagged_job(
                        job.title, job.company, job.job_url, reason, questions
                    )
                    await state(
                        ApplicationState.NEEDS_USER_INPUT,
                        reason=reason,
                        ats=adapter.name,
                        questions=questions,
                        step=step,
                    )
                    return {
                        "status": "flagged",
                        "reason": "validation_error",
                        "questions": questions,
                        "preserve_page": True,
                        "ats": adapter.name,
                    }
                reason = f"{adapter.name} form did not advance from step {step}"
                await state(ApplicationState.FAILED, reason=reason, ats=adapter.name)
                await self._event(log, job, "navigation_failed", step=step, failure=reason)
                return {"status": "incomplete", "reason": "step_did_not_advance", "ats": adapter.name}

        reason = f"External application exceeded the {self.MAX_STEPS}-step safety limit"
        await state(ApplicationState.FAILED, reason=reason, ats=adapter.name)
        return {"status": "incomplete", "reason": "too_many_steps", "ats": adapter.name}

    async def _wait_for_manual_completion(self, adapter: ATSAdapter) -> bool:
        timeout_minutes = max(
            1,
            int(
                config_manager.profile.get("work_preferences", {}).get(
                    "approval_timeout_minutes", 15
                )
            ),
        )
        deadline = asyncio.get_running_loop().time() + timeout_minutes * 60
        while asyncio.get_running_loop().time() < deadline:
            if not await adapter.manual_intervention_reason():
                return True
            await asyncio.sleep(1)
        return False

    @staticmethod
    async def _event(
        log,
        job: JobRecord,
        action: str,
        step: int = 0,
        detected: int = 0,
        resolved: int = 0,
        unresolved: int = 0,
        failure: str = "",
    ) -> None:
        runtime_status.update(
            current_job=job.title,
            company=job.company,
            current_ats=job.ats_type,
            current_action=action.replace("_", " ").title(),
            current_step=step,
            fields_detected=detected,
            fields_resolved=resolved,
            unresolved_questions=unresolved,
        )
        payload = {
            "job": job.title,
            "company": job.company,
            "ats": job.ats_type,
            "action": action,
            "step": step,
            "fields_detected": detected,
            "fields_resolved": resolved,
            "unresolved": unresolved,
        }
        if failure:
            payload["failure"] = failure
        await log("ATS_EVENT " + json.dumps(payload, ensure_ascii=False))
