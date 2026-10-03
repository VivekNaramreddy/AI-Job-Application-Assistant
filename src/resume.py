"""Validation and local persistence for resumes uploaded from the dashboard."""

import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict

from playwright.async_api import Locator


from src.paths import DATA_DIR
RESUMES_DIR = DATA_DIR / "resumes"
ALLOWED_EXTENSIONS = {".pdf", ".doc", ".docx"}
MAX_RESUME_BYTES = 10 * 1024 * 1024


@dataclass(frozen=True)
class ResumeSelection:
    path: Path
    filename: str


class ResumeManager:
    """Resolve and upload the configured resume without guessing a file."""

    @staticmethod
    def configured_resume(personal_info: Dict[str, Any]) -> ResumeSelection:
        raw_path = str(personal_info.get("resume_path", "")).strip()
        if not raw_path:
            raise ValueError("No resume is configured in the candidate profile.")
        path = Path(raw_path).expanduser()
        if not path.is_absolute():
            raise ValueError("The configured resume path must be absolute.")
        if path.suffix.lower() not in ALLOWED_EXTENSIONS:
            raise ValueError("Configured resume must be a PDF, DOC, or DOCX file.")
        if not path.is_file():
            raise ValueError(f"Configured resume does not exist: {path}")
        return ResumeSelection(path=path.resolve(), filename=path.name)

    @staticmethod
    async def upload(field: Locator, selection: ResumeSelection) -> str:
        await field.set_input_files(str(selection.path))
        if not (await field.input_value()).strip():
            raise RuntimeError("LinkedIn did not retain the selected resume file.")
        return selection.filename


def save_uploaded_resume(filename: str, content: bytes) -> Path:
    """Validate an uploaded resume and save it under the application's data folder."""
    suffix = Path(filename).suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        raise ValueError("Resume must be a PDF, DOC, or DOCX file.")
    if not content:
        raise ValueError("The selected resume is empty.")
    if len(content) > MAX_RESUME_BYTES:
        raise ValueError("Resume must be 10 MB or smaller.")
    if suffix == ".pdf" and not content.startswith(b"%PDF"):
        raise ValueError("The selected file is not a valid PDF.")
    if suffix == ".docx" and not content.startswith(b"PK"):
        raise ValueError("The selected file is not a valid DOCX document.")
    if suffix == ".doc" and not content.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        raise ValueError("The selected file is not a valid DOC document.")

    clean_stem = re.sub(r"[^a-zA-Z0-9_-]+", "_", Path(filename).stem).strip("_")
    clean_stem = clean_stem[:60] or "resume"
    RESUMES_DIR.mkdir(parents=True, exist_ok=True)
    destination = RESUMES_DIR / f"{clean_stem}_{uuid.uuid4().hex[:8]}{suffix}"
    destination.write_bytes(content)
    return destination.resolve()
