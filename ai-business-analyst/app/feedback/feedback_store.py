from __future__ import annotations

import threading
from collections import defaultdict
from datetime import datetime, timezone
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from app.config import get_settings


class FeedbackEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    feedback_id: str = Field(default_factory=lambda: str(uuid4()))
    connection_id: str
    question: str
    generated_sql: str
    rating: Literal["up", "down"]
    comment: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class FeedbackStore:
    """Connection-scoped store for user feedback on generated SQL queries.

    Stores thumbs up/down ratings and comments. Confirmed (thumbs-up)
    question->SQL pairs can be retrieved and reused as few-shot examples in
    future SQL generation prompts for the same connection.

    Storage Note:
        In-memory storage (dict keyed by connection_id -> list[FeedbackEntry])
        is used for development/testing, consistent with the rest of the project.
        A production deployment would require persistent storage (e.g. PostgreSQL,
        Redis) to retain feedback across server restarts.

    Schema Drift Known Limitation:
        Before storing a thumbs-up SQL entry, we do not re-validate it against the
        schema because it already executed successfully in /ask. However, if the
        underlying database schema evolves later (e.g. columns renamed or tables
        dropped), previously recorded positive examples may become stale or invalid.
        A future enhancement could periodically validate or prune few-shot examples
        against current schema introspection.
    """

    def __init__(self, few_shot_limit: int | None = None) -> None:
        self._few_shot_limit = few_shot_limit
        self._store: dict[str, list[FeedbackEntry]] = defaultdict(list)
        self._lock = threading.Lock()

    def record_feedback(
        self,
        connection_id: str,
        question: str,
        sql: str,
        rating: Literal["up", "down"],
        comment: str | None = None,
    ) -> FeedbackEntry:
        """Record user feedback for a question and generated SQL query."""
        entry = FeedbackEntry(
            connection_id=connection_id,
            question=question.strip(),
            generated_sql=sql.strip(),
            rating=rating,
            comment=comment.strip() if comment else None,
        )
        with self._lock:
            self._store[connection_id].append(entry)
        return entry

    def get_positive_examples(
        self,
        connection_id: str,
        limit: int | None = None,
    ) -> list[FeedbackEntry]:
        """Return the most recent thumbs-up feedback entries for a connection.

        Returned entries are ordered most recent first, capped at `limit` (or the
        configured feedback_few_shot_limit) to keep prompt sizes bounded.
        """
        max_limit = (
            limit
            if limit is not None
            else (self._few_shot_limit or get_settings().feedback_few_shot_limit)
        )
        with self._lock:
            entries = self._store.get(connection_id, [])
            # Filter thumbs-up entries and sort/order by most recent first
            positive_entries = [e for e in reversed(entries) if e.rating == "up"]
            return positive_entries[:max_limit]

    def get_all_feedback(self, connection_id: str) -> list[FeedbackEntry]:
        """Return all recorded feedback entries for a connection."""
        with self._lock:
            return list(self._store.get(connection_id, []))
