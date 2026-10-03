"""Shared LinkedIn application states and normalized form-field records."""

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Dict, List


class ApplicationState(str, Enum):
    APPLICATION_FOUND = "APPLICATION_FOUND"
    APPLICATION_STARTED = "APPLICATION_STARTED"
    FORM_ANALYZED = "FORM_ANALYZED"
    FILLING = "FILLING"
    NEEDS_USER_INPUT = "NEEDS_USER_INPUT"
    READY_FOR_REVIEW = "READY_FOR_REVIEW"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


@dataclass
class ApplicationField:
    """Serializable description of one visible application-form field."""

    key: str
    label: str
    field_type: str
    required: bool
    options: List[str] = field(default_factory=list)
    current_value: str = ""
    resolved_value: str = ""
    source: str = ""
    confidence: float = 0.0

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class AnswerResolution:
    value: str = ""
    source: str = ""
    confidence: float = 0.0
