"""Deterministic job matching with optional, non-authoritative AI enrichment."""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from src.candidate_profile import CandidateProfile
from src.evaluation import JobEvaluator, RuleBasedJobEvaluator
from src.llm import LLMProvider, create_llm_provider


MATCH_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "match_score": {"type": "integer", "minimum": 0, "maximum": 100},
        "matched_requirements": {"type": "array", "items": {"type": "string"}},
        "missing_requirements": {"type": "array", "items": {"type": "string"}},
        "important_notes": {"type": "array", "items": {"type": "string"}},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
    "required": [
        "match_score", "matched_requirements", "missing_requirements",
        "important_notes", "confidence",
    ],
}


class JobMatchScorer:
    """Keep deterministic eligibility decisions and optionally enrich their score."""

    def __init__(
        self,
        evaluator: Optional[JobEvaluator] = None,
        llm_provider: Optional[LLMProvider] = None,
    ):
        self.evaluator = evaluator or RuleBasedJobEvaluator()
        self.llm_provider = llm_provider

    async def evaluate_search(
        self, title: str, company: str, description: str, roles: List[str],
        preferences: Dict[str, Any], profile_data: Dict[str, Any],
        job_context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Search routing uses one final score; Autopilot keeps evaluate().

        Title contributes 15 points, responsibilities 40, skills/tools 25,
        experience 12 and education 8. Unavailable profile/requirement dimensions
        are omitted from the denominator rather than inventing candidate facts.
        Explicit company/keyword exclusions and required keywords still apply.
        """
        relaxed = {**preferences, "require_target_title_match": False, "minimum_match_score": 0}
        result = await self.evaluate(title, company, description, roles, relaxed, profile_data, job_context)
        candidate = CandidateProfile.from_config(profile_data)
        normalize = RuleBasedJobEvaluator._normalise_text
        corpus = f" {normalize(description)} "
        title_tokens = set(normalize(title).split())
        # Small responsibility vocabulary bridges adjacent analytical roles;
        # other role terms retain literal word matching.
        concepts = {
            "analyst": ("analysis", "analytics", "analyze", "analyse", "analyst", "insights", "research"),
            "data": ("data", "databases", "analytics", "statistics"),
            "marketing": ("marketing", "campaigns", "advertising", "market research", "attribution", "seo"),
            "business": ("business", "commercial", "strategy", "requirements", "stakeholders", "operations"),
        }
        title_similarity = responsibility_fit = 0.0
        for role in roles:
            terms = set(normalize(role).split()) - {"senior", "junior", "sr", "jr", "entry", "level", "lead"}
            if not terms:
                continue
            title_similarity = max(title_similarity, len(terms & title_tokens) / len(terms))
            coverage = sum(any(f" {word} " in corpus for word in concepts.get(term, (term,))) for term in terms)
            responsibility_fit = max(responsibility_fit, coverage / len(terms))
        components = {"Title similarity": (15 * title_similarity, 15),
                      "Description/responsibilities": (40 * responsibility_fit, 40)}
        skills = list(dict.fromkeys(normalize(str(skill)) for skill in candidate.skills if str(skill).strip()))
        if skills:
            present = [skill for skill in skills if f" {skill} " in corpus]
            components["Skills/tools"] = (25 * min(1, len(present) / min(4, len(skills))), 25)
        required_years = [int(value) for value in re.findall(
            r"\b(\d{1,2})\+?\s+years?(?:\s+of)?\s+experience\b", description.lower()
        )]
        years = _first_number(candidate.years_of_experience)
        if required_years and years is not None:
            components["Experience"] = (12 * min(1, years / max(1, max(required_years))), 12)
        education = _education_rank(candidate.value_for_intent("education_level").lower())
        required_education = _education_rank(description.lower())
        if education and required_education:
            components["Education"] = (8 * min(1, education / required_education), 8)
        score = round(100 * sum(value for value, _ in components.values())
                      / sum(weight for _, weight in components.values()))
        minimum = min(100, max(0, int(preferences.get("minimum_match_score", 60))))
        # evaluate() received threshold zero and title gating disabled, so these
        # remaining reasons represent explicit user filters, not an earlier score.
        reasons = list(result["reasons"])
        if score < minimum:
            reasons.append(f"match score {score} is below minimum {minimum}")
        exact_role = any(RuleBasedJobEvaluator._role_matches_title(role, normalize(title)) for role in roles)
        possible = not exact_role and not reasons
        notes = list(result.get("important_notes", []))
        notes.append("Weighted score: " + "; ".join(
            f"{name} {value:.1f}/{weight}" for name, (value, weight) in components.items()
        ) + " (normalized over available evidence)")
        if possible:
            notes.append("Possible Match: adjacent title; overall job fit meets the configured minimum.")
        if result.get("ai_confidence") is not None:
            notes.append("AI notes are advisory; the weighted search score controls routing.")
        return {**result, "score": score, "matches": not reasons, "reasons": reasons,
                "classification": "Possible Match" if possible else ("Match" if not reasons else "Low Match"),
                "score_source": "weighted search v1", "important_notes": notes}

    async def evaluate(
        self,
        title: str,
        company: str,
        description: str,
        roles: List[str],
        preferences: Dict[str, Any],
        profile_data: Dict[str, Any],
        job_context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        base = self.evaluator.evaluate(title, company, description, roles, preferences)
        base = self._enrich_deterministic(
            base, title, description, preferences, profile_data, job_context or {}
        )
        provider = self.llm_provider or create_llm_provider(profile_data)
        if not provider.available:
            return base
        candidate = CandidateProfile.from_config(profile_data)
        facts = candidate.verified_facts()
        # Matching never needs direct identifiers or contact details.
        for private_section in ("personal_information", "contact_information", "social_profiles", "portfolio", "resume_files"):
            facts.pop(private_section, None)
        result = await provider.generate_json(
            {
                "task": "Compare this job with verified candidate facts.",
                "job": {
                    "title": title,
                    "company": company,
                    "description": description[:12_000],
                },
                "target_roles": roles,
                "preferences": {
                    key: preferences.get(key)
                    for key in (
                        "required_keywords", "preferred_keywords", "target_locations",
                        "workplace_types", "experience_level"
                    )
                },
                "verified_candidate_facts": facts,
                "rules": [
                    "Do not infer missing candidate facts.",
                    "List unmet requirements explicitly.",
                    "Score 0 to 100.",
                ],
            },
            MATCH_SCHEMA,
            "job_match",
        )
        if not result.ok or not result.data:
            enriched = dict(base)
            enriched["important_notes"] = list(base.get("important_notes", [])) + [
                f"Optional AI scoring unavailable ({result.error or 'invalid response'}); deterministic score retained."
            ]
            return enriched
        data = result.data
        try:
            confidence = float(data.get("confidence", 0))
            ai_score = max(0, min(100, int(data.get("match_score", base["score"]))))
        except (TypeError, ValueError):
            return base
        minimum = float(profile_data.get("ai_settings", {}).get("minimum_confidence", 0.85))
        if confidence < minimum:
            enriched = dict(base)
            enriched["important_notes"] = list(base.get("important_notes", [])) + [
                "Optional AI match result was below the configured confidence threshold."
            ]
            return enriched
        return {
            **base,
            # Eligibility remains the deterministic result, so an AI score alone
            # can never reject a job.
            "score": ai_score,
            "matched_requirements": _merge(
                base.get("matched_requirements", []), data.get("matched_requirements", [])
            ),
            "missing_requirements": _merge(
                base.get("missing_requirements", []), data.get("missing_requirements", [])
            ),
            "important_notes": _merge(
                base.get("important_notes", []), data.get("important_notes", [])
            ),
            "score_source": f"{provider.provider_name} (eligibility remains deterministic)",
            "ai_confidence": confidence,
        }

    @staticmethod
    def _enrich_deterministic(
        assessment: Dict[str, Any],
        title: str,
        description: str,
        preferences: Dict[str, Any],
        profile_data: Dict[str, Any],
        job_context: Dict[str, Any],
    ) -> Dict[str, Any]:
        result = dict(assessment)
        matched = list(result.get("matched_requirements", []))
        missing = list(result.get("missing_requirements", []))
        notes = list(result.get("important_notes", []))
        candidate = CandidateProfile.from_config(profile_data)
        corpus = f"{title} {description}".lower()
        score_delta = 0

        skills = [str(skill).strip() for skill in candidate.skills if str(skill).strip()]
        present_skills = [skill for skill in skills if skill.lower() in corpus]
        if present_skills:
            matched.append("Candidate skills: " + ", ".join(present_skills[:8]))
            score_delta += min(8, len(present_skills))

        required_years = [int(value) for value in re.findall(
            r"\b(\d{1,2})\+?\s+years?(?:\s+of)?\s+experience\b", corpus
        )]
        candidate_years = _first_number(candidate.years_of_experience)
        if required_years and candidate_years is not None:
            minimum_years = max(required_years)
            if candidate_years >= minimum_years:
                matched.append(f"Experience: {candidate_years} years meets {minimum_years}-year requirement")
                score_delta += 8
            else:
                missing.append(f"Experience: posting mentions {minimum_years} years; profile has {candidate_years}")
                score_delta -= 12

        education = candidate.value_for_intent("education_level")
        required_education = _education_rank(corpus)
        candidate_education = _education_rank(education.lower())
        if education and required_education:
            if candidate_education >= required_education:
                matched.append(f"Education: {education}")
                score_delta += 5
            else:
                missing.append(f"Education requirement may exceed profile value: {education}")
                score_delta -= 8

        location = str(job_context.get("location", "")).strip()
        target_locations = [str(v).strip() for v in preferences.get("target_locations", []) if str(v).strip()]
        if location and target_locations:
            if any(value.lower() in location.lower() or location.lower() in value.lower() for value in target_locations):
                matched.append(f"Location: {location}")
                score_delta += 4
            else:
                notes.append(f"Location needs review: {location}")

        workplace = str(job_context.get("workplace_type", "")).strip()
        wanted_workplaces = [str(v).strip() for v in preferences.get("workplace_types", []) if str(v).strip()]
        if workplace and wanted_workplaces:
            if any(workplace.lower() == value.lower() for value in wanted_workplaces):
                matched.append(f"Work arrangement: {workplace}")
                score_delta += 4
            else:
                notes.append(f"Work arrangement needs review: {workplace}")

        seniority = str(job_context.get("experience_level", "")).strip()
        preferred_seniority = str(preferences.get("experience_level", "")).strip()
        if seniority and preferred_seniority and preferred_seniority.lower() != "any":
            if seniority.lower() == preferred_seniority.lower():
                matched.append(f"Seniority: {seniority}")
                score_delta += 3
            else:
                notes.append(f"Seniority needs review: posting is {seniority}, preference is {preferred_seniority}")

        result["matched_requirements"] = _merge([], matched)
        result["missing_requirements"] = _merge([], missing)
        result["important_notes"] = _merge([], notes)
        if score_delta:
            result["score"] = max(0, min(100, int(result.get("score", 0)) + score_delta))
            result["score_source"] = "deterministic profile-aware"
        return result


def _merge(left: Any, right: Any) -> List[str]:
    values = []
    for item in list(left or []) + list(right or []):
        clean = str(item).strip()
        if clean and clean not in values:
            values.append(clean)
    return values


def _first_number(value: Any) -> Optional[int]:
    match = re.search(r"\d+", str(value or ""))
    return int(match.group()) if match else None


def _education_rank(text: str) -> int:
    levels = (
        (5, ("phd", "doctorate", "doctoral")),
        (4, ("master", "mba", "m.s", "m.a")),
        (3, ("bachelor", "b.s", "b.a", "undergraduate degree")),
        (2, ("associate degree",)),
        (1, ("high school", "ged")),
    )
    for rank, terms in levels:
        if any(term in text for term in terms):
            return rank
    return 0
