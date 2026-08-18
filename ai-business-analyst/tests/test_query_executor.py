from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Column, Integer, MetaData, String, Table, create_engine, event

from app.db.connection import ConnectionManager
from app.db.query_executor import (
    QueryExecutionError,
    QueryExecutor,
    QueryTimeoutError,
    UnsafeQueryError,
)
from app.llm.sql_generator import SQLGenerationResult
from app.main import create_app


def _create_temp_sqlite_database(tmp_path: Path) -> tuple[ConnectionManager, str]:
    sqlite_path = tmp_path / "query_executor.sqlite"
    engine = create_engine(f"sqlite+pysqlite:///{sqlite_path.resolve().as_posix()}")
    metadata = MetaData()

    items = Table(
        "items",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("name", String(50), nullable=False),
    )
    metadata.create_all(engine)

    with engine.begin() as connection:
        connection.execute(
            items.insert(),
            [
                {"id": 1, "name": "Alpha"},
                {"id": 2, "name": "Beta"},
                {"id": 3, "name": "Gamma"},
                {"id": 4, "name": "Delta"},
            ],
        )

    engine.dispose()

    manager = ConnectionManager()
    connection_id = manager.register_connection(
        name="Executor DB",
        connection_string=f"sqlite+pysqlite:///{sqlite_path.resolve().as_posix()}",
    )
    return manager, connection_id


def _register_app_sqlite_connection(app, tmp_path: Path) -> str:
    sqlite_path = tmp_path / "api_query.sqlite"
    engine = create_engine(f"sqlite+pysqlite:///{sqlite_path.resolve().as_posix()}")
    metadata = MetaData()

    items = Table(
        "items",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("name", String(50), nullable=False),
    )
    metadata.create_all(engine)

    with engine.begin() as connection:
        connection.execute(
            items.insert(),
            [
                {"id": 1, "name": "Alpha"},
                {"id": 2, "name": "Beta"},
            ],
        )

    engine.dispose()

    return app.state.connection_manager.register_connection(
        name="API DB",
        connection_string=f"sqlite+pysqlite:///{sqlite_path.resolve().as_posix()}",
    )


def test_query_executor_executes_valid_select_against_sqlite(tmp_path: Path) -> None:
    manager, connection_id = _create_temp_sqlite_database(tmp_path)
    executor = QueryExecutor(manager)

    result = executor.execute(connection_id, "SELECT id, name FROM items ORDER BY id")

    assert result.columns == ["id", "name"]
    assert result.rows == [
        {"id": 1, "name": "Alpha"},
        {"id": 2, "name": "Beta"},
        {"id": 3, "name": "Gamma"},
        {"id": 4, "name": "Delta"},
    ]
    assert result.row_count == 4
    assert result.truncated is False
    assert result.execution_time_ms >= 0


def test_query_executor_rejects_unsafe_query_without_side_effects(tmp_path: Path) -> None:
    manager, connection_id = _create_temp_sqlite_database(tmp_path)
    executor = QueryExecutor(manager)

    with pytest.raises(UnsafeQueryError):
        executor.execute(connection_id, "DELETE FROM items")

    with manager.get_engine(connection_id).connect() as connection:
        row_count = connection.exec_driver_sql("SELECT COUNT(*) FROM items").scalar_one()

    assert row_count == 4


def test_query_executor_enforces_row_limit(tmp_path: Path) -> None:
    manager, connection_id = _create_temp_sqlite_database(tmp_path)
    executor = QueryExecutor(manager, max_rows=3)

    result = executor.execute(connection_id, "SELECT id, name FROM items ORDER BY id")

    assert result.row_count == 3
    assert result.truncated is True
    assert result.rows == [
        {"id": 1, "name": "Alpha"},
        {"id": 2, "name": "Beta"},
        {"id": 3, "name": "Gamma"},
    ]


def test_query_executor_respects_existing_lower_limit(tmp_path: Path) -> None:
    manager, connection_id = _create_temp_sqlite_database(tmp_path)
    executor = QueryExecutor(manager, max_rows=3)

    result = executor.execute(
        connection_id,
        "SELECT id, name FROM items ORDER BY id LIMIT 2",
    )

    assert result.row_count == 2
    assert result.truncated is False
    assert result.rows == [
        {"id": 1, "name": "Alpha"},
        {"id": 2, "name": "Beta"},
    ]


def test_query_executor_times_out_on_slow_query(tmp_path: Path) -> None:
    manager, connection_id = _create_temp_sqlite_database(tmp_path)
    engine = manager.get_engine(connection_id)

    def register_sleep_function(dbapi_connection, _connection_record) -> None:
        dbapi_connection.create_function(
            "sleep",
            1,
            lambda seconds: time.sleep(float(seconds)) or 1,
        )

    event.listen(engine, "connect", register_sleep_function)
    engine.dispose()

    executor = QueryExecutor(manager, timeout_seconds=1, max_rows=3)

    with pytest.raises(QueryTimeoutError):
        executor.execute(
            connection_id,
            "SELECT sleep(0.2)",
            timeout_seconds=0.01,
        )


def test_query_executor_wraps_missing_column_errors(tmp_path: Path) -> None:
    manager, connection_id = _create_temp_sqlite_database(tmp_path)
    executor = QueryExecutor(manager)

    with pytest.raises(QueryExecutionError) as exc_info:
        executor.execute(connection_id, "SELECT missing_col FROM items")

    message = str(exc_info.value).lower()
    assert "missing_col" in message or "no such column" in message


def test_execute_route_returns_query_result(tmp_path: Path) -> None:
    app = create_app()
    connection_id = _register_app_sqlite_connection(app, tmp_path)

    with TestClient(app) as client:
        response = client.post(
            f"/connections/{connection_id}/query/execute",
            json={"sql": "SELECT id, name FROM items ORDER BY id"},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["columns"] == ["id", "name"]
    assert payload["row_count"] == 2
    assert payload["truncated"] is False
    assert payload["rows"] == [
        {"id": 1, "name": "Alpha"},
        {"id": 2, "name": "Beta"},
    ]


def test_ask_route_returns_generated_sql_and_results(tmp_path: Path) -> None:
    app = create_app()
    connection_id = _register_app_sqlite_connection(app, tmp_path)

    class FakeGenerator:
        def generate_sql(self, question, schema_info, dialect, few_shot_examples=None):
            return SQLGenerationResult(
                sql="SELECT id, name FROM items ORDER BY id",
                raw_llm_output="SELECT id, name FROM items ORDER BY id",
                model_used="fake-model",
                prompt_used="fake-prompt",
            )

    app.state.sql_generator = FakeGenerator()

    with TestClient(app) as client:
        response = client.post(
            f"/connections/{connection_id}/query/ask",
            json={"question": "List items", "interpret": False},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["generated_sql"] == "SELECT id, name FROM items ORDER BY id"
    assert payload["query_result"]["row_count"] == 2
    assert payload["query_result"]["rows"] == [
        {"id": 1, "name": "Alpha"},
        {"id": 2, "name": "Beta"},
    ]
    assert payload["interpretation"] is None
    assert payload["warning"] is None


def test_ask_route_rejects_unsafe_generated_sql(tmp_path: Path) -> None:
    app = create_app()
    connection_id = _register_app_sqlite_connection(app, tmp_path)

    class UnsafeGenerator:
        def generate_sql(self, question, schema_info, dialect, few_shot_examples=None):
            return SQLGenerationResult(
                sql="DELETE FROM items",
                raw_llm_output="DELETE FROM items",
                model_used="fake-model",
                prompt_used="fake-prompt",
            )

    app.state.sql_generator = UnsafeGenerator()

    with TestClient(app) as client:
        response = client.post(
            f"/connections/{connection_id}/query/ask",
            json={"question": "Delete items", "interpret": False},
        )

    assert response.status_code == 422
    assert "Generated SQL was rejected" in response.json()["detail"]
