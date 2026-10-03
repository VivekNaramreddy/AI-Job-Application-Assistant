import json
import unittest

from playwright.async_api import async_playwright

from src.job_extractor import LinkedInJobExtractor, JobExtractionError
from src.models import JobRecord


class LinkedInJobExtractorBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def test_external_url_is_read_without_clicking(self):
        page = await self.browser.new_page()
        for link, expected in (
            ('https://ats.example.test/apply/1', 'https://ats.example.test/apply/1'),
            ('https://jobs.lever.co/example/123', 'https://jobs.lever.co/example/123'),
            ('https://www.linkedin.com/redir/redirect?url=https%3A%2F%2Fats.example.test%2Fapply%2F2', 'https://ats.example.test/apply/2'),
            ('javascript:alert(1)', ''),
        ):
            await page.set_content(f'<main><a class="jobs-apply-button" href="{link}" onclick="window.clicked=true">Apply</a></main>')
            job = JobRecord(job_url="https://www.linkedin.com/jobs/view/123456/", source_mode="search_save")
            await LinkedInJobExtractor(page).extract(job)
            self.assertEqual(job.application_method, "external")
            self.assertEqual(job.application_url, expected)
            self.assertEqual(job.ats_type, 'Lever' if 'jobs.lever.co' in expected else '')
            self.assertIsNone(await page.evaluate('window.clicked'))

    async def test_search_uses_structured_data_without_dom_description(self):
        page = await self.browser.new_page()
        description = "Analyze business requirements and customer data, develop clear reports and collaborate with the engineering team to improve services."
        posting = {"@type": "JobPosting", "title": "Analyst", "description": description,
                   "hiringOrganization": {"name": "Example"},
                   "jobLocation": {"address": {"addressLocality": "New York", "addressRegion": "NY"}}}
        await page.set_content(f'<script type="application/ld+json">{json.dumps(posting)}</script>')
        record = JobRecord(job_url="https://www.linkedin.com/jobs/view/123456/")
        await LinkedInJobExtractor(page).extract_for_search(record, timeout=2)
        self.assertEqual((record.title, record.company, record.location), ("Analyst", "Example", "New York, NY"))
        self.assertEqual(record.description, description)

    async def test_search_waits_for_modern_live_dom_and_reads_full_collapsed_description(self):
        page = await self.browser.new_page()
        description = ("Analyze customer data and develop detailed recommendations for the business. "
                       "Work with marketing and engineering teams to investigate trends and present reports.")
        await page.set_content('<main><h2>Loading</h2></main>')
        # Reduced structure observed on live LinkedIn: no h1 or stable CSS classes.
        await page.evaluate("""description => setTimeout(() => {
            document.querySelector('main').innerHTML = `<section><div>
              <div aria-label="Company, Example Inc."><p><a href="/company/example">Example Inc.</a></p></div>
              <div><p>Data Analyst</p></div><p>New York, NY · 2 days ago · 30 applicants</p>
              <button onclick="window.applicationClicked = true">Easy Apply</button></div>
              <div><div><h2>About the job</h2></div><div>
                <span data-testid="expandable-text-box" style="display:block;max-height:20px;overflow:hidden">
                  <p>${description}</p><p>Final requirement: strong communication skills.</p>
                  <button data-testid="expandable-text-button">… more</button>
                </span></div></div>
              <div><h2>About the company</h2><span data-testid="expandable-text-box">Unrelated company content.</span></div>
            </section>`;
        }, 650)""", description)
        record = JobRecord(job_url="https://www.linkedin.com/jobs/view/123456/")
        await LinkedInJobExtractor(page).extract_for_search(record, timeout=4)
        self.assertEqual(record.title, "Data Analyst")
        self.assertEqual(record.company, "Example Inc.")
        self.assertEqual(record.location, "New York, NY")
        self.assertIn(description, record.description)
        self.assertTrue(record.description.endswith("Final requirement: strong communication skills."))
        self.assertNotIn("Unrelated", record.description)
        self.assertNotIn("more", record.description)
        self.assertIsNone(await page.evaluate("window.applicationClicked"))

    async def test_search_rejects_placeholder_description_and_keeps_partial_details(self):
        page = await self.browser.new_page()
        await page.set_content('<main><h1>Analyst</h1><div id="job-details">Loading...</div></main>')
        record = JobRecord(job_url="https://www.linkedin.com/jobs/view/123456/")
        with self.assertRaises(JobExtractionError):
            await LinkedInJobExtractor(page).extract_for_search(record, timeout=0.5)
        self.assertEqual(record.title, "Analyst")
        self.assertIsNone(record.match_score)

    async def test_external_apply_button_is_not_easy_apply(self):
        page = await self.browser.new_page()
        await page.set_content('<main><h1>Analyst</h1><button class="jobs-apply-button">Apply</button></main>')
        job = await LinkedInJobExtractor(page).extract(JobRecord(job_url="https://www.linkedin.com/jobs/view/1/"))
        self.assertFalse(job.easy_apply)
        self.assertTrue(job.external_apply)

    async def test_json_ld_description_survives_missing_dom_description(self):
        page = await self.browser.new_page()
        await page.set_content('<script type="application/ld+json">{"@type":"JobPosting","title":"Analyst","description":"<p>Analyze &amp; report</p>"}</script>')
        job = await LinkedInJobExtractor(page).extract(JobRecord(job_url="https://www.linkedin.com/jobs/view/2/"))
        self.assertEqual(job.title, "Analyst")
        self.assertEqual(job.description, "Analyze & report")

    async def asyncSetUp(self):
        try:
            self.playwright = await async_playwright().start()
            self.browser = await self.playwright.chromium.launch(headless=True)
        except Exception as exc:
            self.skipTest(f"Local Chromium is unavailable: {exc}")

    async def asyncTearDown(self):
        if hasattr(self, "browser"):
            await self.browser.close()
        if hasattr(self, "playwright"):
            await self.playwright.stop()

    async def test_extracts_metadata_from_linkedin_shaped_live_dom(self):
        page = await self.browser.new_page()
        await page.set_content(
            """
            <main>
              <h1 class="job-details-jobs-unified-top-card__job-title">Business Analyst</h1>
              <div class="job-details-jobs-unified-top-card__company-name">
                <a href="/company/abc">ABC Corporation</a>
              </div>
              <div class="job-details-jobs-unified-top-card__primary-description-container">
                <span class="tvm__text--low-emphasis">New York, NY</span>
                <span>2 days ago</span>
              </div>
              <div class="job-details-jobs-unified-top-card__job-insight">
                Full-time · Hybrid · $85,000 - $110,000 a year
              </div>
              <article class="jobs-description">
                <div class="jobs-description__content">Analyze business requirements.</div>
              </article>
              <button class="jobs-apply-button" aria-label="Easy Apply">Easy Apply</button>
            </main>
            """
        )
        record = JobRecord(
            job_url="https://www.linkedin.com/jobs/view/business-analyst-1234567890/"
        )

        result = await LinkedInJobExtractor(page).extract(record)

        self.assertIs(result, record)
        self.assertEqual(record.title, "Business Analyst")
        self.assertEqual(record.company, "ABC Corporation")
        self.assertEqual(record.location, "New York, NY")
        self.assertEqual(record.external_job_id, "1234567890")
        self.assertEqual(record.source, "LinkedIn")
        self.assertEqual(record.workplace_type, "Hybrid")
        self.assertEqual(record.employment_type, "Full-time")
        self.assertEqual(record.salary, "$85,000 - $110,000 a year")
        self.assertEqual(record.posted_date, "2 days ago")
        self.assertTrue(record.easy_apply)


if __name__ == "__main__":
    unittest.main()
