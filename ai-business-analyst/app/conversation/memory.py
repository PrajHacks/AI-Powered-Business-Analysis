from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.config import get_settings


class ConversationTurn(BaseModel):
    """One question/answer round-trip recorded in a conversation."""

    model_config = ConfigDict(extra="forbid")

    question: str
    generated_sql: str
    dialect: str
    # Compact result summary: column names + row_count + up to 3 sample rows.
    # We intentionally do NOT store the full QueryResult here to keep prompt
    # size bounded — only the shape and a tiny sample are included.
    query_result_summary: dict[str, Any]
    timestamp: datetime = Field(default_factory=lambda: datetime.now(tz=timezone.utc))


class _ConversationEntry:
    """Internal container that pairs a turn list with scoping metadata."""

    __slots__ = ("turns", "connection_id", "last_active")

    def __init__(self, connection_id: str) -> None:
        self.turns: list[ConversationTurn] = []
        self.connection_id: str = connection_id
        self.last_active: datetime = datetime.now(tz=timezone.utc)

    def touch(self) -> None:
        self.last_active = datetime.now(tz=timezone.utc)

    def is_expired(self, ttl_minutes: int) -> bool:
        age = datetime.now(tz=timezone.utc) - self.last_active
        return age > timedelta(minutes=ttl_minutes)


class ConversationMemory:
    """Thread-safe in-memory store of conversation turns, keyed by conversation_id.

    Design mirrors the TTL-based cache in SchemaIntrospector: expiry is checked
    lazily on every access rather than via a background sweep, keeping the
    implementation simple and test-friendly.

    Parameters
    ----------
    max_turns:
        Maximum turns stored per conversation (FIFO eviction beyond this cap).
        Defaults to Settings.conversation_max_turns.
    ttl_minutes:
        Inactivity TTL in minutes; an entire conversation is cleared when its
        last-active timestamp is older than this value.
        Defaults to Settings.conversation_ttl_minutes.
    """

    def __init__(
        self,
        *,
        max_turns: int | None = None,
        ttl_minutes: int | None = None,
    ) -> None:
        settings = get_settings()
        self._max_turns: int = max_turns if max_turns is not None else settings.conversation_max_turns
        self._ttl_minutes: int = ttl_minutes if ttl_minutes is not None else settings.conversation_ttl_minutes
        self._store: dict[str, _ConversationEntry] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def add_turn(
        self,
        conversation_id: str,
        turn: ConversationTurn,
        *,
        connection_id: str,
    ) -> None:
        """Append a turn to the conversation, creating it if it does not exist.

        Raises
        ------
        ValueError
            If the conversation already exists but was created against a
            *different* connection_id.  This prevents silently mixing schema
            contexts when a conversation_id is accidentally reused across
            databases.
        """
        with self._lock:
            entry = self._store.get(conversation_id)
            if entry is None:
                entry = _ConversationEntry(connection_id=connection_id)
                self._store[conversation_id] = entry
            elif entry.is_expired(self._ttl_minutes):
                # Expired: treat as new conversation, reset it in place.
                entry = _ConversationEntry(connection_id=connection_id)
                self._store[conversation_id] = entry
            elif entry.connection_id != connection_id:
                raise ValueError(
                    f"Conversation '{conversation_id}' was created against connection "
                    f"'{entry.connection_id}' but the current request uses "
                    f"'{connection_id}'. Pass a new conversation_id to start a fresh thread."
                )

            entry.turns.append(turn)
            # FIFO eviction: drop the oldest turn when we exceed the cap.
            while len(entry.turns) > self._max_turns:
                entry.turns.pop(0)
            entry.touch()

    def get_history(
        self,
        conversation_id: str,
        *,
        connection_id: str | None = None,
    ) -> list[ConversationTurn]:
        """Return stored turns for a conversation_id.

        If the conversation has expired or does not exist, returns an empty
        list.  If *connection_id* is provided and mismatches the stored entry,
        raises ValueError (same guard as add_turn).
        """
        with self._lock:
            entry = self._store.get(conversation_id)
            if entry is None:
                return []
            if entry.is_expired(self._ttl_minutes):
                del self._store[conversation_id]
                return []
            if connection_id is not None and entry.connection_id != connection_id:
                raise ValueError(
                    f"Conversation '{conversation_id}' belongs to connection "
                    f"'{entry.connection_id}', not '{connection_id}'."
                )
            entry.touch()
            return list(entry.turns)

    def get_connection_id(self, conversation_id: str) -> str | None:
        """Return the connection_id the conversation was created against, or None."""
        with self._lock:
            entry = self._store.get(conversation_id)
            if entry is None or entry.is_expired(self._ttl_minutes):
                return None
            return entry.connection_id

    def clear(self, conversation_id: str) -> None:
        """Remove all history for the given conversation_id."""
        with self._lock:
            self._store.pop(conversation_id, None)

    def exists(self, conversation_id: str) -> bool:
        """Return True iff a non-expired conversation exists for this id."""
        with self._lock:
            entry = self._store.get(conversation_id)
            if entry is None:
                return False
            if entry.is_expired(self._ttl_minutes):
                del self._store[conversation_id]
                return False
            return True
