import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from src.answer_memory import AnswerMemoryRepository
from src.answer_resolver import AnswerResolver, IntentClassifier
from src.candidate_profile import CandidateProfile, ensure_candidate_profile
from src.llm import (
    LLMResult,
    LLMSettings,
    LMStudioProvider,
    NullLLMProvider,
    OpenAIProvider,
    create_llm_provider,
)
from src.scoring import JobMatchScorer


class CandidateProfileTests(unittest.TestCase):
    def test_legacy_profile_migrates_without_inventing_missing_facts(self):
        legacy = {
            "personal_info": {"full_name": "Test Candidate", "email": "test@example.com"},
            "default_answers": {"work_authorization": "Yes"},
            "work_preferences": {"target_roles": ["Business Analyst"]},
        }
        document, changed = ensure_candidate_profile(legacy)
        candidate = CandidateProfile.model_validate(document)
        self.assertTrue(changed)
        self.assertEqual(candidate.value_for_intent("email"), "test@example.com")
        self.assertEqual(candidate.value_for_intent("work_authorization"), "Yes")
        self.assertEqual(candidate.value_for_intent("citizenship"), "")
        self.assertEqual(candidate.employment_history, [])

    def test_specific_skill_experience_is_not_total_experience(self):
        intent = IntentClassifier.classify(
            "How many years of experience with Python do you have?"
        )
        self.assertEqual(intent.name, "experience_skill:python")


class AnswerMemoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.repository = AnswerMemoryRepository(Path(self.temp.name) / "answers.db")

    def tearDown(self):
        self.temp.cleanup()

    def test_semantically_equivalent_authorization_question_reuses_approved_answer(self):
        self.repository.save(
            "Are you authorized to work in the United States?",
            "Yes",
            canonical_intent="work_authorization",
            user_approved=True,
        )
        record = self.repository.find_approved(
            "Do you have legal authorization to work in the US?",
            canonical_intent="work_authorization",
        )
        self.assertIsNotNone(record)
        self.assertEqual(record.answer, "Yes")
        self.assertGreaterEqual(record.confidence, 0.9)

    def test_unapproved_legacy_rule_is_not_reused(self):
        path = Path(self.temp.name) / "legacy.json"
        path.write_text(
            json.dumps({"question_rules": {"salary expectation": "100000"}}),
            encoding="utf-8",
        )
        self.repository.migrate_legacy_json(path)
        self.assertIsNone(
            self.repository.find_approved(
                "What is your salary expectation?", "desired_salary"
            )
        )

    def test_profile_fact_has_priority_over_answer_memory(self):
        self.repository.save(
            "Are you authorized to work?", "No", "work_authorization"
        )
        resolver = AnswerResolver(memory=self.repository)
        result = resolver.resolve_deterministic(
            "Are you authorized to work?",
            {
                "personal_info": {},
                "default_answers": {"work_authorization": "Yes"},
                "work_preferences": {},
            },
        )
        self.assertEqual(result.value, "Yes")
        self.assertEqual(result.source, "candidate profile")


class ProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_lm_studio_openai_compatible_response(self):
        async def transport(url, payload, headers, timeout):
            self.assertEqual(url, "http://localhost:1234/v1/chat/completions")
            return {
                "choices": [{"message": {"content": json.dumps({
                    "canonical_intent": "work_authorization",
                    "answer": "Yes",
                    "confidence": 0.96,
                    "needs_user_input": False,
                    "notes": "Verified profile fact",
                })}}]
            }

        provider = LMStudioProvider(
            LLMSettings(
                enabled=True,
                provider="lmstudio",
                base_url="http://localhost:1234/v1",
                model="configured-model",
                timeout_seconds=1,
            ),
            transport,
        )
        result = await provider.interpret_question(
            "Are you authorized to work?",
            {"work_authorization": {"authorized": "Yes"}},
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.answer, "Yes")

    async def test_invalid_json_degrades_to_error_result(self):
        async def transport(url, payload, headers, timeout):
            return {"choices": [{"message": {"content": "not-json"}}]}

        provider = LMStudioProvider(
            LLMSettings(True, "lmstudio", "http://localhost:1234/v1", "configured"),
            transport,
        )
        result = await provider.interpret_question("Question", {})
        self.assertFalse(result.ok)
        self.assertIn("ValueError", result.error)

    async def test_timeout_degrades_to_error_result(self):
        async def transport(url, payload, headers, timeout):
            raise asyncio.TimeoutError

        provider = LMStudioProvider(
            LLMSettings(True, "lmstudio", "http://localhost:1234/v1", "configured"),
            transport,
        )
        result = await provider.interpret_question("Question", {})
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "timeout")

    async def test_no_configuration_returns_null_provider(self):
        provider = create_llm_provider({"ai_settings": {"enabled": False}})
        self.assertIsInstance(provider, NullLLMProvider)
        self.assertFalse(provider.available)

    async def test_openai_provider_uses_responses_and_structured_schema(self):
        captured = {}

        async def transport(url, payload, headers, timeout):
            captured.update({"url": url, "payload": payload})
            return {"output": [{"content": [{"type": "output_text", "text": json.dumps({
                "canonical_intent": "", "answer": "", "confidence": 0,
                "needs_user_input": True, "notes": "Missing fact"
            })}]}]}

        provider = OpenAIProvider(
            LLMSettings(True, "openai", "https://api.openai.com/v1", "configured", "key"),
            transport,
        )
        await provider.interpret_question("Unknown fact?", {})
        self.assertEqual(captured["url"], "https://api.openai.com/v1/responses")
        self.assertEqual(captured["payload"]["text"]["format"]["type"], "json_schema")
        self.assertFalse(captured["payload"]["store"])

    async def test_low_confidence_ai_answer_is_not_used(self):
        class LowConfidenceProvider:
            available = True

            async def interpret_question(self, **kwargs):
                return LLMResult(
                    ok=True, answer="Maybe", confidence=0.4,
                    canonical_intent="", needs_user_input=False
                )

        with tempfile.TemporaryDirectory() as directory:
            resolver = AnswerResolver(
                memory=AnswerMemoryRepository(Path(directory) / "answers.db"),
                llm_provider=LowConfidenceProvider(),
                minimum_confidence=0.85,
            )
            result = await resolver.resolve(
                "Why are you interested in this position?",
                {"personal_info": {}, "default_answers": {}, "work_preferences": {}},
            )
            self.assertEqual(result.value, "")

    async def test_high_confidence_ai_cannot_invent_missing_factual_answer(self):
        class FabricatingProvider:
            available = True

            async def interpret_question(self, **kwargs):
                return LLMResult(
                    ok=True, answer="Yes", confidence=0.99,
                    canonical_intent="work_authorization", needs_user_input=False
                )

        with tempfile.TemporaryDirectory() as directory:
            resolver = AnswerResolver(
                memory=AnswerMemoryRepository(Path(directory) / "answers.db"),
                llm_provider=FabricatingProvider(),
            )
            result = await resolver.resolve(
                "Are you authorized to work?",
                {"personal_info": {}, "default_answers": {}, "work_preferences": {}},
            )
            self.assertEqual(result.value, "")


class MatchScoringTests(unittest.IsolatedAsyncioTestCase):
    async def test_deterministic_score_uses_profile_and_job_dimensions(self):
        scorer = JobMatchScorer()
        result = await scorer.evaluate(
            "Business Analyst", "Example",
            "Requires 3 years of experience, Python, and a bachelor's degree.",
            ["Business Analyst"],
            {
                "minimum_match_score": 60,
                "require_target_title_match": True,
                "target_locations": ["New York, NY"],
                "workplace_types": ["Hybrid"],
                "experience_level": "Mid-Senior level",
            },
            {
                "personal_info": {}, "default_answers": {}, "work_preferences": {},
                "candidate_profile": {
                    "years_of_experience": "5",
                    "skills": ["Python"],
                    "common_answers": {"education_level": "Bachelor's Degree"},
                },
                "ai_settings": {"enabled": False},
            },
            {
                "location": "New York, NY", "workplace_type": "Hybrid",
                "experience_level": "Mid-Senior level",
            },
        )
        self.assertEqual(result["score_source"], "deterministic profile-aware")
        self.assertTrue(any("Experience" in item for item in result["matched_requirements"]))
        self.assertTrue(any("Education" in item for item in result["matched_requirements"]))
        self.assertTrue(any("Work arrangement" in item for item in result["matched_requirements"]))

    async def test_ai_score_does_not_override_deterministic_eligibility(self):
        class ScoringProvider:
            available = True
            provider_name = "test-provider"

            async def generate_json(self, *args):
                return LLMResult(ok=True, needs_user_input=False, data={
                    "match_score": 10,
                    "matched_requirements": ["Communication"],
                    "missing_requirements": ["Certification"],
                    "important_notes": ["Manual review recommended"],
                    "confidence": 0.95,
                })

        scorer = JobMatchScorer(llm_provider=ScoringProvider())
        result = await scorer.evaluate(
            "Business Analyst", "Example", "Business analysis and communication",
            ["Business Analyst"],
            {"minimum_match_score": 60, "require_target_title_match": True},
            {"personal_info": {}, "default_answers": {}, "work_preferences": {},
             "ai_settings": {"minimum_confidence": 0.85}},
        )
        self.assertTrue(result["matches"])
        self.assertEqual(result["score"], 10)
        self.assertIn("eligibility remains deterministic", result["score_source"])


if __name__ == "__main__":
    unittest.main()
