"""Offline regression of the plain Apply -> LinkedIn contact dialog flow."""

import copy
import unittest
from unittest.mock import AsyncMock, patch

from playwright.async_api import async_playwright

from src.application import ApplicationState
from src.config import config_manager
from src.external_navigation import ExternalApplicationLauncher
from src.form_filler import FormFiller
from src.job_extractor import LinkedInJobExtractor
from src.models import JobRecord
from src.runtime import runtime_status
from src.scraper import LinkedInScraper


URL = "https://www.linkedin.com/jobs/view/1234567890/"
HTML = """
<h1>Search Marketing Analyst <span class="visually-hidden">with verification</span></h1>
<div class="job-details-jobs-unified-top-card__company-name">Example Company</div>
<button class="jobs-apply-button" onclick="setTimeout(() => document.querySelector('#application').hidden=false, DELAY)">Apply</button>
<div role="dialog" hidden><h2>Unrelated hidden dialog</h2></div>
<section id="application" role="dialog" hidden>
  <h2>Apply to Example Company</h2>
  <form id="fields">
    <label>Email address<input type="email" required id="email"></label>
    <label>Phone country code<select required><option>United States (+1)</option></select></label>
    <label>Mobile phone number<input type="tel" required id="phone"></label>
    <button type="button" id="next">Next</button>
  </form>
</section>
<script>
window.submitClicks = 0;
document.querySelector('#next').onclick = () => {
  window.contact = {email: document.querySelector('#email').value, phone: document.querySelector('#phone').value};
  document.querySelector('#fields').innerHTML = NEXT_STEP;
};
</script>
"""


class LinkedInModalBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.playwright = await async_playwright().start()
        self.addAsyncCleanup(self.playwright.stop)
        self.browser = await self.playwright.chromium.launch(headless=True)
        self.addAsyncCleanup(self.browser.close)
        self.page = await self.browser.new_page()
        original = copy.deepcopy(config_manager.profile)
        self.addCleanup(setattr, config_manager, "profile", original)
        config_manager.profile = {"personal_info": {"email": "alex@example.test", "phone": "2125550100"}}
        runtime_status.reset()

    async def load(self, unknown=False, delay=100):
        import json
        next_step = ('<label>Professional license number<input required></label><button type="button">Next</button>'
                     if unknown else '<h3>Review your application</h3><button type="button" onclick="window.submitClicks++">Submit application</button>')
        html = HTML.replace('DELAY', str(delay)).replace('NEXT_STEP', json.dumps(next_step))
        await self.page.route("**/*", lambda route: route.fulfill(content_type="text/html", body=html))
        await self.page.goto(URL)

    async def test_plain_apply_contacts_next_ready_and_synchronized_states(self):
        await self.load(delay=1800)
        job = JobRecord(job_url=URL)
        await LinkedInJobExtractor(self.page).extract(job)
        self.assertEqual(job.title, "Search Marketing Analyst")
        job.mark_reviewed()
        self.assertEqual(job.status, "REVIEWED")
        session = await ExternalApplicationLauncher.open(self.page, self.page.locator('.jobs-apply-button'))
        self.assertTrue(session.linkedin_application)
        self.assertIs(session.application_page, self.page)
        self.assertEqual(len(self.page.context.pages), 1)
        self.assertEqual(self.page.url, URL)
        job.easy_apply, job.external_apply, job.ats_type = True, False, "LinkedIn Easy Apply"
        states = []

        async def state_callback(state, details):
            await LinkedInScraper._record_application_state(job, state, details, 90)
            states.append(job.status)
            snapshot = runtime_status.as_dict()
            self.assertEqual(snapshot['current_job'], job.title)
            self.assertEqual(snapshot['company'], job.company)

        with patch('src.scraper.db_manager.save_job_record'):
            await state_callback(ApplicationState.APPLICATION_STARTED, {})
            self.assertEqual(job.status, 'APPLICATION_STARTED')
            self.assertEqual(runtime_status.as_dict()['applications_started'], 1)
            result = await FormFiller(self.page).fill_easy_apply_form(
                job.title, job.company, URL, state_callback=state_callback)
        self.assertEqual(result['status'], 'ready_for_review')
        self.assertEqual(job.status, 'READY_FOR_REVIEW')
        self.assertIn('FILLING', states)
        self.assertNotIn('FAILED', states)
        self.assertNotIn('APPLIED', states)
        self.assertEqual(runtime_status.as_dict()['applications_started'], 1)
        self.assertEqual(await self.page.evaluate('window.submitClicks'), 0)
        self.assertEqual((await self.page.evaluate('window.contact'))['email'], 'alex@example.test')

    async def test_unknown_required_question_stops_without_guessing(self):
        await self.load(unknown=True)
        session = await ExternalApplicationLauncher.open(self.page, self.page.locator('.jobs-apply-button'))
        self.assertTrue(session.linkedin_application)
        states = []
        async def state_callback(state, details):
            states.append(state)
        result = await FormFiller(self.page).fill_easy_apply_form('Analyst', 'Example Company', URL, state_callback=state_callback)
        self.assertEqual(result['status'], 'flagged')
        self.assertEqual(states[-1], ApplicationState.NEEDS_USER_INPUT)
        self.assertEqual(await self.page.locator('#fields input').input_value(), '')
        self.assertEqual(await self.page.evaluate('window.submitClicks'), 0)

    async def test_title_badge_description_removed_but_legitimate_words_kept(self):
        for title in ('Verification Analyst', 'Verified Systems Engineer', 'Engineer with verification experience'):
            self.assertEqual(LinkedInJobExtractor.clean_title(title), title)
        await self.page.set_content('<h1>Verification Analyst <span role="img" aria-label="Verified badge">Verified company badge</span></h1>')
        job = await LinkedInJobExtractor(self.page).extract(JobRecord(job_url=URL))
        self.assertEqual(job.title, 'Verification Analyst')

    async def test_unrelated_dialog_is_not_an_application(self):
        from src.linkedin_modal import wait_for_linkedin_modal
        await self.page.set_content('<div role="dialog"><h2>Cookies</h2><button>Next</button></div>')
        self.assertIsNone(await wait_for_linkedin_modal(self.page, timeout=100))

    async def test_scraper_routes_plain_apply_to_linkedin_handler(self):
        await self.load()
        scraper = LinkedInScraper(self.page)
        job = JobRecord(job_url=URL, title='Stale title', company='Stale company')
        real_fill = scraper.form_filler.fill_easy_apply_form
        async def fill(*args, **kwargs):
            # Exercise the real analyzer, stopping at final review in this test.
            kwargs['approval_callback'] = None
            return await real_fill(*args, **kwargs)
        with patch.object(scraper.match_scorer, 'evaluate', new=AsyncMock(return_value={'matches': True, 'score': 90})), \
             patch.object(scraper.form_filler, 'fill_easy_apply_form', side_effect=fill), \
             patch.object(scraper, '_run_external_application', new=AsyncMock()) as external, \
             patch('src.scraper.db_manager.save_job_record'):
            result = await scraper._review_job(job, ['Analyst'], {}, AsyncMock(), None)
        external.assert_not_awaited()
        self.assertEqual(result['status'], 'ready_for_review')
        self.assertTrue(job.easy_apply)
        self.assertFalse(job.external_apply)
        self.assertEqual(job.status, 'READY_FOR_REVIEW')
        self.assertEqual(runtime_status.as_dict()['applications_started'], 1)
        self.assertEqual(runtime_status.as_dict()['current_job'], 'Search Marketing Analyst')
        self.assertEqual(runtime_status.as_dict()['company'], 'Example Company')

    async def test_applied_requires_submission_confirmation(self):
        for confirmed in (False, True):
            with self.subTest(confirmed=confirmed):
                await self.load()
                scraper = LinkedInScraper(self.page)
                job = JobRecord(job_url=URL)
                with patch.object(scraper.match_scorer, 'evaluate', new=AsyncMock(return_value={'matches': True, 'score': 90})), \
                     patch.object(scraper, '_await_user_approval', new=AsyncMock(return_value={'decision': 'approved'})), \
                     patch.object(scraper.form_filler, '_wait_for_submission_confirmation', new=AsyncMock(return_value=confirmed)), \
                     patch('src.scraper.db_manager.save_job_record'):
                    await scraper._review_job(job, ['Analyst'], {}, AsyncMock(), None)
                self.assertEqual(await self.page.evaluate('window.submitClicks'), 1)
                self.assertEqual(job.status, 'APPLIED' if confirmed else 'FAILED')
                self.assertEqual(bool(job.date_applied), confirmed)

    async def test_no_application_signal_fails(self):
        await self.page.set_content('<button>Apply</button><div role="dialog">Cookies</div>')
        with self.assertRaisesRegex(RuntimeError, 'did not open a LinkedIn application modal'):
            await ExternalApplicationLauncher.open(self.page, self.page.locator('button'))

    async def test_modal_container_and_progress_fallbacks(self):
        from src.linkedin_modal import wait_for_linkedin_modal
        for html in (
            '<div class="artdeco-modal"><h2>Apply to Example Company</h2><input><button>Next</button></div>',
            '<section aria-modal="true"><progress value="1" max="3"></progress><button>Review</button></section>',
        ):
            await self.page.set_content(html)
            self.assertIsNotNone(await wait_for_linkedin_modal(self.page, timeout=100))
