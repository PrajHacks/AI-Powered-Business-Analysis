from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Column, Integer, MetaData, String, Table, create_engine

from app.db.introspection import ColumnInfo, SchemaInfo, TableInfo
from app.llm.ollama_client import OllamaClient, OllamaUnreachableError
from app.llm.sql_generator import SQLGenerationParseError, SQLGenerationResult, SQLGenerator
from app.main import create_app


def _make_schema_info() -> SchemaInfo:
    return SchemaInfo(
        connection_id="conn_1",
        generated_at=datetime.now(timezone.utc),
        tables=[
            TableInfo(
                name="customers",
                kind="table",
                columns=[
                    ColumnInfo(name="id", type="integer", nullable=False, primary_key=True),
                    ColumnInfo(name="name", type="text", nullable=False, primary_key=False),
                ],
                primary_key_columns=["id"],
                foreign_keys=[],
                row_count=2,
                row_count_is_estimate=False,
            )
        ],
    )


def _make_date_schema_info() -> SchemaInfo:
    return SchemaInfo(
        connection_id="conn_2",
        generated_at=datetime.now(timezone.utc),
        tables=[
            TableInfo(
                name="orders",
                kind="table",
                columns=[
                    ColumnInfo(name="id", type="integer", nullable=False, primary_key=True),
                    ColumnInfo(name="order_date", type="text", nullable=False, primary_key=False),
                ],
                primary_key_columns=["id"],
                foreign_keys=[],
                row_count=3,
                row_count_is_estimate=False,
            )
        ],
    )


def _make_sqlite_connection(app, tmp_path: Path) -> str:
    sqlite_path = tmp_path / "query_route.sqlite"
    engine = create_engine(f"sqlite+pysqlite:///{sqlite_path.resolve().as_posix()}")
    metadata = MetaData()

    customers = Table(
        "customers",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("name", String(50), nullable=False),
    )
    metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(customers.insert(), [{"id": 1, "name": "Alice"}])
    engine.dispose()

    return app.state.connection_manager.register_connection(
        name="Route DB",
        connection_string=f"sqlite+pysqlite:///{sqlite_path.resolve().as_posix()}",
    )


def test_generate_sql_parses_clean_response() -> None:
    schema_info = _make_schema_info()
    client = OllamaClient(base_url="http://localhost:11434", timeout_seconds=60)

    def fake_post(url, json=None, **kwargs):
        return httpx.Response(
            200,
            json={"response": "SELECT id, name FROM customers LIMIT 10;"},
        )

    client._client.post = fake_post  # type: ignore[method-assign]
    generator = SQLGenerator(client, model="llama3.2:3b")

    result = generator.generate_sql("List customers", schema_info, dialect="sqlite")

    assert result.sql == "SELECT id, name FROM customers LIMIT 10;"
    assert result.raw_llm_output == "SELECT id, name FROM customers LIMIT 10;"
    assert result.model_used == "llama3.2:3b"
    assert "customers" in result.prompt_used


def test_generate_sql_strips_sql_fences() -> None:
    schema_info = _make_schema_info()
    client = OllamaClient(base_url="http://localhost:11434", timeout_seconds=60)

    def fake_post(url, json=None, **kwargs):
        return httpx.Response(
            200,
            json={"response": "```sql\nSELECT id FROM customers;\n```"},
        )

    client._client.post = fake_post  # type: ignore[method-assign]
    generator = SQLGenerator(client, model="llama3.2:3b")

    result = generator.generate_sql("List customer ids", schema_info, dialect="sqlite")

    assert result.sql == "SELECT id FROM customers;"
    assert "```" not in result.sql


def test_generate_sql_extracts_from_leading_prose() -> None:
    schema_info = _make_schema_info()
    client = OllamaClient(base_url="http://localhost:11434", timeout_seconds=60)

    def fake_post(url, json=None, **kwargs):
        return httpx.Response(
            200,
            json={"response": "Here is the query:\n\nSELECT id, name FROM customers WHERE id > 1;"},
        )

    client._client.post = fake_post  # type: ignore[method-assign]
    generator = SQLGenerator(client, model="llama3.2:3b")

    result = generator.generate_sql("List customers with id > 1", schema_info, dialect="sqlite")

    assert result.sql == "SELECT id, name FROM customers WHERE id > 1;"
    assert not result.sql.lower().startswith("here")


def test_unreachable_ollama_raises_specific_exception() -> None:
    schema_info = _make_schema_info()
    client = OllamaClient(base_url="http://localhost:11434", timeout_seconds=60)

    def raise_connect_error(url, json=None, **kwargs):
        raise httpx.ConnectError(
            "connection failed",
            request=httpx.Request("POST", f"{client.resolved_base_url}/api/generate"),
        )

    client._client.post = raise_connect_error  # type: ignore[method-assign]
    generator = SQLGenerator(client, model="llama3.2:3b")

    with pytest.raises(OllamaUnreachableError):
        generator.generate_sql("List customers", schema_info, dialect="sqlite")


def test_non_sql_response_raises_parse_error() -> None:
    schema_info = _make_schema_info()
    client = OllamaClient(base_url="http://localhost:11434", timeout_seconds=60)

    def fake_post(url, json=None, **kwargs):
        return httpx.Response(200, json={"response": "I cannot help with that."})

    client._client.post = fake_post  # type: ignore[method-assign]
    generator = SQLGenerator(client, model="llama3.2:3b")

    with pytest.raises(SQLGenerationParseError):
        generator.generate_sql("Refuse", schema_info, dialect="sqlite")


def test_prompt_builder_contains_schema_and_read_only_instruction() -> None:
    schema_info = _make_schema_info()
    client = OllamaClient(base_url="http://localhost:11434", timeout_seconds=60)
    generator = SQLGenerator(client, model="llama3.2:3b")

    prompt = generator.build_prompt(
        "How many customers are there?",
        schema_info,
        dialect="sqlite",
        few_shot_examples=[
            {"question": "List customer names", "sql": "SELECT name FROM customers;"},
        ],
    )

    lower_prompt = prompt.lower()
    assert "read-only" in lower_prompt
    assert "customers" in prompt
    assert "id" in prompt
    assert "name" in prompt
    assert "list customer names" in lower_prompt
    assert "select name from customers;" in lower_prompt


def test_prompt_builder_contains_group_by_rule() -> None:
    schema_info = _make_schema_info()
    client = OllamaClient(base_url="http://localhost:11434", timeout_seconds=60)
    generator = SQLGenerator(client, model="llama3.2:3b")

    prompt = generator.build_prompt(
        "Show total profit by sales channel",
        schema_info,
        dialect="sqlite",
    )

    assert (
        "If the query uses GROUP BY, every selected column that is not in the GROUP BY clause MUST be wrapped in an aggregate function"
        in prompt
    )
    assert "Never select a raw non-aggregated column alongside GROUP BY." in prompt
    assert (
        "If the query uses GROUP BY, the SELECT list MUST include every column that appears in the GROUP BY clause"
        in prompt
    )
    assert "Never group by a column without also selecting it." in prompt


def test_prompt_builder_contains_breakdown_by_rule_and_case_insensitive_guidance() -> None:
    schema_info = _make_schema_info()
    client = OllamaClient(base_url="http://localhost:11434", timeout_seconds=60)
    generator = SQLGenerator(client, model="llama3.2:3b")

    prompt_sqlite = generator.build_prompt(
        "Show total profit by sales channel",
        schema_info,
        dialect="sqlite",
    )
    prompt_pg = generator.build_prompt(
        "Show total profit by sales channel",
        schema_info,
        dialect="postgresql",
    )
    prompt_mysql = generator.build_prompt(
        "Show total profit by sales channel",
        schema_info,
        dialect="mysql",
    )

    for prompt in (prompt_sqlite, prompt_pg, prompt_mysql):
        assert "MUST use GROUP BY" in prompt
        assert "Do NOT filter to a single guessed value with WHERE instead of grouping" in prompt
        assert "case-insensitive comparison" in prompt.lower()

    assert "COLLATE NOCASE" in prompt_sqlite
    assert "ILIKE" in prompt_pg
    assert "LOWER(col) = LOWER('value')" in prompt_mysql


def test_sqlite_prompt_includes_sqlite_date_guidance_without_extract() -> None:
    schema_info = _make_date_schema_info()
    client = OllamaClient(base_url="http://localhost:11434", timeout_seconds=60)

    def fake_post(url, json=None, **kwargs):
        return httpx.Response(200, json={"response": "SELECT order_date FROM orders LIMIT 5;"})

    client._client.post = fake_post  # type: ignore[method-assign]
    generator = SQLGenerator(client, model="llama3.2:3b")

    result = generator.generate_sql(
        "Show orders by month",
        schema_info,
        dialect="sqlite",
    )

    prompt = result.prompt_used.lower()
    assert "target sql dialect: sqlite" in prompt
    assert "strftime(" in prompt
    assert "strftime('%m', col)" in prompt
    assert "extract(" not in prompt


def test_postgresql_prompt_mentions_extract_and_date_trunc() -> None:
    schema_info = _make_date_schema_info()
    client = OllamaClient(base_url="http://localhost:11434", timeout_seconds=60)

    def fake_post(url, json=None, **kwargs):
        return httpx.Response(200, json={"response": "SELECT order_date FROM orders LIMIT 5;"})

    client._client.post = fake_post  # type: ignore[method-assign]
    generator = SQLGenerator(client, model="llama3.2:3b")

    result = generator.generate_sql(
        "Show orders by month",
        schema_info,
        dialect="postgresql",
    )

    prompt = result.prompt_used.lower()
    assert "target sql dialect: postgresql" in prompt
    assert "extract(part from col)" in prompt
    assert "date_trunc()" in prompt


def test_query_route_threads_engine_dialect_into_generator(tmp_path: Path) -> None:
    app = create_app()
    connection_id = _make_sqlite_connection(app, tmp_path)
    captured: dict[str, str] = {}

    class FakeGenerator:
        def generate_sql(self, question, schema_info, dialect, few_shot_examples=None):
            captured["dialect"] = dialect
            captured["question"] = question
            return SQLGenerationResult(
                sql="SELECT 1;",
                raw_llm_output="SELECT 1;",
                model_used="fake-model",
                prompt_used="fake-prompt",
            )

    class FakeIntrospector:
        def get_schema(self, connection_id):
            return _make_date_schema_info()

    app.state.sql_generator = FakeGenerator()
    app.state.schema_introspector = FakeIntrospector()

    with TestClient(app) as client:
        response = client.post(
            f"/connections/{connection_id}/query/generate-sql",
            json={"question": "Show orders by month"},
        )

    assert response.status_code == 200
    assert captured["dialect"] == "sqlite"
    assert captured["question"] == "Show orders by month"


def test_query_route_returns_503_for_unreachable_ollama(tmp_path: Path) -> None:
    app = create_app()
    connection_id = _make_sqlite_connection(app, tmp_path)

    def raise_connect_error(url, json=None, **kwargs):
        raise httpx.ConnectError(
            "connection failed",
            request=httpx.Request("POST", f"{app.state.ollama_client.resolved_base_url}/api/generate"),
        )

    app.state.ollama_client._client.post = raise_connect_error  # type: ignore[method-assign]

    with TestClient(app) as client:
        response = client.post(
            f"/connections/{connection_id}/query/generate-sql",
            json={"question": "List all customers"},
        )

    assert response.status_code == 503
    assert response.json()["detail"] == (
        f"Ollama is not reachable at {app.state.ollama_client.resolved_base_url}. Is it running?"
    )


def test_generate_valid_sql_retries_and_succeeds_on_second_attempt() -> None:
    schema_info = _make_schema_info()
    client = OllamaClient(base_url="http://localhost:11434", timeout_seconds=60)
    responses = [
        "SELECT id, name, SUM(id) FROM customers GROUP BY name;",
        "SELECT name, SUM(id) FROM customers GROUP BY name;",
    ]

    def fake_post(url, json=None, **kwargs):
        return httpx.Response(200, json={"response": responses.pop(0)})

    client._client.post = fake_post  # type: ignore[method-assign]
    generator = SQLGenerator(client, model="llama3.2:3b")

    result = generator.generate_valid_sql(
        "Show sum by name",
        schema_info,
        dialect="sqlite",
        max_attempts=2,
    )

    assert result.validation_passed is True
    assert result.attempts == 2
    assert "SELECT name, SUM(id) FROM customers GROUP BY name" in result.sql
    assert result.rejection_reason is None


def test_generate_valid_sql_stops_after_max_attempts_when_all_fail() -> None:
    schema_info = _make_schema_info()
    client = OllamaClient(base_url="http://localhost:11434", timeout_seconds=60)
    calls = 0

    def fake_post(url, json=None, **kwargs):
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            json={"response": "SELECT id, name, SUM(id) FROM customers GROUP BY name;"},
        )

    client._client.post = fake_post  # type: ignore[method-assign]
    generator = SQLGenerator(client, model="llama3.2:3b")

    result = generator.generate_valid_sql(
        "Show sum by name",
        schema_info,
        dialect="sqlite",
        max_attempts=2,
    )

    assert result.validation_passed is False
    assert result.attempts == 2
    assert calls == 2
    assert result.rejection_reason is not None
    assert "non-aggregated column 'id'" in result.rejection_reason


def test_retry_prompt_contains_rejected_sql_and_specific_rejection_reason() -> None:
    schema_info = _make_schema_info()
    client = OllamaClient(base_url="http://localhost:11434", timeout_seconds=60)
    captured_prompts: list[str] = []
    responses = [
        "SELECT id, name, SUM(id) FROM customers GROUP BY name;",
        "SELECT name, SUM(id) FROM customers GROUP BY name;",
    ]

    def fake_post(url, json=None, **kwargs):
        if json and "prompt" in json:
            captured_prompts.append(json["prompt"])
        return httpx.Response(200, json={"response": responses.pop(0)})

    client._client.post = fake_post  # type: ignore[method-assign]
    generator = SQLGenerator(client, model="llama3.2:3b")

    generator.generate_valid_sql(
        "Show sum of ids grouped by name",
        schema_info,
        dialect="sqlite",
        max_attempts=2,
    )

    assert len(captured_prompts) == 2
    retry_prompt = captured_prompts[1]
    assert "Show sum of ids grouped by name" in retry_prompt
    assert "SELECT id, name, SUM(id) FROM customers GROUP BY name;" in retry_prompt
    assert "non-aggregated column 'id'" in retry_prompt
    assert "Fix ONLY the identified problem above and return the corrected SQL query." in retry_prompt


def test_ask_route_returns_422_with_rejection_reason_when_all_retries_fail(tmp_path: Path) -> None:
    app = create_app()
    connection_id = _make_sqlite_connection(app, tmp_path)

    def fake_post(url, json=None, **kwargs):
        return httpx.Response(
            200,
            json={"response": "SELECT id, name, COUNT(*) FROM customers GROUP BY name;"},
        )

    app.state.ollama_client._client.post = fake_post  # type: ignore[method-assign]

    with TestClient(app) as client:
        response = client.post(
            f"/connections/{connection_id}/query/ask",
            json={"question": "Count per name", "interpret": False},
        )

    assert response.status_code == 422
    assert "Generated SQL was rejected: Query selects non-aggregated column 'id'" in response.json()["detail"]


def test_ask_route_succeeds_after_self_correction_retry(tmp_path: Path) -> None:
    app = create_app()
    connection_id = _make_sqlite_connection(app, tmp_path)
    responses = [
        "SELECT id, name, COUNT(*) FROM customers GROUP BY name;",
        "SELECT name, COUNT(*) FROM customers GROUP BY name;",
    ]

    def fake_post(url, json=None, **kwargs):
        return httpx.Response(200, json={"response": responses.pop(0)})

    app.state.ollama_client._client.post = fake_post  # type: ignore[method-assign]

    with TestClient(app) as client:
        response = client.post(
            f"/connections/{connection_id}/query/ask",
            json={"question": "Count per name", "interpret": False},
        )

    assert response.status_code == 200
    payload = response.json()
    assert "SELECT name, COUNT(*) FROM customers GROUP BY name" in payload["generated_sql"]
    assert payload["query_result"]["row_count"] == 1
    assert payload["query_result"]["rows"] == [{"name": "Alice", "COUNT(*)": 1}]


def test_prompt_builder_contains_single_dimension_group_by_rule() -> None:
    schema_info = SchemaInfo(
        connection_id="conn-test",
        generated_at=datetime.now(timezone.utc),
        tables=[
            TableInfo(
                name="sales",
                kind="table",
                columns=[
                    ColumnInfo(name="id", type="integer", nullable=False, primary_key=True),
                    ColumnInfo(name="region", type="text", nullable=False),
                    ColumnInfo(name="country", type="text", nullable=False),
                    ColumnInfo(name="item_type", type="text", nullable=False),
                    ColumnInfo(name="total_cost", type="float", nullable=False),
                ],
                primary_key_columns=["id"],
            )
        ],
    )
    client = OllamaClient(base_url="http://localhost:11434", timeout_seconds=60)
    generator = SQLGenerator(client, model="llama3.2:3b")

    prompt = generator.build_prompt(
        "Break down total expenses by product category",
        schema_info,
        dialect="sqlite",
    )

    assert "Group ONLY by that column/concept" in prompt
    assert "do not add additional GROUP BY columns that were not requested" in prompt
    assert "unless the question explicitly asks for a breakdown by multiple dimensions" in prompt


