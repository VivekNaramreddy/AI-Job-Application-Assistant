"""Central, versioned candidate profile and deterministic fact lookup."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Dict, List, Tuple

from pydantic import BaseModel, ConfigDict, Field


class ProfileSection(BaseModel):
    """Allow future profile fields without breaking older installations."""

    model_config = ConfigDict(extra="allow")


class PersonalInformation(ProfileSection):
    full_name: str = ""


class ContactInformation(ProfileSection):
    email: str = ""
    phone: str = ""


class CandidateLocation(ProfileSection):
    address: str = ""
    city: str = ""
    state: str = ""
    postal_code: str = ""
    city_state: str = ""
    country: str = ""


class WorkAuthorization(ProfileSection):
    authorized: str = ""
    requires_sponsorship: str = ""
    visa_status: str = ""
    citizenship: str = ""
    security_clearance: str = ""


class CandidateProfile(ProfileSection):
    """Single source of verified candidate facts used by answer resolution."""

    schema_version: int = 2
    personal_information: PersonalInformation = Field(default_factory=PersonalInformation)
    contact_information: ContactInformation = Field(default_factory=ContactInformation)
    location: CandidateLocation = Field(default_factory=CandidateLocation)
    work_authorization: WorkAuthorization = Field(default_factory=WorkAuthorization)
    education: List[Dict[str, Any]] = Field(default_factory=list)
    employment_history: List[Dict[str, Any]] = Field(default_factory=list)
    skills: List[str] = Field(default_factory=list)
    years_of_experience: str = ""
    certifications: List[Dict[str, Any]] = Field(default_factory=list)
    salary_preferences: Dict[str, Any] = Field(default_factory=dict)
    relocation_preferences: Dict[str, Any] = Field(default_factory=dict)
    remote_preferences: Dict[str, Any] = Field(default_factory=dict)
    job_preferences: Dict[str, Any] = Field(default_factory=dict)
    social_profiles: Dict[str, str] = Field(default_factory=dict)
    portfolio: Dict[str, str] = Field(default_factory=dict)
    resume_files: List[Dict[str, str]] = Field(default_factory=list)
    cover_letters: List[Dict[str, str]] = Field(default_factory=list)
    common_answers: Dict[str, str] = Field(default_factory=dict)
    resume_facts: Dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def from_config(cls, profile: Dict[str, Any]) -> "CandidateProfile":
        document, _ = ensure_candidate_profile(profile)
        return cls.model_validate(document)

    def verified_facts(self) -> Dict[str, Any]:
        """Return only non-empty facts suitable for bounded semantic assistance."""
        raw = self.model_dump()
        return _remove_empty(raw)

    def value_for_intent(self, intent: str) -> str:
        """Resolve a canonical application intent without inferring missing facts."""
        full_name = self.personal_information.full_name.strip()
        name_parts = full_name.split()
        city_state = self.location.city_state.strip()
        city_parts = [part.strip() for part in city_state.split(",", 1)]
        city = self.location.city.strip() or (city_parts[0] if city_parts else "")
        state = self.location.state.strip() or (
            city_parts[1] if len(city_parts) > 1 else ""
        )
        education_level = self.common_answers.get("education_level", "")
        if not education_level and self.education:
            education_level = str(self.education[0].get("degree", ""))
        resume_path = ""
        if self.resume_files:
            resume_path = str(self.resume_files[0].get("path", ""))

        values = {
            "full_name": full_name,
            "first_name": name_parts[0] if name_parts else "",
            "last_name": name_parts[-1] if len(name_parts) > 1 else "",
            "email": self.contact_information.email,
            "phone": self.contact_information.phone,
            "address": self.location.address,
            "city": city,
            "state": state,
            "postal_code": self.location.postal_code,
            "location": city_state,
            "country": self.location.country,
            "work_authorization": self.work_authorization.authorized,
            "requires_sponsorship": self.work_authorization.requires_sponsorship,
            "visa_status": self.work_authorization.visa_status,
            "citizenship": self.work_authorization.citizenship,
            "security_clearance": self.work_authorization.security_clearance,
            "years_of_experience": self.years_of_experience,
            "desired_salary": self.salary_preferences.get("desired_salary", ""),
            "relocation": self.relocation_preferences.get("willing_to_relocate", ""),
            "remote_preference": self.remote_preferences.get("preference", ""),
            "linkedin_url": self.social_profiles.get("linkedin_url", ""),
            "portfolio_url": self.portfolio.get("url", ""),
            "resume_path": resume_path,
            "education_level": education_level,
            "notice_period": self.common_answers.get("notice_period_weeks", ""),
            "available_start_date": self.common_answers.get("available_start_date", ""),
        }
        value = values.get(intent, self.common_answers.get(intent, ""))
        return str(value or "").strip()


def ensure_candidate_profile(profile: Dict[str, Any]) -> Tuple[Dict[str, Any], bool]:
    """Create the canonical projection from existing data without inventing facts."""
    existing = profile.get("candidate_profile")
    document: Dict[str, Any] = deepcopy(existing) if isinstance(existing, dict) else {}
    original = deepcopy(document)
    document.setdefault("schema_version", 2)
    for name, default in (
        ("personal_information", {}),
        ("contact_information", {}),
        ("location", {}),
        ("work_authorization", {}),
        ("education", []),
        ("employment_history", []),
        ("skills", []),
        ("certifications", []),
        ("salary_preferences", {}),
        ("relocation_preferences", {}),
        ("remote_preferences", {}),
        ("job_preferences", {}),
        ("social_profiles", {}),
        ("portfolio", {}),
        ("resume_files", []),
        ("cover_letters", []),
        ("common_answers", {}),
        ("resume_facts", {}),
    ):
        document.setdefault(name, deepcopy(default))
    document.setdefault("years_of_experience", "")

    personal = profile.get("personal_info", {})
    defaults = profile.get("default_answers", {})
    preferences = profile.get("work_preferences", {})
    _set_missing(document["personal_information"], "full_name", personal.get("full_name"))
    _set_missing(document["contact_information"], "email", personal.get("email"))
    _set_missing(document["contact_information"], "phone", personal.get("phone"))
    for key in ("address", "city", "state", "postal_code", "city_state", "country"):
        _set_missing(document["location"], key, personal.get(key))
    _set_missing(document["work_authorization"], "authorized", defaults.get("work_authorization"))
    _set_missing(
        document["work_authorization"],
        "requires_sponsorship",
        defaults.get("requires_sponsorship"),
    )
    for key in ("visa_status", "citizenship", "security_clearance"):
        _set_missing(document["work_authorization"], key, defaults.get(key))
    if not document.get("years_of_experience"):
        document["years_of_experience"] = str(defaults.get("years_of_experience", "") or "")
    _set_missing(
        document["salary_preferences"], "desired_salary", defaults.get("desired_salary")
    )
    for key in ("notice_period_weeks", "education_level", "available_start_date"):
        _set_missing(document["common_answers"], key, defaults.get(key) or personal.get(key))
    _set_missing(document["social_profiles"], "linkedin_url", personal.get("linkedin_url"))
    _set_missing(document["portfolio"], "url", personal.get("portfolio_url"))
    resume_path = str(personal.get("resume_path", "") or "").strip()
    if resume_path and not document["resume_files"]:
        document["resume_files"] = [{"path": resume_path}]
    if not document["job_preferences"]:
        document["job_preferences"] = deepcopy(preferences)
    return document, document != original


def apply_legacy_updates(
    profile: Dict[str, Any], changed_sections: Dict[str, Any]
) -> Dict[str, Any]:
    """Keep the canonical profile synchronized with the existing dashboard API."""
    document, _ = ensure_candidate_profile(profile)
    personal = changed_sections.get("personal_info", {})
    defaults = changed_sections.get("default_answers", {})
    preferences = changed_sections.get("work_preferences")
    mapping = {
        "full_name": ("personal_information", "full_name"),
        "email": ("contact_information", "email"),
        "phone": ("contact_information", "phone"),
        "address": ("location", "address"),
        "city": ("location", "city"),
        "state": ("location", "state"),
        "postal_code": ("location", "postal_code"),
        "city_state": ("location", "city_state"),
        "country": ("location", "country"),
        "linkedin_url": ("social_profiles", "linkedin_url"),
        "portfolio_url": ("portfolio", "url"),
    }
    for source, (section, target) in mapping.items():
        if source in personal:
            document[section][target] = str(personal.get(source, "") or "")
    if "resume_path" in personal:
        path = str(personal.get("resume_path", "") or "").strip()
        document["resume_files"] = [{"path": path}] if path else []
    default_mapping = {
        "work_authorization": ("work_authorization", "authorized"),
        "requires_sponsorship": ("work_authorization", "requires_sponsorship"),
        "visa_status": ("work_authorization", "visa_status"),
        "citizenship": ("work_authorization", "citizenship"),
        "security_clearance": ("work_authorization", "security_clearance"),
        "desired_salary": ("salary_preferences", "desired_salary"),
        "notice_period_weeks": ("common_answers", "notice_period_weeks"),
        "education_level": ("common_answers", "education_level"),
    }
    if "years_of_experience" in defaults:
        document["years_of_experience"] = str(
            defaults.get("years_of_experience", "") or ""
        )
    for source, (section, target) in default_mapping.items():
        if source in defaults:
            document[section][target] = str(defaults.get(source, "") or "")
    if preferences is not None:
        document["job_preferences"] = deepcopy(preferences)
    return document


def _set_missing(target: Dict[str, Any], key: str, value: Any) -> None:
    if not target.get(key) and value not in (None, ""):
        target[key] = str(value)


def _remove_empty(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: cleaned
            for key, item in value.items()
            if (cleaned := _remove_empty(item)) not in ({}, [], "", None)
        }
    if isinstance(value, list):
        return [cleaned for item in value if (cleaned := _remove_empty(item)) not in ({}, [], "", None)]
    return value
