from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import text

from app.db.connection import (
    ConnectionManager,
    ConnectionNotFoundError,
    InvalidConnectionStringError,
)


def test_sqlite_connection_registration_and_select_one(tmp_path: Path) -> None:
    sqlite_path = tmp_path / "business.sqlite"
    sqlite_path.touch()

    manager = ConnectionManager()
    connection_id = manager.register_connection(
        name="Acme Finance",
        connection_string=f"sqlite+pysqlite:///{sqlite_path.resolve().as_posix()}",
    )

    engine = manager.get_engine(connection_id)
    with engine.connect() as connection:
        assert connection.execute(text("SELECT 1")).scalar_one() == 1

    connections = manager.list_connections()
    assert len(connections) == 1
    assert connections[0].connection_id == connection_id
    assert connections[0].name == "Acme Finance"
    assert connections[0].dialect == "sqlite"


def test_invalid_connection_string_raises_typed_exception() -> None:
    manager = ConnectionManager()

    with pytest.raises(InvalidConnectionStringError):
        manager.register_connection(
            name="Broken",
            connection_string="not-a-real-dialect://user:pass@localhost/db",
        )


def test_remove_connection_disposes_and_forgets_engine(tmp_path: Path) -> None:
    sqlite_path = tmp_path / "cleanup.sqlite"
    sqlite_path.touch()

    manager = ConnectionManager()
    connection_id = manager.register_connection(
        name="Cleanup",
        connection_string=f"sqlite+pysqlite:///{sqlite_path.resolve().as_posix()}",
        cleanup_path=sqlite_path,
    )

    manager.remove_connection(connection_id)

    assert sqlite_path.exists() is False
    with pytest.raises(ConnectionNotFoundError):
        manager.get_engine(connection_id)

