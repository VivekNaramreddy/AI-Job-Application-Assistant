import unittest

from src.suggestions import find_suggestions


class SuggestionTests(unittest.TestCase):
    def test_marketing_query_returns_ranked_job_titles(self):
        suggestions = find_suggestions("target_roles", "marketing", limit=10)

        self.assertIn("Marketing Analyst", suggestions)
        self.assertIn("Marketing Executive", suggestions)
        self.assertTrue(suggestions[0].lower().startswith("marketing"))

    def test_keyword_query_is_case_insensitive(self):
        self.assertIn("Google Analytics", find_suggestions("required_keywords", "ANALYTICS"))

    def test_unknown_field_is_rejected(self):
        with self.assertRaises(KeyError):
            find_suggestions("unknown", "test")


if __name__ == "__main__":
    unittest.main()
