from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Iterable
from uuid import uuid4

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import NoSuchModuleError, SQLAlchemyError


class ConnectionManagerError(RuntimeError):
    """Base exception for connection manager failures."""


class ConnectionRegistrationError(ConnectionManagerError):
    """Raised when a connection cannot be created or validated."""


class InvalidConnectionStringError(ConnectionRegistrationError):
    """Raised when a connection string cannot be parsed or its dialect is unknown."""


class ConnectionValidationError(ConnectionRegistrationError):
    """Raised when a database connection fails validation."""


class ConnectionAlreadyExistsError(ConnectionRegistrationError):
    """Raised when a connection id is already registered."""


class ConnectionNotFoundError(ConnectionManagerError, KeyError):
    """Raised when a requested connection id does not exist."""


@dataclass(slots=True)
class ConnectionSummary:
    connection_id: str
    name: str
    dialect: str
    created_at: datetime


@dataclass(slots=True)
class _ManagedConnection:
    connection_id: str
    name: str
    dialect: str
    created_at: datetime
    engine: Engine
    cleanup_path: Path | None = None


def _sqlite_sidecar_paths(sqlite_path: Path) -> Iterable[Path]:
    yield sqlite_path
    yield sqlite_path.parent / f"{sqlite_path.name}-wal"
    yield sqlite_path.parent / f"{sqlite_path.name}-shm"
    yield sqlite_path.parent / f"{sqlite_path.name}-journal"


def cleanup_sqlite_artifacts(sqlite_path: Path | None) -> None:
    """Remove a SQLite database file and any common sidecar files."""

    if sqlite_path is None:
        return

    for candidate in _sqlite_sidecar_paths(Path(sqlite_path)):
        try:
            candidate.unlink()
        except FileNotFoundError:
            continue


class ConnectionManager:
    """In-memory registry for validated SQLAlchemy engines."""

    def __init__(self) -> None:
        self._connections: dict[str, _ManagedConnection] = {}
        self._lock = RLock()

    def register_connection(
        self,
        *,
        name: str,
        connection_string: str,
        connection_id: str | None = None,
        cleanup_path: Path | None = None,
    ) -> str:
        """Create and validate an engine, then register it in memory."""

        resolved_name = name.strip() or "connection"
        resolved_id = connection_id or f"conn_{uuid4().hex}"

        with self._lock:
            if resolved_id in self._connections:
                raise ConnectionAlreadyExistsError(
                    f"Connection id '{resolved_id}' is already registered."
                )

        try:
            engine = self._create_engine(connection_string)
        except NoSuchModuleError:
            raise InvalidConnectionStringError(
                "Unsupported or malformed database connection string."
            ) from None
        except SQLAlchemyError:
            raise InvalidConnectionStringError(
                "Unsupported or malformed database connection string."
            ) from None

        try:
            self._validate_engine(engine)
        except SQLAlchemyError as exc:
            engine.dispose()
            raise ConnectionValidationError(
                f"Unable to validate database connection '{resolved_name}'."
            ) from None
        except Exception:
            engine.dispose()
            raise

        record = _ManagedConnection(
            connection_id=resolved_id,
            name=resolved_name,
            dialect=engine.dialect.name,
            created_at=datetime.now(timezone.utc),
            engine=engine,
            cleanup_path=cleanup_path,
        )

        with self._lock:
            if resolved_id in self._connections:
                engine.dispose()
                raise ConnectionAlreadyExistsError(
                    f"Connection id '{resolved_id}' is already registered."
                )
            self._connections[resolved_id] = record

        return resolved_id

    def get_engine(self, connection_id: str) -> Engine:
        with self._lock:
            record = self._connections.get(connection_id)
            if record is None:
                raise ConnectionNotFoundError(
                    f"Connection id '{connection_id}' is not registered."
                )
            return record.engine

    def list_connections(self) -> list[ConnectionSummary]:
        with self._lock:
            records = sorted(
                self._connections.values(), key=lambda record: record.created_at
            )
            return [
                ConnectionSummary(
                    connection_id=record.connection_id,
                    name=record.name,
                    dialect=record.dialect,
                    created_at=record.created_at,
                )
                for record in records
            ]

    def remove_connection(self, connection_id: str) -> None:
        with self._lock:
            record = self._connections.pop(connection_id, None)

        if record is None:
            raise ConnectionNotFoundError(
                f"Connection id '{connection_id}' is not registered."
            )

        record.engine.dispose()
        cleanup_sqlite_artifacts(record.cleanup_path)

    def _create_engine(self, connection_string: str) -> Engine:
        engine_kwargs: dict[str, object] = {}
        if connection_string.startswith("sqlite"):
            engine_kwargs["connect_args"] = {"check_same_thread": False}
        return create_engine(connection_string, **engine_kwargs)

    @staticmethod
    def _validate_engine(engine: Engine) -> None:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1")).scalar_one()
