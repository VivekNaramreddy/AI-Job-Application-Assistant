"""Deterministic-first semantic answer resolution."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Optional

from src.answer_memory import AnswerMemoryRepository, answer_memory, normalize_question
from src.application import AnswerResolution
from src.candidate_profile import CandidateProfile


PROTECTED_FACT_INTENTS = {
    "address", "certification", "citizenship", "desired_salary", "education_level",
    "email", "employment_dates", "employment_history", "employer", "first_name",
    "full_name", "job_title", "last_name", "location", "phone", "postal_code",
    "requires_sponsorship", "salary_history", "school", "security_clearance",
    "state", "visa_status", "work_authorization", "years_of_experience",
}
AI_GENERATIVE_INTENTS = {
    "job_interest", "motivation", "professional_summary", "short_response"
}


@dataclass(frozen=True)
class ClassifiedIntent:
    name: str = ""
    confidence: float = 0.0


class IntentClassifier:
    """Map common wording variants to stable candidate-fact intents."""

    PATTERNS = (
        ("requires_sponsorship", r"\b(sponsor|sponsorship)\b"),
        ("work_authorization", r"\b(authori[sz](?:ed|ation)|legal(?:ly)? eligible|right to work)\b"),
        ("security_clearance", r"\b(security clearance|clearance level|active clearance)\b"),
        ("visa_status", r"\b(visa status|immigration status|visa type)\b"),
        ("citizenship", r"\b(citizen|citizenship|nationality)\b"),
        ("years_of_experience", r"\b(years?|yrs?)\b.*\bexperience\b|\bexperience\b.*\b(years?|yrs?)\b"),
        ("desired_salary", r"\b(salary|compensation|desired pay|pay expectation)\b"),
        ("notice_period", r"\b(notice period|weeks? notice)\b"),
        ("available_start_date", r"\b(start date|available to start|availability date)\b"),
        ("education_level", r"\b(education|degree|highest qualification)\b"),
        ("relocation", r"\b(relocat|willing to move)"),
        ("remote_preference", r"\b(remote|hybrid|work arrangement)\b.*\b(prefer|preference|willing)\b"),
        ("linkedin_url", r"\blinkedin\b"),
        ("portfolio_url", r"\b(portfolio|personal website|website url)\b"),
        ("email", r"\be[ -]?mail\b"),
        ("phone", r"\b(phone|mobile|telephone)\b"),
        ("postal_code", r"\b(postal|zip)\b"),
        ("address", r"\b(street address|address line|mailing address)\b"),
        ("city", r"\bcity\b"),
        ("state", r"\b(state|province)\b"),
        ("country", r"\bcountry\b"),
        ("location", r"\b(location|where do you live|current residence)\b"),
        ("first_name", r"\b(first|given) name\b"),
        ("last_name", r"\b(last|family) name\b|\bsurname\b"),
        ("full_name", r"\b(full|legal) name\b|^name$"),
    )

    @classmethod
    def classify(cls, question: str) -> ClassifiedIntent:
        normalized = normalize_question(question)
        for intent, pattern in cls.PATTERNS:
            if re.search(pattern, normalized):
                if intent == "years_of_experience":
                    skill = cls._specific_experience_skill(normalized)
                    if skill:
                        return ClassifiedIntent(f"experience_skill:{skill}", 0.95)
                return ClassifiedIntent(intent, 0.98)
        return ClassifiedIntent()

    @staticmethod
    def _specific_experience_skill(question: str) -> str:
        match = re.search(
            r"experience (?:using|with|in) ([a-z0-9 +#.-]{2,40})", question
        )
        if not match:
            return ""
        return re.sub(r"\b(do you have|have you|required|preferred)\b.*$", "", match.group(1)).strip()


class AnswerResolver:
    """Apply profile, approved memory, rules, resume facts, then optional AI."""

    def __init__(
        self,
        memory: Optional[AnswerMemoryRepository] = None,
        llm_provider: Any = None,
        minimum_confidence: float = 0.85,
    ):
        self.memory = memory or answer_memory
        self.llm_provider = llm_provider
        self.minimum_confidence = minimum_confidence

    def resolve_deterministic(
        self, question: str, profile_data: Dict[str, Any]
    ) -> AnswerResolution:
        intent = IntentClassifier.classify(question)
        profile = CandidateProfile.from_config(profile_data)
        if intent.name:
            value = profile.value_for_intent(intent.name)
            if value:
                return AnswerResolution(value, "candidate profile", 1.0)
        remembered = self.memory.find_approved(question, intent.name)
        if remembered:
            return AnswerResolution(
                remembered.answer, "user-approved answer memory", remembered.confidence
            )
        value = self._deterministic_rule(question, profile)
        if value:
            return AnswerResolution(value, "system-derived", 0.95)
        resume_value = self._resume_fact(profile, intent.name)
        if resume_value:
            return AnswerResolution(resume_value, "resume-derived", 0.9)
        return AnswerResolution()

    async def resolve(
        self,
        question: str,
        profile_data: Dict[str, Any],
        options: Optional[Iterable[str]] = None,
    ) -> AnswerResolution:
        deterministic = self.resolve_deterministic(question, profile_data)
        if deterministic.value:
            return deterministic
        if self.llm_provider is None or not getattr(self.llm_provider, "available", False):
            return AnswerResolution()
        intent = IntentClassifier.classify(question)
        profile = CandidateProfile.from_config(profile_data)
        result = await self.llm_provider.interpret_question(
            question=question,
            verified_facts=profile.verified_facts(),
            options=list(options or []),
        )
        if not result.ok or result.confidence < self.minimum_confidence:
            return AnswerResolution()
        resolved_intent = result.canonical_intent or intent.name
        if resolved_intent not in AI_GENERATIVE_INTENTS:
            verified = profile.value_for_intent(resolved_intent) or self._resume_fact(
                profile, resolved_intent
            )
            if not verified or normalize_question(verified) != normalize_question(result.answer):
                return AnswerResolution()
        option_values = list(options or [])
        if option_values and not any(
            normalize_question(option) == normalize_question(result.answer)
            for option in option_values
        ):
            return AnswerResolution()
        return AnswerResolution(result.answer.strip(), "AI-generated", result.confidence)

    @staticmethod
    def _resume_fact(profile: CandidateProfile, intent: str) -> str:
        value = profile.resume_facts.get(intent, "")
        return str(value or "").strip()

    @staticmethod
    def _deterministic_rule(question: str, profile: CandidateProfile) -> str:
        normalized = normalize_question(question)
        if "preferred name" in normalized:
            return profile.personal_information.full_name.strip()
        return ""
