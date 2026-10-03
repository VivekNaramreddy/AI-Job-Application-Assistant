"""SQLite-backed, approval-aware application answer memory."""

from __future__ import annotations

import json
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from src import database as database_module


def normalize_question(text: str) -> str:
    text = re.sub(r"[^a-z0-9]+", " ", str(text).lower())
    return " ".join(text.split())


@dataclass(frozen=True)
class AnswerMemoryRecord:
    original_question: str
    normalized_question: str
    canonical_intent: str
    answer: str
    answer_source: str
    confidence: float
    user_approved: bool
    last_used: str

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


class AnswerMemoryRepository:
    """Persist and retrieve only explicitly approved reusable answers."""

    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = Path(db_path) if db_path else None
        self.init_db()

    @property
    def path(self) -> Path:
        return self.db_path or database_module.DB_FILE

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def init_db(self) -> None:
        with self.connection() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS answer_memory (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    original_question TEXT NOT NULL,
                    normalized_question TEXT NOT NULL UNIQUE,
                    canonical_intent TEXT,
                    answer TEXT NOT NULL,
                    answer_source TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    user_approved INTEGER NOT NULL DEFAULT 0,
                    last_used TEXT,
                    use_count INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_answer_memory_intent "
                "ON answer_memory(canonical_intent, user_approved)"
            )

    def save(
        self,
        original_question: str,
        answer: str,
        canonical_intent: str = "",
        answer_source: str = "user-approved",
        confidence: float = 1.0,
        user_approved: bool = True,
    ) -> AnswerMemoryRecord:
        normalized = normalize_question(original_question)
        clean_answer = str(answer).strip()
        if not normalized or not clean_answer:
            raise ValueError("Question and answer are required.")
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        with self.connection() as conn:
            conn.execute(
                """
                INSERT INTO answer_memory (
                    original_question, normalized_question, canonical_intent,
                    answer, answer_source, confidence, user_approved,
                    last_used, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(normalized_question) DO UPDATE SET
                    original_question = excluded.original_question,
                    canonical_intent = excluded.canonical_intent,
                    answer = excluded.answer,
                    answer_source = excluded.answer_source,
                    confidence = excluded.confidence,
                    user_approved = excluded.user_approved,
                    last_used = excluded.last_used
                """,
                (
                    original_question.strip(),
                    normalized,
                    canonical_intent.strip(),
                    clean_answer,
                    answer_source,
                    max(0.0, min(1.0, float(confidence))),
                    int(user_approved),
                    now,
                    now,
                ),
            )
        return AnswerMemoryRecord(
            original_question.strip(), normalized, canonical_intent.strip(),
            clean_answer, answer_source, max(0.0, min(1.0, float(confidence))),
            user_approved, now,
        )

    def find_approved(
        self,
        question: str,
        canonical_intent: str = "",
        similarity_threshold: float = 0.72,
    ) -> Optional[AnswerMemoryRecord]:
        normalized = normalize_question(question)
        if not normalized:
            return None
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT * FROM answer_memory WHERE user_approved = 1"
            ).fetchall()
            best = None
            best_score = 0.0
            for row in rows:
                if row["normalized_question"] == normalized:
                    best, best_score = row, 1.0
                    break
                if canonical_intent and row["canonical_intent"] == canonical_intent:
                    score = max(0.9, self._similarity(normalized, row["normalized_question"]))
                else:
                    score = self._similarity(normalized, row["normalized_question"])
                if score > best_score:
                    best, best_score = row, score
            if best is None or best_score < similarity_threshold:
                return None
            now = datetime.now().astimezone().isoformat(timespec="seconds")
            conn.execute(
                "UPDATE answer_memory SET last_used = ?, use_count = use_count + 1 WHERE id = ?",
                (now, best["id"]),
            )
            return AnswerMemoryRecord(
                original_question=best["original_question"],
                normalized_question=best["normalized_question"],
                canonical_intent=best["canonical_intent"] or "",
                answer=best["answer"],
                answer_source=best["answer_source"],
                confidence=min(float(best["confidence"]), best_score),
                user_approved=True,
                last_used=now,
            )

    def list_records(self) -> List[Dict[str, Any]]:
        with self.connection() as conn:
            return [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM answer_memory ORDER BY last_used DESC, id DESC"
                ).fetchall()
            ]

    def migrate_legacy_json(self, path: Path) -> int:
        """Import legacy entries safely; shipped sample rules remain unapproved."""
        if not path.exists():
            return 0
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return 0
        shipped = {
            "work authorization": "Yes",
            "require sponsorship": "No",
            "relocate": "Yes",
            "years of experience": "3",
            "salary expectation": "100000",
        }
        imported = 0
        rules = dict(data.get("question_rules", {}))
        rules.update(data.get("custom_field_rules", {}))
        for question, answer in rules.items():
            normalized = normalize_question(question)
            with self.connection() as conn:
                if conn.execute(
                    "SELECT 1 FROM answer_memory WHERE normalized_question = ?",
                    (normalized,),
                ).fetchone():
                    continue
            approved = (
                question in data.get("custom_field_rules", {})
                or shipped.get(normalized) != str(answer)
            )
            self.save(
                question,
                str(answer),
                answer_source="legacy-memory",
                confidence=1.0 if approved else 0.4,
                user_approved=approved,
            )
            imported += 1
        return imported

    @staticmethod
    def _similarity(left: str, right: str) -> float:
        left_tokens, right_tokens = set(left.split()), set(right.split())
        union = left_tokens | right_tokens
        jaccard = len(left_tokens & right_tokens) / len(union) if union else 0.0
        sequence = SequenceMatcher(None, left, right).ratio()
        return max(jaccard, sequence)


answer_memory = AnswerMemoryRepository()
answer_memory.migrate_legacy_json(database_module.DATA_DIR / "learned_memory.json")
