from __future__ import annotations

"""Tests for ConversationMemory, SQLGenerator history prompt, and /ask route integration."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Column, Integer, MetaData, String, Table, create_engine

from app.conversation.memory import ConversationMemory, ConversationTurn, _ConversationEntry
from app.llm.sql_generator import SQLGenerator, SQLGenerationResult
from app.main import create_app


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _turn(question: str = "What is the total?", sql: str = "SELECT SUM(a) FROM t") -> ConversationTurn:
    return ConversationTurn(
        question=question,
        generated_sql=sql,
        dialect="sqlite",
        query_result_summary={"columns": ["sum_a"], "row_count": 1, "sample_rows": [{"sum_a": 42}]},
    )


def _register_sqlite_connection(app, tmp_path: Path) -> str:
    sqlite_path = tmp_path / "conv_test.sqlite"
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
            [{"id": 1, "region": "North", "revenue": 100}, {"id": 2, "region": "South", "revenue": 200}],
        )
    engine.dispose()
    return app.state.connection_manager.register_connection(
        name="Conv DB",
        connection_string=f"sqlite+pysqlite:///{sqlite_path.resolve().as_posix()}",
    )


# ---------------------------------------------------------------------------
# ConversationMemory unit tests
# ---------------------------------------------------------------------------


class TestConversationMemoryRoundTrip:
    def test_add_then_get_returns_the_same_turn(self) -> None:
        mem = ConversationMemory(max_turns=10, ttl_minutes=30)
        t = _turn("How many orders?", "SELECT COUNT(*) FROM orders")
        mem.add_turn("conv-1", t, connection_id="db-1")
        history = mem.get_history("conv-1", connection_id="db-1")
        assert len(history) == 1
        assert history[0].question == "How many orders?"
        assert history[0].generated_sql == "SELECT COUNT(*) FROM orders"

    def test_get_nonexistent_returns_empty_list(self) -> None:
        mem = ConversationMemory(max_turns=10, ttl_minutes=30)
        assert mem.get_history("no-such-id") == []

    def test_multiple_turns_returned_in_insertion_order(self) -> None:
        mem = ConversationMemory(max_turns=10, ttl_minutes=30)
        for i in range(5):
            mem.add_turn("conv-1", _turn(f"Q{i}", f"SELECT {i}"), connection_id="db-1")
        history = mem.get_history("conv-1")
        assert [t.question for t in history] == ["Q0", "Q1", "Q2", "Q3", "Q4"]


class TestFIFOEviction:
    def test_oldest_dropped_when_cap_exceeded(self) -> None:
        mem = ConversationMemory(max_turns=3, ttl_minutes=30)
        for i in range(5):
            mem.add_turn("conv-1", _turn(f"Q{i}", f"SELECT {i}"), connection_id="db-1")
        history = mem.get_history("conv-1")
        # Only the 3 most-recent should remain
        assert len(history) == 3
        assert [t.question for t in history] == ["Q2", "Q3", "Q4"]

    def test_newest_retained_after_eviction(self) -> None:
        mem = ConversationMemory(max_turns=2, ttl_minutes=30)
        for i in range(10):
            mem.add_turn("conv-1", _turn(f"Q{i}", f"SQL{i}"), connection_id="db-1")
        history = mem.get_history("conv-1")
        assert len(history) == 2
        assert history[-1].question == "Q9"
        assert history[0].question == "Q8"

    def test_eviction_is_fifo_not_random(self) -> None:
        mem = ConversationMemory(max_turns=3, ttl_minutes=30)
        mem.add_turn("conv-1", _turn("First"), connection_id="db-1")
        mem.add_turn("conv-1", _turn("Second"), connection_id="db-1")
        mem.add_turn("conv-1", _turn("Third"), connection_id="db-1")
        mem.add_turn("conv-1", _turn("Fourth"), connection_id="db-1")  # First evicted
        history = mem.get_history("conv-1")
        questions = [t.question for t in history]
        assert "First" not in questions
        assert questions == ["Second", "Third", "Fourth"]


class TestTTLExpiry:
    def test_expired_conversation_returns_empty_and_is_removed(self) -> None:
        mem = ConversationMemory(max_turns=10, ttl_minutes=30)
        mem.add_turn("conv-1", _turn(), connection_id="db-1")

        # Manually back-date the last_active timestamp to simulate expiry.
        with mem._lock:
            mem._store["conv-1"].last_active = datetime.now(tz=timezone.utc) - timedelta(minutes=31)

        history = mem.get_history("conv-1")
        assert history == []
        # The entry should have been removed from the store.
        with mem._lock:
            assert "conv-1" not in mem._store

    def test_non_expired_conversation_is_not_removed(self) -> None:
        mem = ConversationMemory(max_turns=10, ttl_minutes=30)
        mem.add_turn("conv-1", _turn(), connection_id="db-1")

        with mem._lock:
            mem._store["conv-1"].last_active = datetime.now(tz=timezone.utc) - timedelta(minutes=29)

        history = mem.get_history("conv-1")
        assert len(history) == 1

    def test_add_turn_to_expired_conversation_starts_fresh(self) -> None:
        mem = ConversationMemory(max_turns=10, ttl_minutes=30)
        mem.add_turn("conv-1", _turn("OldQ"), connection_id="db-1")

        with mem._lock:
            mem._store["conv-1"].last_active = datetime.now(tz=timezone.utc) - timedelta(minutes=60)

        # Adding a new turn to an expired conversation should reset it.
        mem.add_turn("conv-1", _turn("NewQ"), connection_id="db-1")
        history = mem.get_history("conv-1")
        assert len(history) == 1
        assert history[0].question == "NewQ"


class TestClear:
    def test_clear_removes_only_the_specified_conversation(self) -> None:
        mem = ConversationMemory(max_turns=10, ttl_minutes=30)
        mem.add_turn("conv-A", _turn("A-question"), connection_id="db-1")
        mem.add_turn("conv-B", _turn("B-question"), connection_id="db-1")

        mem.clear("conv-A")

        assert mem.get_history("conv-A") == []
        assert len(mem.get_history("conv-B")) == 1
        assert mem.get_history("conv-B")[0].question == "B-question"

    def test_clear_nonexistent_is_noop(self) -> None:
        mem = ConversationMemory(max_turns=10, ttl_minutes=30)
        mem.clear("never-existed")  # Must not raise

    def test_exists_returns_false_after_clear(self) -> None:
        mem = ConversationMemory(max_turns=10, ttl_minutes=30)
        mem.add_turn("conv-1", _turn(), connection_id="db-1")
        mem.clear("conv-1")
        assert not mem.exists("conv-1")


class TestConnectionIdScoping:
    def test_mismatched_connection_id_raises_on_add(self) -> None:
        mem = ConversationMemory(max_turns=10, ttl_minutes=30)
        mem.add_turn("conv-1", _turn(), connection_id="db-1")
        with pytest.raises(ValueError, match="db-1"):
            mem.add_turn("conv-1", _turn(), connection_id="db-2")

    def test_mismatched_connection_id_raises_on_get(self) -> None:
        mem = ConversationMemory(max_turns=10, ttl_minutes=30)
        mem.add_turn("conv-1", _turn(), connection_id="db-1")
        with pytest.raises(ValueError, match="db-1"):
            mem.get_history("conv-1", connection_id="db-2")

    def test_get_connection_id_returns_stored_value(self) -> None:
        mem = ConversationMemory(max_turns=10, ttl_minutes=30)
        mem.add_turn("conv-1", _turn(), connection_id="my-db")
        assert mem.get_connection_id("conv-1") == "my-db"

    def test_get_connection_id_returns_none_for_unknown(self) -> None:
        mem = ConversationMemory(max_turns=10, ttl_minutes=30)
        assert mem.get_connection_id("unknown") is None


# ---------------------------------------------------------------------------
# SQLGenerator prompt tests
# ---------------------------------------------------------------------------


def _fake_schema():
    from unittest.mock import MagicMock
    schema = MagicMock()
    schema.to_llm_context.return_value = "table sales (id integer, region text, revenue integer)"
    return schema


class TestSQLGeneratorHistoryPrompt:
    def _make_generator(self) -> SQLGenerator:
        client = MagicMock()
        client.generate.return_value = "SELECT region, SUM(revenue) FROM sales GROUP BY region"
        return SQLGenerator(client, model="test-model")

    def test_prompt_contains_previous_question_and_sql(self) -> None:
        gen = self._make_generator()
        history = [
            _turn("Show me total revenue", "SELECT SUM(revenue) FROM sales"),
            _turn("Show me total profit", "SELECT SUM(profit) FROM sales"),
        ]
        prompt = gen.build_prompt(
            question="Break that down by region",
            schema_info=_fake_schema(),
            dialect="sqlite",
            conversation_history=history,
        )
        assert "Previous question: Show me total revenue" in prompt
        assert "Previous SQL: SELECT SUM(revenue) FROM sales" in prompt
        assert "Previous question: Show me total profit" in prompt
        assert "Break that down by region" in prompt

    def test_prompt_labels_distinguish_history_from_current(self) -> None:
        gen = self._make_generator()
        history = [_turn("What is the total?", "SELECT SUM(a) FROM t")]
        prompt = gen.build_prompt(
            question="Now break it down by region",
            schema_info=_fake_schema(),
            dialect="sqlite",
            conversation_history=history,
        )
        # History block must appear before 'Question:' in the prompt
        history_pos = prompt.index("Conversation history")
        question_pos = prompt.index("\nQuestion:\n")
        assert history_pos < question_pos, "History block must appear before the current question"

    def test_prompt_window_limits_turns_in_prompt(self) -> None:
        gen = self._make_generator()
        # Add 6 turns; window=3 → only the last 3 should appear in the prompt.
        history = [_turn(f"Q{i}", f"SQL{i}") for i in range(6)]
        prompt = gen.build_prompt(
            question="follow-up",
            schema_info=_fake_schema(),
            dialect="sqlite",
            conversation_history=history,
            prompt_window=3,
        )
        # Q0, Q1, Q2 must NOT appear; Q3, Q4, Q5 must appear.
        assert "Previous question: Q0" not in prompt
        assert "Previous question: Q1" not in prompt
        assert "Previous question: Q2" not in prompt
        assert "Previous question: Q3" in prompt
        assert "Previous question: Q4" in prompt
        assert "Previous question: Q5" in prompt

    def test_all_6_turns_retrievable_via_get_history(self) -> None:
        """The window only affects the prompt, not what's stored in memory."""
        mem = ConversationMemory(max_turns=10, ttl_minutes=30)
        for i in range(6):
            mem.add_turn("conv-1", _turn(f"Q{i}", f"SQL{i}"), connection_id="db-1")
        history = mem.get_history("conv-1")
        assert len(history) == 6

    def test_prompt_without_history_has_no_history_header(self) -> None:
        gen = self._make_generator()
        prompt = gen.build_prompt(
            question="What is total revenue?",
            schema_info=_fake_schema(),
            dialect="sqlite",
            conversation_history=None,
        )
        assert "Conversation history" not in prompt

    def test_result_columns_appear_in_history_block(self) -> None:
        gen = self._make_generator()
        turn = ConversationTurn(
            question="Show revenue",
            generated_sql="SELECT region, revenue FROM sales",
            dialect="sqlite",
            query_result_summary={"columns": ["region", "revenue"], "row_count": 5, "sample_rows": []},
        )
        prompt = gen.build_prompt(
            question="Filter that to North only",
            schema_info=_fake_schema(),
            dialect="sqlite",
            conversation_history=[turn],
        )
        assert "region" in prompt
        assert "revenue" in prompt
        assert "row_count: 5" in prompt


# ---------------------------------------------------------------------------
# Route-level integration tests
# ---------------------------------------------------------------------------


class FakeGenerator:
    """Captures the prompt passed by the route and returns a fixed SQL query."""

    def __init__(self, sql: str = "SELECT region, SUM(revenue) FROM sales GROUP BY region") -> None:
        self._sql = sql
        self.captured_history: list | None = None
        self.call_count = 0

    def generate_valid_sql(
        self,
        question,
        schema_info,
        validator=None,
        dialect="sqlite",
        few_shot_examples=None,
        max_attempts=None,
        conversation_history=None,
        prompt_window=None,
    ) -> SQLGenerationResult:
        self.captured_history = conversation_history
        self.call_count += 1
        return SQLGenerationResult(
            sql=self._sql,
            raw_llm_output=self._sql,
            model_used="fake",
            prompt_used=f"history={conversation_history}",
            validation_passed=True,
            rejection_reason=None,
            attempts=1,
        )


class TestAskRouteConversation:
    def test_new_ask_returns_a_conversation_id(self, tmp_path: Path) -> None:
        app = create_app()
        conn_id = _register_sqlite_connection(app, tmp_path)
        app.state.sql_generator = FakeGenerator("SELECT id, region, revenue FROM sales")

        with TestClient(app) as client:
            response = client.post(
                f"/connections/{conn_id}/query/ask",
                json={"question": "Show all sales", "interpret": False, "chart": False},
            )
        assert response.status_code == 200
        payload = response.json()
        assert "conversation_id" in payload
        assert isinstance(payload["conversation_id"], str)
        assert len(payload["conversation_id"]) > 0

    def test_second_call_with_same_conversation_id_receives_history(self, tmp_path: Path) -> None:
        app = create_app()
        conn_id = _register_sqlite_connection(app, tmp_path)
        gen = FakeGenerator("SELECT id, region, revenue FROM sales")
        app.state.sql_generator = gen

        with TestClient(app) as client:
            # First ask — establishes the conversation.
            r1 = client.post(
                f"/connections/{conn_id}/query/ask",
                json={"question": "Show total revenue", "interpret": False, "chart": False},
            )
            assert r1.status_code == 200
            conv_id = r1.json()["conversation_id"]

            # Second ask — passes the conversation_id back.
            r2 = client.post(
                f"/connections/{conn_id}/query/ask",
                json={
                    "question": "Break that down by region",
                    "interpret": False,
                    "chart": False,
                    "conversation_id": conv_id,
                },
            )
            assert r2.status_code == 200

        # The second call must have received non-empty history.
        assert gen.call_count == 2
        assert gen.captured_history is not None
        assert len(gen.captured_history) == 1
        assert gen.captured_history[0].question == "Show total revenue"

    def test_two_sequential_ask_calls_mock_ollama_inspect_prompt(self, tmp_path: Path) -> None:
        app = create_app()
        conn_id = _register_sqlite_connection(app, tmp_path)

        prompts_sent: list[str] = []

        def fake_generate(prompt, model, system=None, temperature=0.0):
            prompts_sent.append(prompt)
            return "SELECT id, region, revenue FROM sales"

        app.state.ollama_client.generate = fake_generate
        app.state.sql_generator.client = app.state.ollama_client

        with TestClient(app) as client:
            r1 = client.post(
                f"/connections/{conn_id}/query/ask",
                json={"question": "Show total revenue", "interpret": False, "chart": False},
            )
            assert r1.status_code == 200
            conv_id = r1.json()["conversation_id"]

            r2 = client.post(
                f"/connections/{conn_id}/query/ask",
                json={
                    "question": "Break that down by region",
                    "interpret": False,
                    "chart": False,
                    "conversation_id": conv_id,
                },
            )
            assert r2.status_code == 200

        assert len(prompts_sent) == 2
        first_prompt = prompts_sent[0]
        second_prompt = prompts_sent[1]

        # First prompt should not have conversation history
        assert "Conversation history" not in first_prompt
        assert "Show total revenue" in first_prompt

        # Second prompt must contain previous question and previous SQL
        assert "Conversation history (most recent last):" in second_prompt
        assert "Previous question: Show total revenue" in second_prompt
        assert "Previous SQL: SELECT id, region, revenue FROM sales" in second_prompt
        assert "Break that down by region" in second_prompt

    def test_conversation_id_from_different_connection_returns_400(self, tmp_path: Path) -> None:
        app = create_app()
        conn_id_A = _register_sqlite_connection(app, tmp_path)

        # Register a second (different) SQLite connection.
        sqlite_path_B = tmp_path / "other.sqlite"
        engine_b = create_engine(f"sqlite+pysqlite:///{sqlite_path_B.resolve().as_posix()}")
        MetaData().create_all(engine_b)
        engine_b.dispose()
        conn_id_B = app.state.connection_manager.register_connection(
            name="Other DB",
            connection_string=f"sqlite+pysqlite:///{sqlite_path_B.resolve().as_posix()}",
        )

        gen = FakeGenerator("SELECT id, region, revenue FROM sales")
        app.state.sql_generator = gen

        with TestClient(app) as client:
            # Ask against connection A → creates a conversation.
            r1 = client.post(
                f"/connections/{conn_id_A}/query/ask",
                json={"question": "Show revenue", "interpret": False, "chart": False},
            )
            assert r1.status_code == 200
            conv_id = r1.json()["conversation_id"]

            # Replay the same conversation_id but against connection B → must 400.
            r2 = client.post(
                f"/connections/{conn_id_B}/query/ask",
                json={
                    "question": "Follow up",
                    "interpret": False,
                    "chart": False,
                    "conversation_id": conv_id,
                },
            )
        assert r2.status_code == 400
        assert "conversation" in r2.json()["detail"].lower()

    def test_delete_conversation_returns_204(self, tmp_path: Path) -> None:
        app = create_app()
        conn_id = _register_sqlite_connection(app, tmp_path)
        gen = FakeGenerator("SELECT id, region, revenue FROM sales")
        app.state.sql_generator = gen

        with TestClient(app) as client:
            r1 = client.post(
                f"/connections/{conn_id}/query/ask",
                json={"question": "Show all", "interpret": False, "chart": False},
            )
            conv_id = r1.json()["conversation_id"]

            r_del = client.delete(f"/conversations/{conv_id}")
            assert r_del.status_code == 204

    def test_delete_nonexistent_conversation_returns_404(self, tmp_path: Path) -> None:
        app = create_app()
        with TestClient(app) as client:
            r = client.delete("/conversations/does-not-exist")
        assert r.status_code == 404

    def test_explicit_conversation_id_is_echoed_back(self, tmp_path: Path) -> None:
        app = create_app()
        conn_id = _register_sqlite_connection(app, tmp_path)
        app.state.sql_generator = FakeGenerator("SELECT id, region, revenue FROM sales")

        my_conv_id = "my-custom-conversation-abc"
        with TestClient(app) as client:
            r = client.post(
                f"/connections/{conn_id}/query/ask",
                json={
                    "question": "Show revenue",
                    "interpret": False,
                    "chart": False,
                    "conversation_id": my_conv_id,
                },
            )
        assert r.status_code == 200
        assert r.json()["conversation_id"] == my_conv_id
