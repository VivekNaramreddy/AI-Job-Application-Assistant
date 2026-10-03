import unittest

from src.job_extractor import LinkedInJobExtractor


class LinkedInJobExtractorTests(unittest.TestCase):
    def test_extracts_stable_job_id_from_canonical_url(self):
        self.assertEqual(
            LinkedInJobExtractor.job_id_from_url(
                "https://www.linkedin.com/jobs/view/business-analyst-1234567890/"
            ),
            "1234567890",
        )

    def test_location_fallback_ignores_posting_age_and_applicant_count(self):
        self.assertEqual(
            LinkedInJobExtractor._choose_location(
                ["3 days ago", "Over 100 applicants", "New York, NY"]
            ),
            "New York, NY",
        )

    def test_location_fallback_does_not_repeat_company(self):
        self.assertEqual(
            LinkedInJobExtractor._choose_location(
                ["ABC Corporation", "Boston, MA"], excluded=["ABC Corporation"]
            ),
            "Boston, MA",
        )

    def test_extracts_salary_when_present(self):
        self.assertEqual(
            LinkedInJobExtractor._extract_salary("Compensation is $85,000 - $110,000 a year"),
            "$85,000 - $110,000 a year",
        )

    def test_missing_salary_is_empty(self):
        self.assertEqual(LinkedInJobExtractor._extract_salary("Salary not listed"), "")

    def test_finds_semantic_job_posting_fallback(self):
        posting = LinkedInJobExtractor._find_job_posting(
            {
                "@graph": [
                    {"@type": "Organization", "name": "LinkedIn"},
                    {
                        "@type": "JobPosting",
                        "title": "Business Analyst",
                        "employmentType": "FULL_TIME",
                    },
                ]
            }
        )
        self.assertEqual(posting["title"], "Business Analyst")
        self.assertEqual(
            LinkedInJobExtractor._structured_employment_type(
                posting["employmentType"]
            ),
            "Full-time",
        )

    def test_extracts_semantic_location_fallback(self):
        self.assertEqual(
            LinkedInJobExtractor._structured_location(
                {
                    "jobLocation": {
                        "address": {
                            "addressLocality": "New York",
                            "addressRegion": "NY",
                            "addressCountry": "US",
                        }
                    }
                }
            ),
            "New York, NY, US",
        )


if __name__ == "__main__":
    unittest.main()
