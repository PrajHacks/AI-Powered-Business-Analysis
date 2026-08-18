from __future__ import annotations

"""Tests for FeedbackStore, few-shot prompt integration, and /connections/{connection_id}/feedback routes."""

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Column, Integer, MetaData, String, Table, create_engine

from app.conversation.memory import ConversationTurn
from app.db.introspection import ColumnInfo, SchemaInfo, TableInfo
from app.feedback.feedback_store import FeedbackEntry, FeedbackStore
from app.llm.ollama_client import OllamaClient
from app.llm.sql_generator import SQLGenerationResult, SQLGenerator
from app.main import create_app


def _make_fake_schema_info(connection_id: str = "conn-1") -> SchemaInfo:
    return SchemaInfo(
        connection_id=connection_id,
        generated_at=datetime.now(timezone.utc),
        tables=[
            TableInfo(
                name="sales",
                kind="table",
                columns=[
                    ColumnInfo(name="id", type="integer", nullable=False, primary_key=True),
                    ColumnInfo(name="region", type="text", nullable=False),
                    ColumnInfo(name="revenue", type="integer", nullable=False),
                ],
                primary_key_columns=["id"],
                foreign_keys=[],
                row_count=10,
            )
        ],
    )


def _register_sqlite_connection(app, tmp_path: Path) -> str:
    sqlite_path = tmp_path / "feedback_test.sqlite"
    engine = create_engine(f"sqlite+pysqlite:///{sqlite_path.resolve().as_posix()}")
    metadata = MetaData()
    Table(
        "sales",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("region", String(50)),
        Column("revenue", Integer),
    )
    metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(
            Table("sales", metadata).insert(),
            [
                {"id": 1, "region": "North", "revenue": 100},
                {"id": 2, "region": "South", "revenue": 200},
            ],
        )
    engine.dispose()
    return app.state.connection_manager.register_connection(
        name="Feedback DB",
        connection_string=f"sqlite+pysqlite:///{sqlite_path.resolve().as_posix()}",
    )


# ---------------------------------------------------------------------------
# Unit tests for FeedbackStore
# ---------------------------------------------------------------------------


class TestFeedbackStoreUnit:
    def test_record_feedback_and_get_positive_examples_round_trip(self) -> None:
        store = FeedbackStore(few_shot_limit=5)
        conn_id = "conn-test-1"

        # Record a mix of up and down ratings
        e1 = store.record_feedback(conn_id, "total revenue", "SELECT SUM(revenue) FROM sales", "up")
        e2 = store.record_feedback(conn_id, "bad query", "SELECT * FROM nowhere", "down", comment="wrong table")
        e3 = store.record_feedback(conn_id, "revenue by region", "SELECT region, SUM(revenue) FROM sales GROUP BY region", "up")

        positive = store.get_positive_examples(conn_id)
        assert len(positive) == 2
        # Most recent first: e3 followed by e1
        assert positive[0].question == "revenue by region"
        assert positive[0].generated_sql == "SELECT region, SUM(revenue) FROM sales GROUP BY region"
        assert positive[1].question == "total revenue"
        assert positive[1].generated_sql == "SELECT SUM(revenue) FROM sales"

        # Check all feedback returns all 3 entries
        all_entries = store.get_all_feedback(conn_id)
        assert len(all_entries) == 3

    def test_limit_cap_returns_most_recent_positive_examples(self) -> None:
        store = FeedbackStore(few_shot_limit=3)
        conn_id = "conn-test-limit"

        for i in range(1, 8):
            store.record_feedback(conn_id, f"question {i}", f"SELECT {i} FROM sales", "up")

        # Default limit from store (3)
        positive = store.get_positive_examples(conn_id)
        assert len(positive) == 3
        assert [p.question for p in positive] == ["question 7", "question 6", "question 5"]

        # Explicit custom limit
        positive_custom = store.get_positive_examples(conn_id, limit=2)
        assert len(positive_custom) == 2
        assert [p.question for p in positive_custom] == ["question 7", "question 6"]

    def test_feedback_is_connection_scoped(self) -> None:
        store = FeedbackStore()
        conn_a = "conn-a"
        conn_b = "conn-b"

        store.record_feedback(conn_a, "question A", "SELECT a FROM sales", "up")
        store.record_feedback(conn_b, "question B", "SELECT b FROM sales", "up")

        pos_a = store.get_positive_examples(conn_a)
        assert len(pos_a) == 1
        assert pos_a[0].question == "question A"

        pos_b = store.get_positive_examples(conn_b)
        assert len(pos_b) == 1
        assert pos_b[0].question == "question B"

        all_a = store.get_all_feedback(conn_a)
        assert len(all_a) == 1
        assert all_a[0].question == "question A"


# ---------------------------------------------------------------------------
# Prompt builder test for SQLGenerator with few-shot examples
# ---------------------------------------------------------------------------


class TestSQLGeneratorFewShotPrompt:
    def test_prompt_builder_formats_few_shot_examples_and_delineates_from_history(self) -> None:
        client = OllamaClient(base_url="http://localhost:11434", timeout_seconds=60)
        generator = SQLGenerator(client, model="llama3.2:3b")
        schema = _make_fake_schema_info()

        few_shot_examples = [
            {"question": "total sales", "sql": "SELECT SUM(revenue) FROM sales"},
            {"question": "sales by region", "sql": "SELECT region, SUM(revenue) FROM sales GROUP BY region"},
        ]

        history = [
            ConversationTurn(
                question="what tables exist?",
                generated_sql="SELECT name FROM sqlite_master",
                dialect="sqlite",
                query_result_summary={"columns": ["name"], "row_count": 1},
            )
        ]

        prompt = generator.build_prompt(
            question="show regional revenue breakdown",
            schema_info=schema,
            dialect="sqlite",
            few_shot_examples=few_shot_examples,
            conversation_history=history,
        )

        # Confirm few-shot examples block is present and properly formatted
        assert "Examples:" in prompt
        assert "Example 1:" in prompt
        assert "Question: total sales" in prompt
        assert "SQL: SELECT SUM(revenue) FROM sales" in prompt
        assert "Example 2:" in prompt
        assert "Question: sales by region" in prompt
        assert "SQL: SELECT region, SUM(revenue) FROM sales GROUP BY region" in prompt

        # Confirm conversation history is also present and separate
        assert "Conversation history (most recent last):" in prompt
        assert "[Turn 1]" in prompt
        assert "Previous question: what tables exist?" in prompt

        # Ensure order: Rules -> Examples -> Conversation history -> Schema -> Question -> SQL
        ex_idx = prompt.index("Examples:")
        hist_idx = prompt.index("Conversation history (most recent last):")
        schema_idx = prompt.index("Schema:")
        q_idx = prompt.index("Question:\nshow regional revenue breakdown")
        assert ex_idx < hist_idx < schema_idx < q_idx


# ---------------------------------------------------------------------------
# Route tests for /connections/{connection_id}/feedback
# ---------------------------------------------------------------------------


class TestFeedbackRoutes:
    def test_post_feedback_and_get_feedback(self, tmp_path: Path) -> None:
        app = create_app()
        client = TestClient(app)
        conn_id = _register_sqlite_connection(app, tmp_path)

        # GET empty feedback list
        resp = client.get(f"/connections/{conn_id}/feedback")
        assert resp.status_code == 200
        assert resp.json() == []

        # POST thumbs-up feedback
        payload_up = {
            "question": "total revenue",
            "generated_sql": "SELECT SUM(revenue) FROM sales",
            "rating": "up",
            "comment": "Accurate query",
        }
        post_resp = client.post(f"/connections/{conn_id}/feedback", json=payload_up)
        assert post_resp.status_code == 201
        data = post_resp.json()
        assert data["connection_id"] == conn_id
        assert data["question"] == "total revenue"
        assert data["generated_sql"] == "SELECT SUM(revenue) FROM sales"
        assert data["rating"] == "up"
        assert data["comment"] == "Accurate query"
        assert "feedback_id" in data

        # POST thumbs-down feedback
        payload_down = {
            "question": "show users",
            "generated_sql": "SELECT * FROM users",
            "rating": "down",
            "comment": "table does not exist",
        }
        client.post(f"/connections/{conn_id}/feedback", json=payload_down)

        # GET all feedback
        get_resp = client.get(f"/connections/{conn_id}/feedback")
        assert get_resp.status_code == 200
        all_feedback = get_resp.json()
        assert len(all_feedback) == 2
        ratings = [f["rating"] for f in all_feedback]
        assert "up" in ratings
        assert "down" in ratings

    def test_feedback_for_nonexistent_connection_returns_404(self) -> None:
        app = create_app()
        client = TestClient(app)

        resp_get = client.get("/connections/nonexistent-conn/feedback")
        assert resp_get.status_code == 404

        resp_post = client.post(
            "/connections/nonexistent-conn/feedback",
            json={"question": "q", "generated_sql": "SELECT 1", "rating": "up"},
        )
        assert resp_post.status_code == 404

    def test_ask_route_threads_positive_feedback_as_few_shot_examples(self, tmp_path: Path) -> None:
        app = create_app()
        client = TestClient(app)
        conn_id = _register_sqlite_connection(app, tmp_path)

        # Record a positive feedback entry
        client.post(
            f"/connections/{conn_id}/feedback",
            json={
                "question": "total revenue",
                "generated_sql": "SELECT SUM(revenue) FROM sales",
                "rating": "up",
            },
        )

        # Mock sql_generator to capture the kwargs passed to generate_valid_sql
        mock_generator = MagicMock()
        mock_generator.generate_valid_sql.return_value = SQLGenerationResult(
            sql="SELECT region, SUM(revenue) FROM sales GROUP BY region",
            raw_llm_output="SELECT region, SUM(revenue) FROM sales GROUP BY region",
            model_used="llama3.2:3b",
            prompt_used="fake prompt",
            validation_passed=True,
            rejection_reason=None,
            attempts=1,
        )
        app.state.sql_generator = mock_generator

        # Call /ask
        ask_resp = client.post(
            f"/connections/{conn_id}/query/ask",
            json={"question": "revenue by region", "interpret": False},
        )
        assert ask_resp.status_code == 200

        # Verify that generate_valid_sql was called with few_shot_examples containing the thumbs-up entry
        mock_generator.generate_valid_sql.assert_called_once()
        call_kwargs = mock_generator.generate_valid_sql.call_args.kwargs
        assert "few_shot_examples" in call_kwargs
        few_shots = call_kwargs["few_shot_examples"]
        assert few_shots is not None
        assert len(few_shots) == 1
        assert few_shots[0]["question"] == "total revenue"
        assert few_shots[0]["sql"] == "SELECT SUM(revenue) FROM sales"
