"""Shared domain records passed through discovery, review, history, and export."""

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Dict, Optional


def current_timestamp() -> str:
    """Return an ISO timestamp with the machine's local timezone."""
    return datetime.now().astimezone().isoformat(timespec="seconds")


@dataclass
class JobRecord:
    """Normalized job data retained throughout one job's lifecycle."""

    job_url: str
    job_id: Optional[int] = None
    external_job_id: str = ""
    title: str = ""
    company: str = ""
    location: str = ""
    application_url: str = ""
    application_method: str = ""
    application_stage: str = ""
    automation_failure_reason: str = ""
    source: str = ""
    source_mode: str = "autopilot"
    original_bucket: str = ""
    description: str = ""
    status: str = "DISCOVERED"
    match_score: Optional[int] = None
    workplace_type: str = ""
    employment_type: str = ""
    experience_level: str = ""
    salary: str = ""
    posted_date: str = ""
    date_discovered: str = field(default_factory=current_timestamp)
    date_reviewed: str = ""
    date_application_started: str = ""
    date_applied: str = ""
    resume_used: str = ""
    failure_reason: str = ""
    notes: str = ""
    ats_type: str = ""
    easy_apply: Optional[bool] = None
    external_apply: Optional[bool] = None

    def mark_reviewed(self) -> None:
        self.status = "REVIEWED"
        if not self.date_reviewed:
            self.date_reviewed = current_timestamp()

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)
