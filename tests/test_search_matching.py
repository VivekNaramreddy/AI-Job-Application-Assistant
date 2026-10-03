"""Search-only match regression tests, using a synthetic adjacent-role listing."""

import json
import unittest
from pathlib import Path

from src.llm import LLMResult
from src.scoring import JobMatchScorer


class SearchMatchingTests(unittest.IsolatedAsyncioTestCase):
    async def test_synthetic_adjacent_role_and_unchanged_autopilot(self):
        fixture = json.loads((Path(__file__).parent / "fixtures/synthetic_search_match.json").read_text(encoding="utf-8"))
        job, prefs = fixture["job"], fixture["preferences"]
        args = (job["job_title"], job["company"], job["description"], prefs["target_roles"],
                prefs, fixture["profile"], job)
        scorer = JobMatchScorer()
        previous = await scorer.evaluate(*args)
        self.assertEqual(previous["score"], 61)
        self.assertFalse(previous["matches"])
        self.assertIn("match score 40 is below minimum 60", previous["reasons"])
        result = await scorer.evaluate_search(*args)
        self.assertEqual(result["score"], 90)
        self.assertTrue(result["matches"])
        self.assertEqual(result["classification"], "Possible Match")
        self.assertEqual(result["reasons"], [])
        print(f"Synthetic search fixture: old displayed={previous['score']}, rejected={previous['reasons']}; new={result['score']}, {result['classification']}")

    async def test_final_threshold_and_reason_use_same_score(self):
        args = ("Data Analyst", "Example", "Analyze business requirements and provide commercial insights.",
                ["Business Analyst"])
        scorer = JobMatchScorer()
        prefs = {"require_target_title_match": True}
        profile = {"ai_settings": {"enabled": False}}
        first = await scorer.evaluate_search(*args, prefs, profile)
        score = first["score"]
        equal = await scorer.evaluate_search(*args, {**prefs, "minimum_match_score": score}, profile)
        above = await scorer.evaluate_search(*args, {**prefs, "minimum_match_score": score + 1}, profile)
        self.assertTrue(equal["matches"])
        self.assertFalse(above["matches"])
        self.assertEqual(above["reasons"], [f"match score {score} is below minimum {score + 1}"])

    async def test_strong_description_can_qualify_without_any_title_overlap(self):
        result = await JobMatchScorer().evaluate_search(
            "Insights Specialist", "Example", "Analyze commercial requirements and research business strategy.",
            ["Business Analyst"], {"require_target_title_match": True, "minimum_match_score": 60},
            {"ai_settings": {"enabled": False}},
        )
        self.assertTrue(result["matches"])
        self.assertEqual(result["classification"], "Possible Match")

    async def test_title_alone_cannot_outweigh_unrelated_description(self):
        scorer = JobMatchScorer()
        for title in ("Business Analyst", "Registered Nurse"):
            result = await scorer.evaluate_search(
                title, "Hospital", "Provide bedside care, administer medication and monitor patient vital signs.",
                ["Business Analyst"], {"minimum_match_score": 60}, {"ai_settings": {"enabled": False}},
            )
            self.assertFalse(result["matches"])
            self.assertLess(result["score"], 60)
            self.assertNotIn("title does not match a target role", result["reasons"])

    async def test_skills_tools_experience_and_education_affect_final_score(self):
        args = ("Insights Specialist", "Example",
                "Analyze commercial requirements with Python, SQL, Excel and Tableau. Requires 4 years of experience and a bachelor's degree.",
                ["Business Analyst"], {"minimum_match_score": 60})
        good = {"candidate_profile": {"skills": ["Python", "SQL", "Excel", "Tableau"],
                "years_of_experience": "5", "education": [{"degree": "Master's Degree"}]}, "ai_settings": {"enabled": False}}
        weak = {"candidate_profile": {"skills": ["Nursing"], "years_of_experience": "0",
                "education": [{"degree": "High school"}]}, "ai_settings": {"enabled": False}}
        scorer = JobMatchScorer()
        good_result = await scorer.evaluate_search(*args, good)
        weak_result = await scorer.evaluate_search(*args, weak)
        self.assertTrue(good_result["matches"])
        self.assertFalse(weak_result["matches"])
        self.assertGreater(good_result["score"], weak_result["score"])

    async def test_explicit_exclusions_still_apply(self):
        result = await JobMatchScorer().evaluate_search(
            "Business Analyst", "Blocked", "Analyze commercial requirements and business strategy.",
            ["Business Analyst"], {"excluded_companies": ["Blocked"], "minimum_match_score": 60},
            {"ai_settings": {"enabled": False}},
        )
        self.assertFalse(result["matches"])
        self.assertEqual(result["reasons"], ["excluded company: Blocked"])

    async def test_ai_score_cannot_create_a_second_routing_score(self):
        class Provider:
            available = True
            provider_name = "fixture"
            async def generate_json(self, *args):
                return LLMResult(ok=True, data={"match_score": 1, "confidence": 1,
                    "matched_requirements": [], "missing_requirements": [], "important_notes": []})
        result = await JobMatchScorer(llm_provider=Provider()).evaluate_search(
            "Insights Specialist", "Example", "Analyze business requirements and commercial strategy.",
            ["Business Analyst"], {"minimum_match_score": 60}, {},
        )
        self.assertTrue(result["matches"])
        self.assertGreaterEqual(result["score"], 60)
        self.assertEqual(result["reasons"], [])
