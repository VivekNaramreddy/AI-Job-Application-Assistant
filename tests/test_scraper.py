import asyncio
import unittest
from unittest.mock import patch

from src.application import ApplicationState
from src.models import JobRecord
from src.scraper import EXPERIENCE_FILTERS, LinkedInScraper


class LinkedInScraperRulesTests(unittest.TestCase):
    def test_normalises_linkedin_job_urls(self):
        url = "https://www.linkedin.com/jobs/view/content-manager-1234567890/?trackingId=x"
        self.assertEqual(
            LinkedInScraper._normalise_linkedin_job_url(url),
            "https://www.linkedin.com/jobs/view/1234567890/",
        )

    def test_normalises_relative_linkedin_job_url(self):
        self.assertEqual(
            LinkedInScraper._normalise_linkedin_job_url(
                "/jobs/view/content-manager-1234567890/?trackingId=x"
            ),
            "https://www.linkedin.com/jobs/view/1234567890/",
        )

    def test_normalises_employment_types(self):
        self.assertEqual(
            LinkedInScraper._normalise_employment_types(
                ["full time", "Contract", "not-a-real-type"]
            ),
            ["Full-time", "Contract"],
        )

    def test_accepts_target_role_with_extra_title_words(self):
        result = LinkedInScraper.evaluate_job_match(
            title="Senior Digital Marketing Specialist",
            company="Example Inc",
            description="SEO and analytics",
            roles=["Digital Marketing"],
            prefs={"require_target_title_match": True, "minimum_match_score": 60},
        )
        self.assertTrue(result["matches"])
        self.assertGreaterEqual(result["score"], 60)

    def test_rejects_excluded_keyword(self):
        result = LinkedInScraper.evaluate_job_match(
            title="Content Marketing Manager",
            company="Example Inc",
            description="This role is commission only.",
            roles=["Content Marketing"],
            prefs={
                "excluded_keywords": ["commission only"],
                "require_target_title_match": True,
                "minimum_match_score": 60,
            },
        )
        self.assertFalse(result["matches"])
        self.assertIn("excluded keyword", result["reasons"][0])

    def test_rejects_missing_required_keyword(self):
        result = LinkedInScraper.evaluate_job_match(
            title="Content Marketing Manager",
            company="Example Inc",
            description="Editorial planning and writing",
            roles=["Content Marketing"],
            prefs={
                "required_keywords": ["Google Analytics"],
                "require_target_title_match": True,
                "minimum_match_score": 60,
            },
        )
        self.assertFalse(result["matches"])
        self.assertTrue(any("missing required" in reason for reason in result["reasons"]))

    def test_detects_closed_job_copy(self):
        self.assertEqual(
            LinkedInScraper._closed_reason("This job is no longer accepting applications"),
            "no longer accepting applications",
        )

    def test_linkedin_experience_levels_have_filter_codes(self):
        self.assertEqual(EXPERIENCE_FILTERS["entry level"], "2")
        self.assertEqual(EXPERIENCE_FILTERS["executive"], "6")

    def test_ready_for_review_state_is_persisted_on_job_record(self):
        job = JobRecord(
            job_url="https://www.linkedin.com/jobs/view/1234567890/",
            title="Business Analyst",
            company="ABC Corporation",
        )
        with patch("src.scraper.db_manager.save_job_record") as save:
            asyncio.run(
                LinkedInScraper._record_application_state(
                    job,
                    ApplicationState.READY_FOR_REVIEW,
                    {
                        "required_fields_complete": True,
                        "resume_uploaded": True,
                        "resume_used": "candidate.pdf",
                        "final_submit_detected": True,
                        "step_count": 3,
                    },
                    88,
                )
            )
        self.assertEqual(job.status, "READY_FOR_REVIEW")
        self.assertEqual(job.resume_used, "candidate.pdf")
        self.assertEqual(job.match_score, 88)
        self.assertIn("Final submit detected: Yes", job.notes)
        save.assert_called_once_with(job)


if __name__ == "__main__":
    unittest.main()
