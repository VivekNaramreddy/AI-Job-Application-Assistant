import copy
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
from pathlib import Path

from playwright.async_api import async_playwright

from src.application import ApplicationState
from src.config import config_manager
from src.external_application import ExternalApplicationRunner
from src.models import JobRecord


class ExternalApplicationRunnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_external_blockers_never_fill_or_submit(self):
        cases = (("<main><input type='password'></main>", "login_required"),
                 ("<main><div id='captcha'>Verify</div></main>", "captcha"),
                 ("<main>This job is no longer accepting applications</main>", "application_closed"))
        for markup, expected in cases:
            page = await self.browser.new_page()
            await page.set_content(markup)
            runner = ExternalApplicationRunner(page)
            with patch.object(runner, '_wait_for_manual_completion', AsyncMock(return_value=False)), \
                    patch('src.ats.base.ATSAdapter.fill_fields', AsyncMock()) as fill:
                result = await runner.run(JobRecord(job_url='https://www.linkedin.com/jobs/view/9/'), AsyncMock())
                self.assertEqual(result['reason'], expected)
                fill.assert_not_called()
            await page.close()

    async def test_search_save_cannot_enter_external_runner(self):
        page = await self.browser.new_page()
        job = JobRecord(job_url='https://www.linkedin.com/jobs/view/9/', source_mode='search_save')
        with self.assertRaisesRegex(ValueError, 'Search & Save'):
            await ExternalApplicationRunner(page).run(job, AsyncMock())

    async def asyncSetUp(self):
        try:
            self.playwright = await async_playwright().start()
            self.browser = await self.playwright.chromium.launch(headless=True)
        except Exception as exc:
            self.skipTest(f"Local Chromium is unavailable: {exc}")
        self.original_profile = copy.deepcopy(config_manager.profile)

    async def asyncTearDown(self):
        config_manager.profile = self.original_profile
        if hasattr(self, "browser"):
            await self.browser.close()
        if hasattr(self, "playwright"):
            await self.playwright.stop()

    async def test_generic_form_fills_and_stops_at_ready_for_review(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            resume = Path(temp_dir) / "candidate.pdf"
            resume.write_bytes(b"%PDF-1.7\nresume")
            config_manager.profile = {
                "personal_info": {
                    "full_name": "Alex Candidate",
                    "email": "alex@example.com",
                    "phone": "+1 212 555 0100",
                    "resume_path": str(resume.resolve()),
                },
                "default_answers": {"years_of_experience": "5"},
                "work_preferences": {"approval_timeout_minutes": 1},
            }
            page = await self.browser.new_page()
            await page.set_content(
                """
                <form id="application-form">
                  <label for="email">Email *</label><input id="email" type="email" required>
                  <label for="phone">Phone *</label><input id="phone" type="tel" required>
                  <label for="years">Years of experience *</label><input id="years" type="number" required>
                  <label for="resume">Resume *</label><input id="resume" type="file" required>
                  <button id="submit" type="button">Submit application</button>
                </form>
                <script>
                  window.submitClicks = 0;
                  document.querySelector('#submit').addEventListener('click', () => window.submitClicks++);
                </script>
                """
            )
            job = JobRecord(
                job_url="https://www.linkedin.com/jobs/view/1234567890/",
                title="Business Analyst",
                company="ABC Corporation",
                location="New York, NY",
                application_url=page.url,
            )
            states = []
            logs = []

            async def log(message):
                logs.append(message)

            async def state_callback(state, details):
                states.append((state, details))

            result = await ExternalApplicationRunner(page).run(
                job, log, state_callback=state_callback
            )

            self.assertEqual(result["status"], "ready_for_review")
            self.assertEqual(result["ats"], "Generic HTML")
            self.assertTrue(result["preserve_page"])
            self.assertEqual(await page.evaluate("window.submitClicks"), 0)
            self.assertEqual(await page.locator("#email").input_value(), "alex@example.com")
            self.assertIn("candidate.pdf", await page.locator("#resume").input_value())
            self.assertIn(
                ApplicationState.READY_FOR_REVIEW,
                [state for state, _ in states],
            )
            self.assertTrue(any(message.startswith("ATS_EVENT") for message in logs))

    async def test_missing_form_returns_isolated_failure(self):
        config_manager.profile = {
            "personal_info": {}, "default_answers": {}, "work_preferences": {}
        }
        page = await self.browser.new_page()
        await page.set_content("<main>No application form here</main>")
        job = JobRecord(
            job_url="https://www.linkedin.com/jobs/view/2222222222/",
            title="Analyst",
            company="Example",
        )

        async def log(message):
            return None

        result = await ExternalApplicationRunner(page).run(job, log)
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(result["reason"], "form_not_found")

    async def test_named_adapters_fill_standard_saved_forms(self):
        config_manager.profile = {
            "personal_info": {
                "email": "alex@example.com",
                "phone": "+1 212 555 0100",
            },
            "default_answers": {},
            "work_preferences": {"approval_timeout_minutes": 1},
        }
        cases = (
            (
                "https://boards.greenhouse.io/example/jobs/1",
                "Greenhouse",
                "<form id='application-form'>{fields}</form>",
            ),
            (
                "https://jobs.lever.co/example/1",
                "Lever",
                "<form class='application-form'>{fields}</form>",
            ),
            (
                "https://jobs.ashbyhq.com/example/1",
                "Ashby",
                "<form data-testid='application-form'>{fields}</form>",
            ),
            (
                "https://example.wd5.myworkdayjobs.com/jobs/1",
                "Workday",
                "<form data-automation-id='applicationForm'>{fields}</form>",
            ),
        )
        fields = (
            "<label>Email *<input name='email' type='email' required></label>"
            "<label>Phone *<input name='phone' type='tel' required></label>"
            "<button type='button'>Submit application</button>"
        )

        async def log(message):
            return None

        for index, (url, expected_ats, template) in enumerate(cases):
            with self.subTest(ats=expected_ats):
                page = await self.browser.new_page()
                body = template.format(fields=fields)

                async def saved_form(route):
                    await route.fulfill(content_type="text/html", body=body)

                await page.route(url, saved_form)
                await page.goto(url)
                job = JobRecord(
                    job_url=f"https://www.linkedin.com/jobs/view/{3000000000 + index}/",
                    title="Analyst",
                    company="Example",
                )
                result = await ExternalApplicationRunner(page).run(job, log)
                self.assertEqual(result["status"], "ready_for_review")
                self.assertEqual(result["ats"], expected_ats)
                self.assertEqual(
                    await page.locator("input[name=email]").input_value(),
                    "alex@example.com",
                )
                await page.close()


if __name__ == "__main__":
    unittest.main()
