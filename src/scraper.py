"""LinkedIn discovery with separate save-only and application workflows."""

import asyncio
import re
import traceback
import logging
import urllib.parse
from uuid import uuid4
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Optional, Set

from playwright.async_api import Locator, Page, TimeoutError as PlaywrightTimeoutError

from src.application import ApplicationState
from src.config import config_manager
from src.database import db_manager
from src.evaluation import JobEvaluator, RuleBasedJobEvaluator
from src.external_application import ExternalApplicationRunner
from src.external_navigation import ExternalApplicationLauncher, ExternalPageSession
from src.form_filler import FormFiller
from src.job_extractor import LinkedInJobExtractor
from src.models import JobRecord, current_timestamp
from src.review import review_coordinator
from src.runtime import runtime_status
from src.scoring import JobMatchScorer
from src.paths import DATA_DIR


LogCallback = Optional[Callable[[str], Awaitable[None]]]

WORKPLACE_FILTERS = {
    "on-site": "1",
    "onsite": "1",
    "office": "1",
    "remote": "2",
    "hybrid": "3",
}

EMPLOYMENT_FILTERS = {
    "full-time": "F",
    "full time": "F",
    "part-time": "P",
    "part time": "P",
    "contract": "C",
    "temporary": "T",
    "internship": "I",
    "volunteer": "V",
    "other": "O",
}

EXPERIENCE_FILTERS = {
    "internship": "1",
    "entry level": "2",
    "associate": "3",
    "mid-senior level": "4",
    "director": "5",
    "executive": "6",
}

RESULT_CONTAINER_SELECTORS = (
    ".jobs-search-results-list",
    ".scaffold-layout__list-container",
    "ul.jobs-search__job-details--container",
)

RESULT_CARD_LINK_SELECTOR = (
    "li.jobs-search-results__list-item a[href*='/jobs/view/'], "
    "li[data-occludable-job-id] a[href*='/jobs/view/'], "
    ".job-card-container a[href*='/jobs/view/']"
)

CLOSED_PHRASES = (
    "no longer accepting applications",
    "this job is no longer available",
    "job is no longer available",
    "this job has expired",
    "position has been filled",
)


class LinkedInScraper:
    """Search LinkedIn and drive one resilient serial application worker."""

    def __init__(self, page: Page, evaluator: Optional[JobEvaluator] = None):
        self.page = page
        self.form_filler = FormFiller(page)
        self.job_extractor = LinkedInJobExtractor(page)
        self.evaluator = evaluator or RuleBasedJobEvaluator()
        self.match_scorer = JobMatchScorer(self.evaluator)
        self._seen_job_keys: Set[str] = set()
        self._active_external_session: Optional[ExternalPageSession] = None

    async def run_autopilot(self, log_callback: LogCallback = None) -> Dict[str, Any]:
        async def log(message: str) -> None:
            print(f"[AUTOPILOT] {message}")
            if log_callback:
                await log_callback(message)

        prefs = config_manager.profile.get("work_preferences", {})
        runtime_status.reset()
        roles = self._clean_values(prefs.get("target_roles"))
        locations = self._clean_values(prefs.get("target_locations"))
        if not roles or not locations:
            raise ValueError("Save at least one target role and location in Profile before starting a session.")
        workplace_types = self._normalise_workplace_types(
            prefs.get("workplace_types") or ["Remote", "Hybrid", "On-site"]
        )
        employment_types = self._normalise_employment_types(
            prefs.get("job_types") or ["Full-time"]
        )
        experience_level = str(prefs.get("experience_level", "Any")).strip()
        # Application method never limits job discovery, including legacy profiles.
        easy_apply_only = False
        max_apps = max(1, int(prefs.get("max_applications_per_run", 25)))
        delay_sec = max(2, int(prefs.get("delay_between_applications_sec", 5)))
        max_pages = min(10, max(1, int(prefs.get("max_search_pages_per_query", 3))))

        totals = {"applied": 0, "flagged": 0, "skipped": 0, "failed": 0}
        await log(
            "Starting LinkedIn-only autopilot for "
            f"{', '.join(employment_types)} / {', '.join(workplace_types)} jobs "
            f"(limit: {max_apps} applications)."
        )

        await self._ensure_linkedin_ready(log)
        browser_paused = False
        for role in roles:
            if totals["applied"] >= max_apps or browser_paused:
                break
            for location in locations:
                if totals["applied"] >= max_apps or browser_paused:
                    break
                jobs = await self._search_linkedin(
                    role,
                    location,
                    workplace_types,
                    employment_types,
                    experience_level,
                    easy_apply_only,
                    max_pages,
                    log,
                )
                browser_paused = await self._process_jobs(
                    jobs=jobs,
                    totals=totals,
                    max_apps=max_apps,
                    delay_sec=delay_sec,
                    roles=roles,
                    prefs=prefs,
                    log=log,
                    log_callback=log_callback,
                )

        report_paths = await asyncio.to_thread(db_manager.export_job_reports)
        completion_message = (
            "LinkedIn run paused on the live application. "
            if browser_paused
            else "LinkedIn run complete. "
        )
        await log(
            completion_message
            +
            f"Applied: {totals['applied']}, Flagged: {totals['flagged']}, "
            f"Skipped: {totals['skipped']}, Failed: {totals['failed']}. "
            f"Reports: {report_paths['reviewed'].name} and {report_paths['applied'].name}"
        )
        return {
            "applied_count": totals["applied"],
            "flagged_count": totals["flagged"],
            "skipped_count": totals["skipped"],
            "failed_count": totals["failed"],
            "browser_paused": browser_paused,
            "reviewed_jobs_report": str(report_paths["reviewed"]),
            "applied_jobs_report": str(report_paths["applied"]),
        }

    async def run_search_only(self, log_callback: LogCallback = None) -> Dict[str, int]:
        """Discover and persist matches; never enter the application processor."""
        async def log(message: str) -> None:
            if log_callback:
                await log_callback(message)

        prefs = config_manager.profile.get("work_preferences", {})
        roles = self._clean_values(prefs.get("target_roles"))
        locations = self._clean_values(prefs.get("target_locations"))
        if not roles or not locations:
            raise ValueError("Save at least one target role and location before searching.")
        workplace = self._normalise_workplace_types(
            prefs.get("workplace_types") or ["Remote", "Hybrid", "On-site"]
        )
        employment = self._normalise_employment_types(prefs.get("job_types") or ["Full-time"])
        max_pages = min(10, max(1, int(prefs.get("max_search_pages_per_query", 3))))
        totals = {"saved": 0, "skipped": 0, "failed": 0}
        run_id = uuid4().hex
        self._seen_job_keys.clear()
        runtime_status.reset()
        await log("Starting LinkedIn Search & Save Jobs. Search only; no applications will be opened.")
        await self._ensure_linkedin_ready(log)
        for role in roles:
            for location in locations:
                search_query = f"{role} in {location}"
                jobs = await self._search_linkedin(
                    role, location, workplace, employment,
                    str(prefs.get("experience_level", "Any")).strip(),
                    False, max_pages, log,
                )
                for job in jobs:
                    job.source_mode = "search_save"
                    job.job_url = self._normalise_linkedin_job_url(job.job_url)
                    if not job.job_url:
                        continue
                    job.external_job_id = LinkedInJobExtractor.job_id_from_url(job.job_url)
                    key = job.external_job_id or job.job_url
                    if key in self._seen_job_keys:
                        continue
                    self._seen_job_keys.add(key)
                    if db_manager.is_job_known(
                        job.job_url, job.external_job_id, job.title, job.company, job.location,
                        retry_legacy_search_rejections=True,
                    ):
                        db_manager.save_search_issue(job, run_id, search_query, "Duplicate",
                                                     "Job already exists in saved jobs or history")
                        totals["skipped"] += 1
                        continue
                    runtime_status.update(current_job=job.title, company=job.company,
                                          current_ats="LinkedIn", current_action="Reading job (search only)")
                    stage = "Extraction Failed"
                    try:
                        await self.page.goto(job.job_url, wait_until="domcontentloaded", timeout=30_000)
                        self._raise_for_login_gate()
                        await self.job_extractor.extract_for_search(job)
                        stage = "Processing Error"
                        page_text = await self._main_text()
                        job.mark_reviewed()
                        runtime_status.increment("jobs_reviewed")
                        reason = self._closed_reason(page_text)
                        if reason:
                            self._save_outcome(job, "CLOSED", reason)
                            db_manager.save_search_issue(job, run_id, search_query, "Closed", reason)
                            totals["skipped"] += 1
                            continue
                        if await self._has_applied_indicator():
                            self._save_outcome(job, "ALREADY_APPLIED", "LinkedIn shows this job as already applied")
                            db_manager.save_search_issue(job, run_id, search_query, "Duplicate", "LinkedIn shows this job as already applied")
                            totals["skipped"] += 1
                            continue
                        assessment = await self.match_scorer.evaluate_search(
                            job.title, job.company, job.description, roles, prefs,
                            config_manager.profile,
                            {"location": job.location, "workplace_type": job.workplace_type,
                             "experience_level": job.experience_level},
                        )
                        job.notes = self._match_notes(assessment)
                        if not assessment["matches"]:
                            self._save_outcome(job, "NOT_QUALIFIED", "; ".join(assessment["reasons"]), assessment["score"])
                            db_manager.save_search_issue(job, run_id, search_query, "Low Match", "; ".join(assessment["reasons"]))
                            totals["skipped"] += 1
                            continue
                        status = "POSSIBLE_MATCH" if assessment["classification"] == "Possible Match" else "SAVED_FOR_REVIEW"
                        self._save_outcome(job, status, match_score=assessment["score"])
                        totals["saved"] += 1
                        await log(f"Saved for review: {job.title} at {job.company} (not applied).")
                    except Exception as exc:
                        # Failed extraction never receives a match score/category.
                        if stage == "Extraction Failed":
                            job.match_score = None
                        self._save_outcome(job, "FAILED", self._error_text(exc))
                        db_manager.save_search_issue(job, run_id, search_query, stage, self._error_text(exc))
                        # Login/security gates stop the run instead of cycling through jobs.
                        self._raise_for_login_gate()
                        totals["failed"] += 1
                        runtime_status.increment("failures")
                        await log(f"Search could not read {job.job_url}: {self._error_text(exc)}")
                    await asyncio.sleep(1)
        await log(f"Search & Save complete. Saved: {totals['saved']}, skipped: {totals['skipped']}, failed: {totals['failed']}.")
        return totals

    async def _ensure_linkedin_ready(self, log) -> None:
        if "linkedin.com" not in self.page.url:
            await log("Opening LinkedIn Jobs...")
            await self.page.goto(
                "https://www.linkedin.com/jobs/",
                wait_until="domcontentloaded",
                timeout=30_000,
            )
        await self.page.wait_for_load_state("domcontentloaded", timeout=15_000)
        self._raise_for_login_gate()

    async def _search_linkedin(
        self,
        role: str,
        location: str,
        workplace_types: List[str],
        employment_types: List[str],
        experience_level: str,
        easy_apply_only: bool,
        max_pages: int,
        log,
    ) -> List[JobRecord]:
        workplace_values = [WORKPLACE_FILTERS[value.lower()] for value in workplace_types]
        employment_values = [EMPLOYMENT_FILTERS[value.lower()] for value in employment_types]
        collected: Dict[str, JobRecord] = {}

        await log(
            f"LinkedIn search: '{role}' in '{location}' "
            f"({', '.join(employment_types)}; {', '.join(workplace_types)}; "
            f"{'Easy Apply only' if easy_apply_only else 'Easy Apply + external ATS'})."
        )

        for page_index in range(max_pages):
            query = {
                "keywords": role,
                "location": location,
                "f_WT": ",".join(workplace_values),
                "f_JT": ",".join(employment_values),
                "start": str(page_index * 25),
            }
            if easy_apply_only:
                query["f_AL"] = "true"
            experience_code = EXPERIENCE_FILTERS.get(experience_level.lower())
            if experience_code:
                query["f_E"] = experience_code
            search_url = "https://www.linkedin.com/jobs/search/?" + urllib.parse.urlencode(query)
            try:
                await self.page.goto(
                    search_url,
                    wait_until="domcontentloaded",
                    timeout=30_000,
                )
            except Exception as exc:
                await log(f"Could not load LinkedIn results page {page_index + 1}: {self._error_text(exc)}")
                break

            self._raise_for_login_gate()
            try:
                await self.page.locator(RESULT_CARD_LINK_SELECTOR).first.wait_for(state="attached", timeout=10_000)
            except PlaywrightTimeoutError:
                await log("LinkedIn result cards did not appear within 10 seconds; checking available results.")
            page_jobs = await self._collect_result_records()
            new_jobs = [job for job in page_jobs if job.job_url not in collected]
            if not new_jobs:
                if page_index == 0:
                    await log("No scoped LinkedIn result cards appeared for this search.")
                break

            for job in new_jobs:
                collected[job.job_url] = job
            await log(
                f"Collected {len(new_jobs)} jobs from results page {page_index + 1} "
                f"({len(collected)} total)."
            )
            if len(new_jobs) < 10:
                break

        return list(collected.values())

    async def _collect_result_records(self) -> List[JobRecord]:
        container = await self._first_visible_selector(RESULT_CONTAINER_SELECTORS)
        if container is None:
            # This fallback is still restricted to LinkedIn job-card elements;
            # it intentionally does not scrape every job link on the page.
            return await self._records_from_card_links(
                self.page.locator(RESULT_CARD_LINK_SELECTOR)
            )

        records: Dict[str, JobRecord] = {}
        stable_passes = 0
        for _ in range(8):
            before = len(records)
            links = container.locator("a[href*='/jobs/view/']")
            for record in await self._records_from_card_links(links):
                existing = records.get(record.job_url)
                if existing is None:
                    records[record.job_url] = record
                else:
                    existing.title = existing.title or record.title
                    existing.company = existing.company or record.company
                    existing.location = existing.location or record.location

            cards = container.locator("[data-job-id], [data-occludable-job-id]")
            for index in range(await cards.count()):
                card = cards.nth(index)
                raw_id = (
                    await card.get_attribute("data-job-id")
                    or await card.get_attribute("data-occludable-job-id")
                    or ""
                )
                match = re.search(r"(\d{6,})", raw_id)
                if match:
                    url = f"https://www.linkedin.com/jobs/view/{match.group(1)}/"
                    if url not in records:
                        records[url] = JobRecord(
                            job_url=url,
                            external_job_id=match.group(1),
                            source="LinkedIn",
                        )

            stable_passes = stable_passes + 1 if len(records) == before else 0
            if stable_passes >= 2:
                break
            try:
                await container.evaluate("element => { element.scrollTop = element.scrollHeight; }")
            except Exception:
                break
            await asyncio.sleep(0.7)
        return list(records.values())

    async def _records_from_card_links(self, links: Locator) -> List[JobRecord]:
        records: Dict[str, JobRecord] = {}
        for index in range(min(await links.count(), 150)):
            link = links.nth(index)
            href = await link.get_attribute("href") or ""
            normalised = self._normalise_linkedin_job_url(href)
            if not normalised:
                continue
            record = records.setdefault(
                normalised,
                JobRecord(
                    job_url=normalised,
                    external_job_id=LinkedInJobExtractor.job_id_from_url(normalised),
                    source="LinkedIn",
                ),
            )
            if not record.title:
                record.title = " ".join(
                    (
                        await link.get_attribute("aria-label")
                        or await link.get_attribute("title")
                        or await link.inner_text()
                        or ""
                    ).split()
                )
                record.title = LinkedInJobExtractor.clean_title(record.title)

            card = link.locator(
                "xpath=ancestor::*[self::li or @data-job-id or @data-occludable-job-id][1]"
            )
            if await card.count():
                record.company = record.company or await self._first_text(
                    card.locator(
                        ".artdeco-entity-lockup__subtitle, "
                        ".job-card-container__primary-description, "
                        "[data-test-job-card-company-name]"
                    )
                )
                locations = await self._visible_texts(
                    card.locator(
                        ".artdeco-entity-lockup__caption, "
                        ".job-card-container__metadata-item, "
                        "[data-test-job-card-location]"
                    )
                )
                record.location = record.location or LinkedInJobExtractor._choose_location(
                    locations
                )
        return list(records.values())

    async def _process_jobs(
        self,
        jobs: Iterable[JobRecord],
        totals: Dict[str, int],
        max_apps: int,
        delay_sec: int,
        roles: List[str],
        prefs: Dict[str, Any],
        log,
        log_callback: LogCallback,
    ) -> bool:
        pending = list(jobs)
        for index, job in enumerate(pending, start=1):
            if totals["applied"] >= max_apps:
                await log(f"Reached the run limit of {max_apps} submitted applications.")
                return False

            job.job_url = self._normalise_linkedin_job_url(job.job_url)
            job.external_job_id = (
                job.external_job_id
                or LinkedInJobExtractor.job_id_from_url(job.job_url)
            )
            job_key = job.external_job_id or job.job_url
            if not job.job_url:
                continue
            if job_key in self._seen_job_keys:
                await log(f"Duplicate skipped within this run: {job.job_url}")
                continue
            if db_manager.is_job_known(
                job.job_url,
                job.external_job_id,
                job.title,
                job.company,
                job.location,
            ):
                await log(
                    f"Duplicate/history match skipped: {job.title or job.job_url}"
                )
                continue
            self._seen_job_keys.add(job_key)
            outcome = "failed"
            preserve_modal = False

            try:
                result = await self._review_job(
                    job=job,
                    roles=roles,
                    prefs=prefs,
                    log=log,
                    log_callback=log_callback,
                )
                outcome = result["status"]
                preserve_modal = bool(result.get("preserve_modal"))
                if outcome == "applied":
                    totals["applied"] += 1
                elif outcome == "needs_review":
                    totals["flagged"] += 1
                elif outcome == "failed":
                    totals["failed"] += 1
                else:
                    totals["skipped"] += 1
            except Exception as exc:
                totals["failed"] += 1
                stage = job.status or "UNKNOWN"
                timestamp = current_timestamp()
                trace = traceback.format_exc()
                try:
                    artifact_note = await self._capture_failure_artifacts(job, stage)
                except OSError as artifact_error:
                    artifact_note = f"Diagnostics could not be saved: {artifact_error}"
                details = self._error_text(exc)
                job.status = "FAILED"
                job.failure_reason = details
                job.notes = (
                    f"Stage: {stage}\nTimestamp: {timestamp}\nException: {details}\n"
                    f"{artifact_note}\nTraceback:\n{trace}"
                ).strip()
                try:
                    db_manager.save_job_record(job)
                except Exception as history_error:
                    await log(f"History write failed for {job.job_url}: {self._error_text(history_error)}. Check storage permissions/free space.")
                runtime_status.increment("failures")
                runtime_status.update(current_action="Failed")
                await log(f"Could not process {job.job_url} at {stage}: {details}")
            finally:
                if not preserve_modal:
                    for cleanup in (self._dismiss_open_easy_apply_modal, self._close_external_application):
                        try:
                            await cleanup()
                        except Exception as cleanup_error:
                            await log(f"Browser cleanup failed for {job.job_url}: {self._error_text(cleanup_error)}")

            if preserve_modal:
                await log(
                    "The browser is paused on this application. Resolve it manually or "
                    "stop the session before starting another job."
                )
                return True

            if index < len(pending) and totals["applied"] < max_apps:
                wait_seconds = delay_sec if outcome == "applied" else 1
                await log(
                    f"Continuing to the next LinkedIn job in {wait_seconds} second"
                    f"{'s' if wait_seconds != 1 else ''}."
                )
                await asyncio.sleep(wait_seconds)
        return False

    async def _review_job(
        self,
        job: JobRecord,
        roles: List[str],
        prefs: Dict[str, Any],
        log,
        log_callback: LogCallback,
    ) -> Dict[str, Any]:
        if job.source_mode == "search_save":
            raise ValueError("Search & Save jobs cannot enter application automation")
        runtime_status.update(current_job=job.title, company=job.company,
                              current_ats="LinkedIn", current_action="Opening job",
                              current_step=0, fields_detected=0, fields_resolved=0,
                              unresolved_questions=0)
        await self.page.goto(job.job_url, wait_until="domcontentloaded", timeout=30_000)
        self._raise_for_login_gate()
        try:
            await self.page.locator("h1, .job-details-jobs-unified-top-card__job-title").first.wait_for(state="visible", timeout=10_000)
        except PlaywrightTimeoutError:
            await log(f"Job heading did not appear for {job.job_url}; using available metadata fallbacks.")

        await self.job_extractor.extract(job)
        job.mark_reviewed()
        db_manager.save_job_record(job)
        runtime_status.increment("jobs_reviewed")
        runtime_status.update(
            current_job=job.title,
            company=job.company,
            current_ats="LinkedIn",
            current_action="Reviewing job",
        )
        page_text = await self._main_text()
        title_for_log = job.title or "Title unavailable"
        company_for_log = job.company or "Company unavailable"
        await log(
            f"Reviewed: {title_for_log} at {company_for_log}"
            + (f" — {job.location}" if job.location else "")
        )

        closed_reason = self._closed_reason(page_text)
        if closed_reason:
            self._save_outcome(job, "CLOSED", closed_reason)
            await log(f"Skipped {title_for_log}: {closed_reason}. Moving on immediately.")
            return {"status": "closed", "reason": closed_reason}

        if await self._has_applied_indicator():
            reason = "LinkedIn shows this job as already applied"
            self._save_outcome(job, "ALREADY_APPLIED", reason)
            await log(f"Skipped {title_for_log}: already applied.")
            return {"status": "already_applied", "reason": reason}

        assessment = await self.match_scorer.evaluate(
            job.title,
            job.company,
            job.description or page_text,
            roles,
            prefs,
            config_manager.profile,
            {
                "location": job.location,
                "workplace_type": job.workplace_type,
                "experience_level": job.experience_level,
            },
        )
        job.notes = self._match_notes(assessment)
        if not assessment["matches"]:
            reason = "; ".join(assessment["reasons"])
            self._save_outcome(job, "NOT_QUALIFIED", reason, assessment["score"])
            await log(f"Skipped {title_for_log}: preference check failed ({reason}).")
            return {"status": "rejected_by_preferences", "reason": reason}

        await log(f"Preference match score: {assessment['score']}/100.")
        self._save_outcome(job, "REVIEWED", match_score=assessment["score"])
        apply_control = await ExternalApplicationLauncher._find_external_apply(self.page)
        easy_apply = await self._first_visible(
            self.page.locator(
                "button.jobs-apply-button:has-text('Easy Apply'), "
                "a.jobs-apply-button:has-text('Easy Apply'), "
                "button[aria-label*='Easy Apply' i], a[aria-label*='Easy Apply' i]"
            )
        )
        apply_control = easy_apply or apply_control
        if apply_control is None:
            reason = "No active LinkedIn application control"
            job.easy_apply = False
            job.external_apply = False
            job.application_method = "unavailable"
            self._save_outcome(job, "SKIPPED", reason, assessment["score"])
            await log(f"Skipped {title_for_log}: {reason}.")
            return {"status": "no_easy_apply", "reason": reason}

        if db_manager.is_job_blocked(job.job_url):
            await log(f"Skipped {title_for_log}: job is marked Applied or Rejected.")
            return {"status": "skipped", "reason": "saved job status"}
        session = await ExternalApplicationLauncher.open(self.page, apply_control)
        if not session.linkedin_application:
            return await self._run_external_application(
                job, apply_control, assessment["score"], prefs, log, session=session
            )
        job.easy_apply = True
        job.external_apply = False
        job.application_method = "easy_apply"
        job.ats_type = "LinkedIn Easy Apply"
        await self._record_application_state(
            job, ApplicationState.APPLICATION_STARTED, {}, assessment["score"]
        )
        await log(f"LinkedIn application modal opened for {title_for_log}.")
        result = await self.form_filler.fill_easy_apply_form(
            job.title,
            job.company,
            job.job_url,
            location=job.location,
            job_type=job.employment_type,
            log_callback=log_callback,
            approval_callback=lambda details: self._await_user_approval(
                details,
                assessment["score"],
                prefs,
                log,
            ),
            state_callback=lambda state, details: self._record_application_state(
                job, state, details, assessment["score"]
            ),
        )
        form_status = result.get("status", "incomplete")
        review_id = result.get("review_id")
        if result.get("resume_used"):
            job.resume_used = str(result["resume_used"])
        if form_status == "submitted":
            if review_id is not None:
                db_manager.resolve_application_review(
                    review_id, "submitted", "Application submission confirmed"
                )
            job.date_applied = current_timestamp()
            self._save_outcome(job, "APPLIED", match_score=assessment["score"])
            await log(f"Application submitted: {title_for_log}.")
            return {"status": "applied", "reason": "submitted"}
        if form_status == "flagged":
            reason = result.get("reason", "unanswered required field")
            self._save_outcome(job, "NEEDS_USER_INPUT", reason, assessment["score"])
            await log(f"Needs review: {title_for_log} ({reason}).")
            return {
                "status": "needs_review",
                "reason": reason,
                "preserve_modal": bool(result.get("preserve_modal")),
            }
        if form_status == "ready_for_review":
            return {
                "status": "ready_for_review",
                "reason": "final submit detected",
                "preserve_modal": True,
            }
        if form_status == "rejected":
            reason = "User rejected the prepared application"
            self._save_outcome(job, "SKIPPED", reason, assessment["score"])
            await log(f"Application rejected by user: {title_for_log}.")
            return {"status": "rejected_by_user", "reason": reason}

        reason = result.get("reason", "application did not complete")
        if review_id is not None and reason != "approval_timeout":
            db_manager.resolve_application_review(
                review_id, "submission_failed", reason
            )
        self._save_outcome(job, "FAILED", reason, assessment["score"])
        await log(
            f"Application did not complete for {title_for_log} ({reason}); moving on."
        )
        return {"status": "failed", "reason": reason}

    async def _run_external_application(
        self,
        job: JobRecord,
        apply_control: Locator,
        match_score: int,
        prefs: Dict[str, Any],
        log,
        session=None,
    ) -> Dict[str, Any]:
        """Launch and process an external ATS while preserving LinkedIn."""
        if job.source_mode == "search_save":
            raise ValueError("Search & Save jobs cannot enter application automation")
        job.easy_apply = False
        job.external_apply = True
        job.application_method = "external"
        job.ats_type = "Detecting external ATS"
        self._save_outcome(job, "APPLICATION_FOUND", match_score=match_score)
        await log(f"Opening external application for {job.title} at {job.company}.")
        session = session or await ExternalApplicationLauncher.open(self.page, apply_control)
        self._active_external_session = session
        job.application_url = session.application_page.url
        if db_manager.is_application_url_known(job.application_url, job.job_url):
            reason = "External application URL matches a previously processed job"
            self._save_outcome(job, "DUPLICATE", reason, match_score)
            await log(f"Skipped duplicate external application: {job.application_url}")
            return {"status": "duplicate", "reason": reason}

        db_manager.save_job_record(job)
        runner = ExternalApplicationRunner(session.application_page)
        result = await runner.run(
            job,
            log,
            approval_callback=lambda details: self._await_user_approval(
                details, match_score, prefs, log
            ),
            state_callback=lambda state, details: self._record_application_state(
                job, state, details, match_score
            ),
        )
        job.application_url = session.application_page.url
        if result.get("ats"):
            job.ats_type = str(result["ats"])
        if result.get("resume_used"):
            job.resume_used = str(result["resume_used"])
        form_status = result.get("status", "incomplete")
        review_id = result.get("review_id")
        if form_status == "submitted":
            if review_id is not None:
                db_manager.resolve_application_review(
                    review_id, "submitted", "External application submission confirmed"
                )
            job.date_applied = current_timestamp()
            self._save_outcome(job, "APPLIED", match_score=match_score)
            return {"status": "applied", "reason": "submitted"}
        if form_status == "flagged":
            reason = result.get("reason", "external application requires user input")
            self._save_outcome(job, "NEEDS_USER_INPUT", reason, match_score)
            return {
                "status": "needs_review",
                "reason": reason,
                "preserve_modal": bool(result.get("preserve_page")),
            }
        if form_status == "ready_for_review":
            return {
                "status": "ready_for_review",
                "reason": "external final submit detected",
                "preserve_modal": True,
            }
        if form_status == "rejected":
            reason = "User rejected the prepared external application"
            self._save_outcome(job, "SKIPPED", reason, match_score)
            return {"status": "rejected_by_user", "reason": reason}

        reason = result.get("reason", "external application did not complete")
        if review_id is not None and reason != "approval_timeout":
            db_manager.resolve_application_review(review_id, "submission_failed", reason)
        self._save_outcome(job, "FAILED", reason, match_score)
        return {
            "status": "failed",
            "reason": reason,
            "preserve_modal": bool(result.get("preserve_page")),
        }

    @staticmethod
    async def _record_application_state(
        job: JobRecord,
        state: ApplicationState,
        details: Dict[str, Any],
        match_score: int,
    ) -> None:
        """Persist form progress before navigation or a human-review wait."""
        previous_status = job.status
        job.status = state.value
        job.match_score = match_score
        if details.get("application_url"):
            job.application_url = str(details["application_url"])
        if details.get("ats"):
            job.ats_type = str(details["ats"])
        prior_started = bool(job.date_application_started)
        if state == ApplicationState.APPLICATION_STARTED and not prior_started:
            job.date_application_started = current_timestamp()
            runtime_status.increment("applications_started")
        if state == ApplicationState.READY_FOR_REVIEW and previous_status != "READY_FOR_REVIEW":
            runtime_status.increment("ready_for_review")
        if state == ApplicationState.FAILED and previous_status != "FAILED":
            runtime_status.increment("failures")
        if details.get("resume_used"):
            job.resume_used = str(details["resume_used"])
        if details.get("reason"):
            job.failure_reason = str(details["reason"])
        elif state in {
            ApplicationState.APPLICATION_STARTED,
            ApplicationState.FORM_ANALYZED,
            ApplicationState.FILLING,
            ApplicationState.READY_FOR_REVIEW,
        }:
            job.failure_reason = ""
        if state == ApplicationState.READY_FOR_REVIEW:
            job.notes = (
                "Required fields complete: Yes; "
                f"Resume uploaded: {'Yes' if details.get('resume_uploaded') else 'No/not requested'}; "
                f"Final submit detected: {'Yes' if details.get('final_submit_detected') else 'No'}; "
                f"Application steps: {details.get('step_count', '')}"
            )
        elif state == ApplicationState.NEEDS_USER_INPUT:
            questions = details.get("questions") or []
            job.notes = "; ".join(
                str(question.get("question", "")) for question in questions
                if question.get("question")
            )
        runtime_status.update(
            current_job=job.title,
            company=job.company,
            current_ats=job.ats_type,
            current_action=state.value.replace("_", " ").title(),
            current_step=int(details.get("step", 0) or 0),
            fields_detected=int(details.get("field_count", 0) or 0),
            fields_resolved=int(details.get("fields_resolved", 0) or 0),
            unresolved_questions=int(details.get("unresolved_count", 0) or 0),
        )
        db_manager.save_job_record(job)

    async def _capture_failure_artifacts(self, job: JobRecord, stage: str) -> str:
        """Best-effort local diagnostics for unexpected browser failures."""
        folder = DATA_DIR / "failures"
        folder.mkdir(parents=True, exist_ok=True)
        identity = job.external_job_id or "job"
        stamp = re.sub(r"[^0-9]", "", current_timestamp())[:14]
        prefix = folder / f"{identity}_{re.sub(r'[^A-Za-z0-9_-]', '_', stage)}_{stamp}"
        created = []
        try:
            screenshot_path = prefix.with_suffix(".png")
            artifact_page = (
                self._active_external_session.application_page
                if self._active_external_session
                and not self._active_external_session.application_page.is_closed()
                else self.page
            )
            await artifact_page.screenshot(path=str(screenshot_path), full_page=True)
            created.append(str(screenshot_path))
        except Exception as exc:
            created.append(f"Screenshot unavailable: {self._error_text(exc)}")
        try:
            html_path = prefix.with_suffix(".html")
            artifact_page = (
                self._active_external_session.application_page
                if self._active_external_session
                and not self._active_external_session.application_page.is_closed()
                else self.page
            )
            html_path.write_text(await artifact_page.content(), encoding="utf-8")
            created.append(str(html_path))
        except Exception as exc:
            created.append(f"HTML unavailable: {self._error_text(exc)}")
        return "Diagnostics: " + ", ".join(created) if created else ""

    @staticmethod
    def _save_outcome(
        job: JobRecord,
        status: str,
        failure_reason: str = "",
        match_score: Optional[int] = None,
    ) -> None:
        job.status = status
        job.failure_reason = failure_reason
        if match_score is not None:
            job.match_score = match_score
        db_manager.save_job_record(job)

    async def _await_user_approval(
        self,
        details: Dict[str, Any],
        match_score: int,
        prefs: Dict[str, Any],
        log,
    ) -> Dict[str, Any]:
        """Pause the serial browser worker at Submit until the dashboard decides."""
        if db_manager.is_job_blocked(details["job_url"]):
            return {"decision": "rejected"}
        review_id = db_manager.queue_application_review(
            job_title=details["job_title"],
            company=details["company"],
            job_url=details["job_url"],
            location=details.get("location", ""),
            job_type=details.get("job_type", ""),
            application_system=details.get("application_system", "LinkedIn Easy Apply"),
            match_score=match_score,
            required_fields_complete=bool(details.get("required_fields_complete")),
            resume_uploaded=bool(details.get("resume_uploaded")),
            resume_used=str(details.get("resume_used", "")),
            application_url=str(details.get("application_url", "")),
            final_submit_detected=bool(details.get("final_submit_detected")),
            field_count=details.get("field_count"),
            step_count=details.get("step_count"),
        )
        waiter = review_coordinator.create_waiter(review_id)
        timeout_minutes = max(1, int(prefs.get("approval_timeout_minutes", 15)))
        await log(
            f"Approval required for {details['job_title']} at {details['company']}. "
            f"Review request #{review_id} is waiting in the dashboard for up to "
            f"{timeout_minutes} minute{'s' if timeout_minutes != 1 else ''}."
        )
        try:
            decision = await asyncio.wait_for(waiter, timeout=timeout_minutes * 60)
            return {"review_id": review_id, "decision": decision}
        except asyncio.TimeoutError:
            db_manager.resolve_application_review(
                review_id,
                "expired",
                f"No dashboard decision within {timeout_minutes} minute(s)",
            )
            return {"review_id": review_id, "decision": "expired"}
        finally:
            review_coordinator.finish(review_id)

    async def _close_external_application(self) -> None:
        if self._active_external_session is None:
            return
        await self._active_external_session.close()
        self._active_external_session = None

    @classmethod
    def evaluate_job_match(
        cls,
        title: str,
        company: str,
        description: str,
        roles: List[str],
        prefs: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Compatibility wrapper around the replaceable evaluator provider."""
        return RuleBasedJobEvaluator().evaluate(
            title, company, description, roles, prefs
        )

    @staticmethod
    def _match_notes(assessment: Dict[str, Any]) -> str:
        parts = []
        for label, key in (
            ("Matched", "matched_requirements"),
            ("Missing", "missing_requirements"),
            ("Notes", "important_notes"),
        ):
            values = [str(item).strip() for item in assessment.get(key, []) if str(item).strip()]
            if values:
                parts.append(f"{label}: {', '.join(values)}")
        source = str(assessment.get("score_source", "")).strip()
        if source:
            parts.append(f"Score source: {source}")
        return " | ".join(parts)

    async def _dismiss_open_easy_apply_modal(self) -> None:
        modal = await self._first_visible(self.page.locator(FormFiller.MODAL_SELECTOR))
        if modal is None:
            return
        dismiss = await self._first_visible(
            modal.locator(
                "button[aria-label='Dismiss'], button.artdeco-modal__dismiss, "
                "button:has-text('Done')"
            )
        )
        if dismiss is None:
            return
        try:
            await dismiss.click(timeout=3_000)
            await asyncio.sleep(0.4)
            discard = await self._first_visible(
                self.page.locator(
                    "button[data-control-name='discard_application_confirm_btn'], "
                    "button:has-text('Discard')"
                )
            )
            if discard is not None:
                await discard.click(timeout=3_000)
        except Exception:
            logging.getLogger(__name__).exception("Could not dismiss Easy Apply modal")

    async def _has_applied_indicator(self) -> bool:
        indicator = await self._first_visible(
            self.page.locator(
                "button[aria-label*='Applied'], button:has-text('Applied')"
            )
        )
        if indicator is None:
            return False
        text = f"{await indicator.inner_text()} {await indicator.get_attribute('aria-label') or ''}"
        return "applied" in text.lower() and "easy apply" not in text.lower()

    async def _main_text(self) -> str:
        main = await self._first_visible(self.page.locator("main"))
        if main is None:
            main = await self._first_visible(self.page.locator("body"))
        if main is None:
            return ""
        try:
            return (await main.inner_text(timeout=5_000))[:80_000]
        except Exception:
            return ""

    async def _first_visible_selector(self, selectors: Iterable[str]) -> Optional[Locator]:
        for selector in selectors:
            candidate = await self._first_visible(self.page.locator(selector))
            if candidate is not None:
                return candidate
        return None

    def _raise_for_login_gate(self) -> None:
        if any(token in self.page.url for token in ("/login", "/checkpoint", "/authwall")):
            raise RuntimeError(
                "LinkedIn requires a login or security check. Complete it in the browser and start again."
            )

    @staticmethod
    def _closed_reason(page_text: str) -> str:
        normalised = page_text.lower()
        return next((phrase for phrase in CLOSED_PHRASES if phrase in normalised), "")

    @classmethod
    def _detect_label(cls, text: str, allowed_labels: List[str]) -> str:
        normalised = cls._normalise_text(text)
        for label in allowed_labels:
            if cls._normalise_text(label) in normalised:
                return label
        return ", ".join(allowed_labels)

    @staticmethod
    def _clean_values(values: Any) -> List[str]:
        if not isinstance(values, list):
            return []
        return [str(value).strip() for value in values if str(value).strip()]

    @staticmethod
    def _normalise_workplace_types(values: Any) -> List[str]:
        labels = {"1": "On-site", "2": "Remote", "3": "Hybrid"}
        result: List[str] = []
        for value in LinkedInScraper._clean_values(values):
            code = WORKPLACE_FILTERS.get(value.lower())
            if code and labels[code] not in result:
                result.append(labels[code])
        return result or ["Remote", "Hybrid", "On-site"]

    @staticmethod
    def _normalise_employment_types(values: Any) -> List[str]:
        labels = {
            "F": "Full-time",
            "P": "Part-time",
            "C": "Contract",
            "T": "Temporary",
            "I": "Internship",
            "V": "Volunteer",
            "O": "Other",
        }
        result: List[str] = []
        for value in LinkedInScraper._clean_values(values):
            code = EMPLOYMENT_FILTERS.get(value.lower())
            if code and labels[code] not in result:
                result.append(labels[code])
        return result or ["Full-time"]

    @staticmethod
    def _normalise_linkedin_job_url(url: str) -> str:
        decoded = urllib.parse.unquote(url)
        match = re.search(
            r"(?:https?://(?:[a-z]{2,3}\.)?linkedin\.com)?"
            r"/jobs/view/(?:[^/?#]+-)?(\d+)",
            decoded,
            flags=re.IGNORECASE,
        )
        return f"https://www.linkedin.com/jobs/view/{match.group(1)}/" if match else ""

    @staticmethod
    def _normalise_text(value: str) -> str:
        return re.sub(r"[^a-z0-9]+", " ", str(value).lower()).strip()

    @staticmethod
    def _error_text(exc: Exception) -> str:
        details = str(exc).strip()
        return f"{type(exc).__name__}: {details}" if details else type(exc).__name__

    @staticmethod
    async def _first_visible(locator: Locator) -> Optional[Locator]:
        for index in range(await locator.count()):
            candidate = locator.nth(index)
            if await candidate.is_visible():
                return candidate
        return None

    @classmethod
    async def _first_text(cls, locator: Locator) -> str:
        candidate = await cls._first_visible(locator)
        return (await candidate.inner_text()).strip() if candidate else ""

    @staticmethod
    async def _visible_texts(locator: Locator) -> List[str]:
        values: List[str] = []
        for index in range(min(await locator.count(), 30)):
            candidate = locator.nth(index)
            if await candidate.is_visible():
                value = " ".join((await candidate.inner_text()).split()).strip()
                if value and value not in values:
                    values.append(value)
        return values
