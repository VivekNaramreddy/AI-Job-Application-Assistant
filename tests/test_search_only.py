"""Offline search-only integration: real server/browser, fixture LinkedIn pages."""

import asyncio
import html
import json
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlparse

import uvicorn
from playwright.async_api import async_playwright

import src.database as database_module
import src.main as main_module
import src.scraper as scraper_module
from src.config import config_manager
from src.scraper import LinkedInScraper
from src.job_extractor import LinkedInJobExtractor
from src.models import JobRecord
from src.scoring import JobMatchScorer


class SearchOnlyTests(unittest.IsolatedAsyncioTestCase):
    async def save_rejection(self):
        await self.ui.locator('.rejection-editor select').select_option('position_closed')
        await self.ui.get_by_role('button', name='Save rejection', exact=True).click()

    async def test_rejection_editor_validation_editing_and_history(self):
        job = JobRecord(job_url="https://www.linkedin.com/jobs/view/999000/",
                        title="Analyst", status="SAVED_FOR_REVIEW", source_mode="search_save")
        self.db.save_job_record(job)
        await self.ui.reload()
        await self.ui.locator('[data-tab="tab-reviews"]').click()
        await self.ui.locator('#review-container select[data-job-url]').select_option('Rejected')
        await self.ui.get_by_role('button', name='Save rejection', exact=True).click()
        self.assertEqual(self.db.get_dashboard_jobs()[0]['job_status'], 'Pending')
        response = await self.ui.request.post(self.base + '/api/jobs/status', data={'job_url': job.job_url, 'status': 'Rejected'})
        self.assertEqual(response.status, 409)
        notes = 'Posting says remote, but description requires 3 days onsite.'
        await self.ui.locator('[name="rejection_feedback"]').fill(notes)
        await self.ui.evaluate('loadDashboardJobs()')
        self.assertEqual(await self.ui.locator('[name="rejection_feedback"]').input_value(), notes)
        await self.ui.route('**/api/jobs/status', lambda route: route.fulfill(
            status=500, content_type='application/json', body='{"detail":"Test save failure"}'))
        await self.save_rejection()
        await self.ui.locator('.rejection-error', has_text='Test save failure').wait_for()
        self.assertEqual(self.db.get_dashboard_jobs()[0]['job_status'], 'Pending')
        self.assertEqual(await self.ui.locator('[name="rejection_feedback"]').input_value(), notes)
        await self.ui.unroute('**/api/jobs/status')
        await self.save_rejection()
        await self.ui.wait_for_function("document.querySelector('#badge-rejected').textContent === '1'")
        await self.ui.reload()
        await self.ui.locator('[data-tab="tab-rejected"]').click()
        await self.ui.get_by_role('button', name='Edit rejection').click()
        self.assertEqual(await self.ui.locator('[name="rejection_feedback"]').input_value(), notes)
        await self.ui.locator('[name="rejection_reason"]').select_option('work_arrangement_not_suitable')
        await self.ui.locator('[name="rejection_feedback"]').fill('')
        await self.ui.get_by_role('button', name='Save rejection', exact=True).click()
        await self.ui.locator('.rejection-summary', has_text='Work arrangement not suitable').wait_for()
        await self.ui.locator('#rejected-container select[data-job-url]').select_option('Pending')
        await self.ui.wait_for_function("document.querySelector('#badge-rejected').textContent === '0'")
        row = self.db.get_dashboard_jobs()[0]
        self.assertEqual(row['rejection_reason'], 'work_arrangement_not_suitable')
        self.assertEqual(row['rejection_feedback'], '')
        self.assertEqual(await self.ui.locator('.rejection-summary').count(), 0)
        self.assertEqual(self.errors, [])

    async def asyncSetUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        folder = Path(temporary.name)
        paths = patch.multiple(database_module, DB_FILE=folder / "jobs.db", EXPORTS_DIR=folder)
        paths.start()
        self.addCleanup(paths.stop)
        self.db = database_module.DatabaseManager()
        for module in (main_module, scraper_module):
            patcher = patch.object(module, "db_manager", self.db)
            patcher.start()
            self.addCleanup(patcher.stop)
        profile = patch.object(config_manager, "profile", {
            "work_preferences": {"target_roles": ["Analyst", "Data Analyst"],
                                 "target_locations": ["Remote"], "easy_apply_only": True,
                                 "max_search_pages_per_query": 2,
                                 "excluded_keywords": ["commission only"]},
            "ai_settings": {"enabled": False},
        })
        profile.start()
        self.addCleanup(profile.stop)
        self.playwright = await async_playwright().start()
        self.addAsyncCleanup(self.playwright.stop)
        self.browser = await self.playwright.chromium.launch(headless=True)
        self.addAsyncCleanup(self.browser.close)
        self.context = await self.browser.new_context()
        self.clicks = []
        await self.context.expose_binding("recordClick", lambda source, text: self.clicks.append(text))
        await self.context.add_init_script("document.addEventListener('click', e => window.recordClick(e.target.textContent));")
        self.site = await self.context.new_page()
        self.urls = []
        await self.site.route("**/*", self.fixture)
        browser_patch = patch.object(main_module.browser_manager, "get_active_page", AsyncMock(return_value=self.site))
        browser_patch.start()
        self.addCleanup(browser_patch.stop)
        self.forbidden = []
        for target, name in ((LinkedInScraper, "_review_job"), (LinkedInScraper, "_process_jobs"),
                             (scraper_module.ExternalApplicationLauncher, "open"),
                             (scraper_module.FormFiller, "fill_easy_apply_form")):
            mock = AsyncMock(side_effect=AssertionError("Search entered application workflow"))
            patcher = patch.object(target, name, mock)
            patcher.start()
            self.addCleanup(patcher.stop)
            self.forbidden.append(mock)
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        self.addCleanup(sock.close)
        self.base = f"http://127.0.0.1:{sock.getsockname()[1]}"
        self.server = uvicorn.Server(uvicorn.Config(main_module.app, log_level="error"))
        self.server_task = asyncio.create_task(self.server.serve(sockets=[sock]))
        self.addAsyncCleanup(self.stop_server)
        async with asyncio.timeout(15):
            while not self.server.started:
                if self.server_task.done():
                    self.server_task.result()
                await asyncio.sleep(0.02)
        self.ui = await self.browser.new_page(viewport={"width": 1440, "height": 1000})
        await self.ui.route("https://**/*", lambda route: route.abort())
        self.errors = []
        self.ui.on("pageerror", lambda error: self.errors.append(str(error)))
        await self.ui.goto(self.base)

    async def stop_server(self):
        self.server.should_exit = True
        await asyncio.wait_for(self.server_task, 15)

    async def fixture(self, route):
        url = route.request.url
        self.urls.append(url)
        self.assertEqual(urlparse(url).hostname, "www.linkedin.com")
        if "/jobs/search/" in url:
            query = parse_qs(urlparse(url).query)
            self.assertNotIn("f_AL", query, "Legacy Easy Apply preferences must not restrict discovery")
            self.assertEqual(query["location"], ["Remote"])
            ids = range(100001, 100011) if query["start"] == ["0"] else [100001, 100011]
            html = "<main>" + "".join(
                f'<li class="jobs-search-results__list-item"><a href="/jobs/view/{job_id}/?trackingId=x">Data Analyst</a></li>'
                for job_id in ids
            ) + "</main>"
        elif "/jobs/view/" in url:
            job_id = int(urlparse(url).path.rstrip("/").split("/")[-1])
            description = ("Analyze data and business requirements, prepare detailed reports, collaborate with "
                           "the analytics team and present recommendations to stakeholders. Full-time Remote.")
            if 100003 <= job_id <= 100009:
                description += " commission only"
            if job_id == 100010:
                description += " No longer accepting applications"
            if getattr(self, "failure_mode", False) and job_id == 100009:
                description = "Loading..."
            if getattr(self, "failure_mode", False) and job_id == 100008:
                description += " scorer-error"
            apply = ('<button class="jobs-apply-button" aria-label="Easy Apply">Easy Apply</button>'
                     if job_id != 100002 else '<a class="jobs-apply-button" href="https://ats.example.test/apply">Apply</a>')
            if job_id == 100011:
                apply = ""
            html = f'''<main><h1>Data Analyst</h1>
                <div class="job-details-jobs-unified-top-card__company-name">Company {job_id}</div>
                <div class="job-details-jobs-unified-top-card__primary-description-container"><span>Remote</span></div>
                <div class="jobs-description__content">{description}</div>{apply}</main>'''
        else:
            html = "<main>LinkedIn Jobs</main>"
        await route.fulfill(status=200, content_type="text/html", body=html)

    async def wait_search(self):
        async with asyncio.timeout(40):
            while main_module.search_running:
                await asyncio.sleep(0.05)
        await main_module.search_task

    async def test_search_saves_matches_without_any_application_actions(self):
        self.assertTrue(await self.ui.locator("#btn-start").is_visible())
        await self.ui.locator("#btn-search-save").click()
        async with asyncio.timeout(10):
            while main_module.search_task is None:
                await asyncio.sleep(0.01)
        await self.wait_search()
        saved = self.db.get_saved_jobs()
        self.assertEqual({job["external_job_id"] for job in saved}, {"100001", "100002", "100011"})
        self.assertTrue(any("start=25" in url for url in self.urls))
        self.assertTrue(all(job["description"] and job["match_score"] for job in saved))
        self.assertTrue(all(not job["date_application_started"] and not job["date_applied"] for job in saved))
        by_id = {job["external_job_id"]: job for job in saved}
        self.assertEqual(by_id["100001"]["application_method"], "easy_apply")
        self.assertEqual(by_id["100002"]["application_method"], "external")
        self.assertEqual(by_id["100011"]["application_method"], "unavailable")
        self.assertEqual(by_id["100002"]["application_url"], "https://ats.example.test/apply")
        self.assertTrue(all(job["source_mode"] == "search_save" and job["application_stage"] == "not_started" for job in saved))
        self.assertTrue(all(job["tracking_status"] == "Pending" for job in saved))
        self.assertTrue(all(not job["automation_failure_reason"] for job in saved))
        self.assertEqual(self.db.get_application_reviews(), [])
        self.assertEqual(self.db.get_applied_jobs(), [])
        await self.ui.locator('[data-tab="tab-reviews"]').click()
        await self.ui.locator("#btn-refresh-reviews").click()
        await self.ui.wait_for_function("document.querySelectorAll('.saved-job-card').length === 3")
        self.assertEqual(await self.ui.locator(".saved-job-card .review-actions").count(), 0)
        external = next(job for job in self.db.get_dashboard_jobs() if job["external_job_id"] == "100002")
        self.assertEqual((external["bucket"], external["job_status"]), ("review", "Pending"))
        await self.ui.locator(".saved-job-card summary").first.click()
        self.assertIn("Analyze data", await self.ui.locator(".saved-job-card").first.inner_text())
        response = await self.ui.request.post(self.base + "/api/search/start")
        self.assertEqual(response.status, 200)
        await self.wait_search()
        self.assertEqual(len(self.db.get_saved_jobs()), 3)
        issues = self.db.get_search_issues()
        duplicates = [job for job in issues if job["reason"] == "Duplicate"]
        self.assertEqual(len(duplicates), 11)
        self.assertEqual(len({(job["run_id"], job["job_url"]) for job in issues}), len(issues))
        self.assertTrue(all(job["company"] and job["description"] for job in duplicates))
        views = [url for url in self.urls if "/jobs/view/" in url]
        self.assertEqual(len(views), 11, "Each job is read once across duplicate queries and repeated runs")
        for mock in self.forbidden:
            mock.assert_not_called()
        self.assertEqual(self.clicks, [])
        self.assertEqual(len(self.context.pages), 1)
        self.assertEqual(self.errors, [])

    async def test_failures_are_persistent_distinct_and_do_not_run_matching(self):
        self.failure_mode = True
        evaluated = []
        original_evaluate = JobMatchScorer.evaluate
        original_extract = LinkedInJobExtractor.extract_for_search

        async def evaluate(scorer, title, company, description, *args):
            evaluated.append(description)
            if "scorer-error" in description:
                raise RuntimeError("Fixture scoring failure")
            return await original_evaluate(scorer, title, company, description, *args)

        async def extract(extractor, job):
            return await original_extract(extractor, job, timeout=2)

        ids = [100001, 100009, 100008, 100003, 100009]
        async def collect(*args, **kwargs):
            return [JobRecord(job_url=f"https://www.linkedin.com/jobs/view/{job_id}/") for job_id in ids]

        with patch.object(LinkedInScraper, "_search_linkedin", collect), \
                patch.object(JobMatchScorer, "evaluate", evaluate), \
                patch.object(LinkedInJobExtractor, "extract_for_search", extract):
            response = await self.ui.request.post(self.base + "/api/search/start")
            self.assertEqual(response.status, 200)
            await self.wait_search()

        self.assertEqual(len(evaluated), 3)
        self.assertTrue(all("Loading" not in description for description in evaluated))
        issues = self.db.get_search_issues()
        self.assertEqual(len(issues), 3)
        by_reason = {job["reason"]: job for job in issues}
        self.assertEqual(set(by_reason), {"Extraction Failed", "Processing Error", "Low Match"})
        self.assertIsNone(by_reason["Extraction Failed"]["match_score"])
        self.assertIsNotNone(by_reason["Low Match"]["match_score"])
        self.assertTrue(all(job["search_query"] and job["occurred_at"] and job["error"] for job in issues))
        self.assertEqual(len(self.db.get_saved_jobs()), 1)
        self.db.recover_interrupted_applications()
        self.assertEqual(database_module.DatabaseManager().get_search_issues(), issues)
        self.assertFalse(self.db.is_job_known("https://www.linkedin.com/jobs/view/100009/"))
        await self.ui.locator('[data-tab="tab-search-issues"]').click()
        await self.ui.locator("#btn-refresh-search-issues").click()
        await self.ui.wait_for_function("document.querySelectorAll('.search-issue-card').length === 3")
        self.assertEqual(await self.ui.locator('.search-issue-card a[href*="linkedin.com/jobs/view/"]').count(), 3)
        for mock in self.forbidden:
            mock.assert_not_called()
        self.assertEqual(self.clicks, [])
        self.assertEqual(self.errors, [])

    async def test_synthetic_legacy_rejection_is_reconsidered_and_shown_as_possible_match(self):
        fixture = json.loads((Path(__file__).parent / "fixtures/synthetic_search_match.json").read_text(encoding="utf-8"))
        job = fixture["job"]
        record = JobRecord(job_url=job["job_url"], external_job_id=job["external_job_id"],
                           title=job["job_title"], company=job["company"], status="NOT_QUALIFIED",
                           match_score=61, failure_reason="title does not match a target role; match score 40 is below minimum 60",
                           notes="Score source: deterministic profile-aware")
        self.db.save_job_record(record)
        self.assertTrue(self.db.is_job_known(record.job_url))
        self.assertFalse(self.db.is_job_known(record.job_url, retry_legacy_search_rejections=True))

        async def listing(route):
            await route.fulfill(content_type="text/html", body=f'''<main>
                <h1>{html.escape(job['job_title'])}</h1>
                <div class="job-details-jobs-unified-top-card__company-name">Example Company</div>
                <div class="job-details-jobs-unified-top-card__primary-description-container"><span>United States</span></div>
                <div class="jobs-description__content">{html.escape(job['description'])}</div>
                <button class="jobs-apply-button">Easy Apply</button></main>''')
        await self.site.route("**/jobs/view/9000000001/", listing)
        profile = {**fixture["profile"], "work_preferences": fixture["preferences"]}
        with patch.object(config_manager, "profile", profile), patch.object(
            LinkedInScraper, "_search_linkedin", AsyncMock(return_value=[JobRecord(job_url=job["job_url"])])
        ):
            response = await self.ui.request.post(self.base + "/api/search/start")
            self.assertEqual(response.status, 200)
            await self.wait_search()
        saved = self.db.get_saved_jobs()
        self.assertEqual(len(saved), 1)
        self.assertEqual((saved[0]["status"], saved[0]["match_score"]), ("POSSIBLE_MATCH", 90))
        self.assertEqual(saved[0]["failure_reason"], "")
        self.assertEqual(self.db.get_search_issues(), [])
        self.assertTrue(self.db.is_job_known(record.job_url, retry_legacy_search_rejections=True))
        await self.ui.locator('[data-tab="tab-reviews"]').click()
        await self.ui.locator("#btn-refresh-reviews").click()
        await self.ui.get_by_text("Possible Match — Not Applied", exact=False).wait_for()
        self.assertIn("90/100", await self.ui.locator(".saved-job-card").inner_text())
        for mock in self.forbidden:
            mock.assert_not_called()
        self.assertEqual(self.clicks, [])
        self.assertEqual(self.db.get_applied_jobs(), [])
        self.assertEqual(self.errors, [])

    async def test_manual_status_updates_both_tabs_and_survives_server_restart(self):
        for job_id, origin in ((700001, "POSSIBLE_MATCH"), (700002, "SAVED_FOR_REVIEW")):
            job = JobRecord(job_url=f"https://www.linkedin.com/jobs/view/{job_id}/",
                            title="Analyst", company="Example", status=origin)
            self.db.save_job_record(job)
            self.db.save_search_issue(job, "manual-test", "Analyst", "Duplicate")
        await self.ui.reload()
        await self.ui.locator('[data-tab="tab-reviews"]').click()
        applied = self.ui.locator('.saved-job-card select[data-job-url$="700001/"]')
        self.assertEqual(await applied.input_value(), "Pending")
        await applied.select_option("Applied")
        await self.ui.wait_for_function("document.querySelector('#review-container select[data-job-url$=\"700001/\"]') === null && document.querySelector('#badge-applied').textContent === '1'")
        rejected = self.ui.locator('#review-container select[data-job-url$="700002/"]')
        self.assertEqual(await rejected.input_value(), "Pending")
        await rejected.select_option("Rejected")
        await self.save_rejection()
        await self.ui.wait_for_function("document.querySelector('#review-container select') === null && document.querySelector('#badge-rejected').textContent === '1'")
        before = {row["job_url"]: row["status_changed_at"] for row in self.db.get_saved_jobs()}
        self.assertTrue(all(before.values()))
        self.assertEqual(len(self.db.get_saved_jobs()), 2)
        self.assertEqual(len(self.db.get_search_issues()), 2)

        await self.ui.close()
        await self.stop_server()
        self.db = database_module.DatabaseManager()
        main_module.db_manager = self.db
        scraper_module.db_manager = self.db
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        self.addCleanup(sock.close)
        self.base = f"http://127.0.0.1:{sock.getsockname()[1]}"
        self.server = uvicorn.Server(uvicorn.Config(main_module.app, log_level="error"))
        self.server_task = asyncio.create_task(self.server.serve(sockets=[sock]))
        async with asyncio.timeout(15):
            while not self.server.started:
                await asyncio.sleep(0.02)
        self.ui = await self.browser.new_page()
        await self.ui.route("https://**/*", lambda route: route.abort())
        self.ui.on("pageerror", lambda error: self.errors.append(str(error)))
        await self.ui.goto(self.base)
        await self.ui.locator('[data-tab="tab-reviews"]').click()
        for job_id, status in ((700001, "Applied"), (700002, "Rejected")):
            await self.ui.locator(f'[data-tab="tab-{status.lower()}"]').click()
            control = self.ui.locator(f'#{status.lower()}-container select[data-job-url$="{job_id}/"]')
            await control.wait_for()
            self.assertEqual(await control.input_value(), status)
            self.assertTrue(self.db.is_job_blocked(f"https://www.linkedin.com/jobs/view/{job_id}/"))
        self.assertEqual({row["job_url"]: row["status_changed_at"] for row in self.db.get_saved_jobs()}, before)
        self.assertEqual(self.errors, [])

    async def test_manual_terminal_status_closes_active_approval_without_submitting(self):
        from src.review import review_coordinator
        job = JobRecord(job_url="https://www.linkedin.com/jobs/view/700003/", title="Analyst", status="READY_FOR_REVIEW")
        self.db.save_job_record(job)
        review_id = self.db.queue_application_review("Analyst", "Example", job.job_url)
        waiter = review_coordinator.create_waiter(review_id)
        try:
            response = await self.ui.request.post(self.base + "/api/jobs/status", data={"job_url": job.job_url, "status": "Rejected", "rejection_reason": "position_closed"})
            self.assertEqual(response.status, 200)
            self.assertEqual(await waiter, "rejected")
            response = await self.ui.request.post(self.base + f"/api/applications/reviews/{review_id}/decision", data={"decision": "approve"})
            self.assertEqual(response.status, 409)
            await self.ui.reload()
            await self.ui.locator('[data-tab="tab-rejected"]').click()
            await self.ui.locator('#rejected-container select').wait_for()
            self.assertEqual(await self.ui.locator('#rejected-container select').input_value(), "Rejected")
            self.assertEqual(await self.ui.locator('#review-container select').count(), 0)
            self.assertEqual(await self.ui.get_by_role("button", name="Approve & Submit", exact=True).count(), 0)
            result = await LinkedInScraper(self.site)._await_user_approval(
                {"job_url": job.job_url}, 70, {}, AsyncMock()
            )
            self.assertEqual(result["decision"], "rejected")
        finally:
            review_coordinator.finish(review_id)
        self.assertEqual(self.db.get_applied_jobs(), [])
        self.assertEqual(self.errors, [])

    async def test_mode_guards_and_existing_autopilot_dispatch(self):
        for state in ("search_running", "autopilot_running"):
            with patch.object(main_module, state, True):
                response = await self.ui.request.post(self.base + "/api/search/start")
                self.assertEqual(response.status, 409)
        with patch.object(main_module, "search_running", True):
            response = await self.ui.request.post(self.base + "/api/autopilot/start")
            self.assertEqual(response.status, 409)
        with patch.object(main_module, "browser_paused", True), patch.object(main_module.browser_manager, "page", self.site):
            response = await self.ui.request.post(self.base + "/api/search/start")
            self.assertEqual(response.status, 409)
        with patch.object(config_manager, "profile", {}):
            response = await self.ui.request.post(self.base + "/api/search/start")
            self.assertEqual(response.status, 422)
        with patch.object(LinkedInScraper, "run_autopilot", AsyncMock(return_value={})) as autopilot:
            async with self.ui.expect_response("**/api/autopilot/start") as response:
                await self.ui.locator("#btn-start").click()
            self.assertEqual((await response.value).status, 200)
            await main_module.autopilot_task
            autopilot.assert_awaited_once()
        self.assertEqual(self.errors, [])

    async def test_source_filters_and_skipped_pending_move(self):
        for index, (status, source) in enumerate((
            ("SKIPPED", "autopilot"), ("SAVED_FOR_REVIEW", "search_save"),
            ("FAILED", "search_save"), ("APPLIED", "autopilot"),
            ("REJECTED", "search_save"),
        )):
            self.db.save_job_record(JobRecord(
                job_url=f"https://www.linkedin.com/jobs/view/{880000 + index}/",
                title="Analyst", company="Example", status=status, source_mode=source))
        await self.ui.reload()
        await self.ui.locator('[data-tab="tab-search-issues"]').click()
        await self.ui.locator('#source-filters-skipped button', has_text="Autopilot (1)").click()
        await self.ui.locator('#search-issues-container select').select_option("Pending")
        await self.ui.wait_for_function("document.querySelector('#badge-search-issues').textContent === '0' && document.querySelector('#badge-reviews').textContent === '2'")
        await self.ui.locator('[data-tab="tab-reviews"]').click()
        await self.ui.locator('#source-filters-review button', has_text="Autopilot (1)").click()
        self.assertEqual(await self.ui.locator('#review-container select').count(), 1)
        self.assertEqual(await self.ui.locator('#review-container .source-badge').inner_text(), "AUTO")
        await self.ui.locator('#review-container select').select_option("Rejected")
        await self.save_rejection()
        await self.ui.wait_for_function("document.querySelector('#badge-rejected').textContent === '2'")
        self.assertEqual(await self.ui.locator('#review-container select').count(), 0)
        await self.ui.locator('[data-tab="tab-rejected"]').click()
        await self.ui.locator('#source-filters-rejected button', has_text="Autopilot (1)").click()
        self.assertEqual(await self.ui.locator('#rejected-container select').count(), 1)
        row = next(j for j in self.db.get_dashboard_jobs() if j['job_url'].endswith('880000/'))
        self.assertEqual((row['source_mode'], row['original_bucket']), ('autopilot', 'skipped'))
        for bucket in ('review', 'applied', 'rejected', 'skipped', 'failed'):
            self.assertEqual(await self.ui.locator(f'#source-filters-{bucket} button').count(), 3)
        self.assertEqual(self.errors, [])

    async def test_status_moves_without_refetch_and_legacy_filter_counts(self):
        for index, (status, source) in enumerate((
            ('POSSIBLE_MATCH', 'search_save'), ('SAVED_FOR_REVIEW', 'search_save'),
            ('APPLIED', 'autopilot'), ('FAILED', 'unknown'),
            ('SKIPPED', 'autopilot'),
        )):
            self.db.save_job_record(JobRecord(
                job_url=f'https://www.linkedin.com/jobs/view/{881000 + index}/',
                title='Analyst', company='Example', status=status,
                source_mode=source, original_bucket='possible_match'))
        await self.ui.reload()
        await self.ui.wait_for_function("document.querySelector('#badge-reviews').textContent === '2'")
        # A saved decision must render even if a follow-up read is unavailable.
        await self.ui.route('**/api/jobs/dashboard', lambda route: route.abort())
        await self.ui.locator('[data-tab="tab-reviews"]').click()
        for job_id, status in ((881000, 'Applied'), (881001, 'Rejected')):
            await self.ui.locator(f'#review-container select[data-job-url$="{job_id}/"]').select_option(status)
            if status == "Rejected":
                await self.save_rejection()
            await self.ui.wait_for_function(
                "([id, destination]) => !document.querySelector(`#review-container select[data-job-url$=\"${id}/\"]`) && !!document.querySelector(`#${destination}-container select[data-job-url$=\"${id}/\"]`)",
                arg=[job_id, status.lower()])
        self.assertEqual(await self.ui.locator('#review-container select').count(), 0)
        self.assertEqual(await self.ui.locator('#badge-applied').inner_text(), '2')
        self.assertEqual(await self.ui.locator('#badge-rejected').inner_text(), '1')
        await self.ui.unroute('**/api/jobs/dashboard')
        await self.ui.reload()
        await self.ui.wait_for_function("document.querySelector('#badge-applied').textContent === '2'")
        self.assertEqual(await self.ui.locator('#review-container select').count(), 0)
        self.assertEqual(await self.ui.locator('#applied-container select').count(), 2)
        self.assertEqual(await self.ui.locator('#rejected-container select').count(), 1)
        views = [('reviews', 'review', 'review-container'), ('applied', 'applied', 'applied-container'),
                 ('rejected', 'rejected', 'rejected-container'), ('search-issues', 'skipped', 'search-issues-container'),
                 ('flagged', 'failed', 'flagged-container')]
        for tab, bucket, container in views:
            await self.ui.locator(f'[data-tab="tab-{tab}"]').click()
            controls = self.ui.locator(f'#source-filters-{bucket} button')
            self.assertEqual(await controls.count(), 3)
            for index, badge in ((0, None), (1, 'AUTO'), (2, 'SEARCH')):
                button = controls.nth(index)
                count = int((await button.inner_text()).rsplit('(', 1)[1].rstrip(')'))
                await button.click()
                self.assertEqual(await self.ui.locator(f'#{container} select').count(), count)
                if badge:
                    self.assertTrue(all(value == badge for value in await self.ui.locator(f'#{container} .source-badge').all_inner_texts()))
            await controls.nth(0).click()
        self.assertEqual(await self.ui.locator('#flagged-container .source-badge').inner_text(), 'LEGACY')
        self.assertEqual(self.errors, [])
