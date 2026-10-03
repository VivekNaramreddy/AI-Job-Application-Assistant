"""Replaceable job-evaluation providers for the application pipeline."""

import re
from typing import Any, Dict, List, Protocol


class JobEvaluator(Protocol):
    """Contract implemented by rule-based and future AI evaluators."""

    def evaluate(
        self,
        title: str,
        company: str,
        description: str,
        roles: List[str],
        preferences: Dict[str, Any],
    ) -> Dict[str, Any]: ...


class RuleBasedJobEvaluator:
    """Current deterministic evaluator; safe fallback when no AI is configured."""

    def evaluate(
        self,
        title: str,
        company: str,
        description: str,
        roles: List[str],
        preferences: Dict[str, Any],
    ) -> Dict[str, Any]:
        title_text = self._normalise_text(title)
        company_text = self._normalise_text(company)
        corpus = self._normalise_text(f"{title} {company} {description}")
        reasons: List[str] = []
        matched_requirements: List[str] = []
        missing_requirements: List[str] = []
        important_notes: List[str] = []

        excluded_companies = self._clean_values(preferences.get("excluded_companies"))
        blocked_company = next(
            (
                value
                for value in excluded_companies
                if self._normalise_text(value) in company_text
            ),
            "",
        )
        if blocked_company:
            reasons.append(f"excluded company: {blocked_company}")
            important_notes.append(f"Excluded company matched: {blocked_company}")

        excluded_keywords = self._clean_values(preferences.get("excluded_keywords"))
        blocked_keyword = next(
            (
                value
                for value in excluded_keywords
                if self._normalise_text(value) in corpus
            ),
            "",
        )
        if blocked_keyword:
            reasons.append(f"excluded keyword: {blocked_keyword}")
            important_notes.append(f"Excluded keyword matched: {blocked_keyword}")

        required_keywords = self._clean_values(preferences.get("required_keywords"))
        missing = [
            value
            for value in required_keywords
            if self._normalise_text(value) not in corpus
        ]
        if missing:
            reasons.append("missing required keyword(s): " + ", ".join(missing))
            missing_requirements.extend(missing)
        matched_required = [
            value for value in required_keywords if self._normalise_text(value) in corpus
        ]
        matched_requirements.extend(matched_required)

        title_match = any(self._role_matches_title(role, title_text) for role in roles)
        if bool(preferences.get("require_target_title_match", True)) and not title_match:
            reasons.append("title does not match a target role")

        score = 70 if title_match else 40
        score += min(15, len(required_keywords) * 5)
        preferred = self._clean_values(preferences.get("preferred_keywords"))
        matched_preferred = [
            value for value in preferred if self._normalise_text(value) in corpus
        ]
        matched_requirements.extend(matched_preferred)
        score += min(
            15,
            len(matched_preferred) * 5,
        )
        score = min(100, score)
        minimum_score = min(
            100, max(0, int(preferences.get("minimum_match_score", 60)))
        )
        if score < minimum_score:
            reasons.append(f"match score {score} is below minimum {minimum_score}")

        if title_match:
            matched_requirements.insert(0, "Target job title")
        return {
            "matches": not reasons,
            "score": score,
            "reasons": reasons,
            "matched_requirements": list(dict.fromkeys(matched_requirements)),
            "missing_requirements": list(dict.fromkeys(missing_requirements)),
            "important_notes": important_notes,
            "score_source": "deterministic",
        }

    @classmethod
    def _role_matches_title(cls, role: str, normalised_title: str) -> bool:
        normalised_role = cls._normalise_text(role)
        if not normalised_role:
            return False
        if normalised_role in normalised_title:
            return True
        role_tokens = [token for token in normalised_role.split() if len(token) > 2]
        title_tokens = set(normalised_title.split())
        return bool(role_tokens) and all(token in title_tokens for token in role_tokens)

    @staticmethod
    def _clean_values(values: Any) -> List[str]:
        if not isinstance(values, list):
            return []
        return [str(value).strip() for value in values if str(value).strip()]

    @staticmethod
    def _normalise_text(value: str) -> str:
        return re.sub(r"[^a-z0-9]+", " ", str(value).lower()).strip()
