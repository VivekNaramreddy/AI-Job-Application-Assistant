"""Small in-process status snapshot used by the existing dashboard."""

from dataclasses import asdict, dataclass
from threading import Lock
from typing import Any, Dict


@dataclass
class RuntimeSnapshot:
    current_job: str = ""
    company: str = ""
    current_ats: str = ""
    current_action: str = "Idle"
    current_step: int = 0
    fields_detected: int = 0
    fields_resolved: int = 0
    unresolved_questions: int = 0
    jobs_reviewed: int = 0
    applications_started: int = 0
    ready_for_review: int = 0
    failures: int = 0


class RuntimeStatus:
    def __init__(self) -> None:
        self._lock = Lock()
        self._snapshot = RuntimeSnapshot()

    def reset(self) -> None:
        with self._lock:
            self._snapshot = RuntimeSnapshot(current_action="Starting")

    def update(self, **values: Any) -> None:
        with self._lock:
            for key, value in values.items():
                if hasattr(self._snapshot, key):
                    setattr(self._snapshot, key, value)

    def increment(self, field: str, amount: int = 1) -> None:
        with self._lock:
            if hasattr(self._snapshot, field):
                setattr(
                    self._snapshot,
                    field,
                    int(getattr(self._snapshot, field)) + amount,
                )

    def as_dict(self) -> Dict[str, Any]:
        with self._lock:
            return asdict(self._snapshot)


runtime_status = RuntimeStatus()
