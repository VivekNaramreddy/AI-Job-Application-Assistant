"""
src/config.py
=============================================================================
Configuration & User Profile Manager

PYTHON LESSON:
- A `class` is a blueprint for storing data and methods (functions inside a class).
- `json` is a light-weight text format for storing structured data (like key-value pairs).
- `Path` from Python's built-in `pathlib` library makes working with file paths easy 
  across Windows, Mac, and Linux.
=============================================================================
"""

import json
import sys
import os
import tempfile
from threading import RLock
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict

from src.candidate_profile import CandidateProfile, apply_legacy_updates, ensure_candidate_profile

# Ensure stdout handles UTF-8 characters cleanly on Windows
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

# Define file path relative to project root
from src.paths import BASE_DIR, DATA_DIR
PROFILE_FILE = DATA_DIR / "user_profile.json"

# Default profile settings created on first run
DEFAULT_PROFILE: Dict[str, Any] = {
    "personal_info": {
        "full_name": "",
        "email": "",
        "phone": "",
        "city_state": "",
        "country": "",
        "linkedin_url": "",
        "portfolio_url": "",
        "resume_path": "",
    },
    "work_preferences": {
        "target_roles": [],
        "target_locations": [],
        # Workplace type is separate from employment type. LinkedIn calls these
        # On-site, Hybrid, and Remote.
        "workplace_types": ["Remote", "Hybrid", "On-site"],
        "job_types": ["Full-time"],
        "experience_years_range": "Any",
        "experience_level": "Any",
        "easy_apply_only": False,
        "parallel_google_search": False,
        "required_keywords": [],
        "preferred_keywords": [],
        "excluded_keywords": [],
        "excluded_companies": [],
        "require_target_title_match": True,
        "minimum_match_score": 60,
        "max_search_pages_per_query": 3,
        "max_applications_per_run": 25,
        "delay_between_applications_sec": 5,
        "approval_timeout_minutes": 15,
    },
    "default_answers": {
        "years_of_experience": "",
        "work_authorization": "",
        "requires_sponsorship": "",
        "desired_salary": "",
        "notice_period_weeks": "",
        "education_level": ""
    },
    "ai_settings": {
        "enabled": False,
        "provider": "none",
        "base_url": "",
        "model": "",
        "api_key_env": "OPENAI_API_KEY",
        "timeout_seconds": 10,
        "minimum_confidence": 0.85
    },
    "candidate_profile": {}
}


class ConfigManager:
    """Manages loading, updating, and saving user profile & app settings."""

    def __init__(self):
        """Initializes the ConfigManager and ensures default settings exist."""
        # Ensure the data directory exists
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.profile: Dict[str, Any] = {}
        self._lock = RLock()
        self.load_profile()

    def load_profile(self) -> Dict[str, Any]:
        """Loads user settings from user_profile.json or creates it if missing."""
        if PROFILE_FILE.exists():
            try:
                with open(PROFILE_FILE, "r", encoding="utf-8") as f:
                    self.profile = json.load(f)
                if not isinstance(self.profile, dict):
                    raise ValueError("Profile must be a JSON object.")
                for section in ("personal_info", "default_answers", "work_preferences", "ai_settings", "candidate_profile"):
                    if not isinstance(self.profile.get(section, {}), dict):
                        raise ValueError(f"Profile section {section} must be an object.")
                # Add newly introduced preferences without overwriting any of
                # the user's existing profile values.
                work_preferences = self.profile.setdefault("work_preferences", {})
                changed = False
                for key in (
                    "workplace_types",
                    "job_types",
                    "experience_years_range",
                    "experience_level",
                    "easy_apply_only",
                    "required_keywords",
                    "preferred_keywords",
                    "excluded_keywords",
                    "excluded_companies",
                    "require_target_title_match",
                    "minimum_match_score",
                    "max_search_pages_per_query",
                    "approval_timeout_minutes",
                ):
                    if key not in work_preferences:
                        work_preferences[key] = DEFAULT_PROFILE["work_preferences"][key]
                        changed = True
                # LinkedIn-only mode intentionally disables the legacy Google
                # discovery producer, even in profiles created by older builds.
                if work_preferences.get("parallel_google_search") is not False:
                    work_preferences["parallel_google_search"] = False
                    changed = True
                ai_settings = self.profile.setdefault("ai_settings", {})
                for key, value in DEFAULT_PROFILE["ai_settings"].items():
                    if key not in ai_settings:
                        ai_settings[key] = value
                        changed = True
                candidate_profile, migrated = ensure_candidate_profile(self.profile)
                CandidateProfile.model_validate(candidate_profile)
                if migrated or self.profile.get("candidate_profile") != candidate_profile:
                    self.profile["candidate_profile"] = candidate_profile
                    changed = True
                if changed:
                    self.save_profile()
            except (OSError, ValueError, TypeError, AttributeError) as e:
                raise RuntimeError(
                    f"Cannot load profile {PROFILE_FILE}: {e}. "
                    "The file was preserved. Repair its JSON or restore a backup before restarting."
                ) from e
        else:
            print(f"[Info] Creating default profile at {PROFILE_FILE}")
            self.profile = deepcopy(DEFAULT_PROFILE)
            candidate_profile, _ = ensure_candidate_profile(self.profile)
            self.profile["candidate_profile"] = candidate_profile
            self.save_profile()
        
        return self.profile

    def save_profile(self) -> None:
        """Saves current memory settings to user_profile.json."""
        with self._lock:
            handle, name = tempfile.mkstemp(dir=PROFILE_FILE.parent, suffix=".tmp")
            try:
                with os.fdopen(handle, "w", encoding="utf-8") as f:
                    json.dump(self.profile, f, indent=4)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(name, PROFILE_FILE)
            finally:
                Path(name).unlink(missing_ok=True)
        print(f"[OK] User profile saved to {PROFILE_FILE}")

    def update_profile(self, new_data: Dict[str, Any]) -> Dict[str, Any]:
        """Updates profile with new key-value data and persists to disk immediately."""
        with self._lock:
            previous = deepcopy(self.profile)
            try:
                return self._update_profile(new_data)
            except Exception:
                self.profile = previous
                raise

    def _update_profile(self, new_data: Dict[str, Any]) -> Dict[str, Any]:
        # Update sub-dictionaries if present
        for section in [
            "personal_info", "work_preferences", "default_answers", "ai_settings"
        ]:
            if section in new_data:
                self.profile.setdefault(section, {}).update(new_data[section])

        if isinstance(new_data.get("candidate_profile"), dict):
            self.profile["candidate_profile"] = new_data["candidate_profile"]
        self.profile["candidate_profile"] = apply_legacy_updates(
            self.profile, new_data
        )
        CandidateProfile.model_validate(self.profile["candidate_profile"])

        self.save_profile()
        return self.profile


# Singleton instance for easy import across the application
config_manager = ConfigManager()
