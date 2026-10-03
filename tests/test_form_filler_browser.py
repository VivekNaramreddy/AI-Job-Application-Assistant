import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from playwright.async_api import async_playwright

from src.application import ApplicationState
from src.config import config_manager
from src.form_filler import FormFiller


class FormFillerBrowserTests(unittest.IsolatedAsyncioTestCase):
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

    async def test_multistep_form_reaches_ready_without_clicking_submit(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            resume_path = Path(temp_dir) / "candidate_resume.pdf"
            resume_path.write_bytes(b"%PDF-1.7\ntest resume")
            config_manager.profile = {
                "personal_info": {
                    "full_name": "Alex Candidate",
                    "email": "alex@example.com",
                    "phone": "+1 212 555 0100",
                    "country": "United States",
                    "resume_path": str(resume_path.resolve()),
                },
                "default_answers": {
                    "years_of_experience": "5",
                    "work_authorization": "Yes",
                },
            }
            page = await self.browser.new_page()
            await page.set_content(
                """
                <div class="jobs-easy-apply-modal" role="dialog" aria-label="Easy Apply">
                  <form id="application-form">
                    <div><label for="email">Email address *</label><input id="email" type="email" required></div>
                    <div><label for="phone">Mobile phone *</label><input id="phone" type="tel" required></div>
                    <div><label for="years">Years of experience *</label><input id="years" type="number" required></div>
                    <div><label for="country">Country *</label><select id="country" required>
                      <option value="">Select an option</option><option>United States</option>
                    </select></div>
                    <fieldset><legend>Are you legally authorized to work? *</legend>
                      <label><input type="radio" name="authorization" value="Yes" required>Yes</label>
                      <label><input type="radio" name="authorization" value="No" required>No</label>
                    </fieldset>
                    <label><input id="privacy" type="checkbox" required>I agree to the privacy terms *</label>
                    <div><label for="resume">Resume *</label><input id="resume" type="file" required></div>
                    <button type="button" id="next" aria-label="Continue to next step">Next</button>
                  </form>
                </div>
                <script>
                  window.submitClicks = 0;
                  document.querySelector('#next').addEventListener('click', () => {
                    window.captured = {
                      email: document.querySelector('#email').value,
                      phone: document.querySelector('#phone').value,
                      years: document.querySelector('#years').value,
                      country: document.querySelector('#country').value,
                      authorization: document.querySelector('input[name=authorization]:checked')?.value,
                      privacy: document.querySelector('#privacy').checked,
                      resume: document.querySelector('#resume').value
                    };
                    document.querySelector('#application-form').innerHTML = `
                      <h3>Review your application</h3>
                      <button type="button" id="submit" aria-label="Submit application">Submit application</button>`;
                    document.querySelector('#submit').addEventListener('click', () => window.submitClicks++);
                  });
                </script>
                """
            )
            states = []

            async def state_callback(state, details):
                states.append((state, details))

            result = await FormFiller(page).fill_easy_apply_form(
                "Business Analyst",
                "ABC Corporation",
                "https://www.linkedin.com/jobs/view/123/",
                state_callback=state_callback,
            )

            self.assertEqual(result["status"], "ready_for_review")
            self.assertTrue(result["preserve_modal"])
            self.assertEqual(await page.evaluate("window.submitClicks"), 0)
            captured = await page.evaluate("window.captured")
            self.assertEqual(captured["email"], "alex@example.com")
            self.assertEqual(captured["years"], "5")
            self.assertEqual(captured["country"], "United States")
            self.assertEqual(captured["authorization"], "Yes")
            self.assertTrue(captured["privacy"])
            self.assertIn("candidate_resume.pdf", captured["resume"])
            self.assertIn(
                ApplicationState.READY_FOR_REVIEW,
                [state for state, _ in states],
            )
            ready = next(
                details
                for state, details in states
                if state == ApplicationState.READY_FOR_REVIEW
            )
            self.assertTrue(ready["required_fields_complete"])
            self.assertTrue(ready["resume_uploaded"])
            self.assertTrue(ready["final_submit_detected"])

    async def test_unknown_required_question_pauses_and_preserves_modal(self):
        config_manager.profile = {"personal_info": {}, "default_answers": {}}
        page = await self.browser.new_page()
        await page.set_content(
            """
            <div class="jobs-easy-apply-modal" role="dialog" aria-label="Easy Apply">
              <div><label for="license">Professional license number *</label>
                <input id="license" type="text" required></div>
              <button type="button" id="next" aria-label="Continue to next step">Next</button>
            </div>
            <script>
              window.nextClicks = 0;
              document.querySelector('#next').addEventListener('click', () => window.nextClicks++);
            </script>
            """
        )
        states = []

        async def state_callback(state, details):
            states.append(state)

        with patch("src.form_filler.db_manager.log_flagged_job", return_value=True):
            result = await FormFiller(page).fill_easy_apply_form(
                "Accountant",
                "Example Corp",
                "https://www.linkedin.com/jobs/view/456/",
                state_callback=state_callback,
            )

        self.assertEqual(result["status"], "flagged")
        self.assertTrue(result["preserve_modal"])
        self.assertEqual(await page.evaluate("window.nextClicks"), 0)
        self.assertTrue(
            await page.locator(".jobs-easy-apply-modal").is_visible()
        )
        self.assertIn(ApplicationState.NEEDS_USER_INPUT, states)


if __name__ == "__main__":
    unittest.main()
