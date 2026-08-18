from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import Column, Integer, MetaData, String, Table, create_engine

from app.db.query_executor import QueryResult
from app.llm.ollama_client import OllamaUnreachableError
from app.llm.result_interpreter import ResultInterpreter
from app.llm.sql_generator import SQLGenerationResult
from app.main import create_app


def _make_query_result(
    rows: list[dict],
    *,
    columns: list[str],
    truncated: bool = False,
) -> QueryResult:
    return QueryResult(
        columns=columns,
        rows=rows,
        row_count=len(rows),
        truncated=truncated,
        execution_time_ms=12.5,
    )


def _register_sqlite_connection(app, tmp_path: Path) -> str:
    sqlite_path = tmp_path / "result_interpreter.sqlite"
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
        name="Result DB",
        connection_string=f"sqlite+pysqlite:///{sqlite_path.resolve().as_posix()}",
    )


def test_result_interpreter_prompt_includes_question_rows_and_aggregates() -> None:
    captured: dict[str, str] = {}

    class FakeClient:
        def generate(self, prompt, model, system=None, temperature=0.0):
            captured["prompt"] = prompt
            captured["model"] = model
            return "North America performed best, while Europe lagged behind."

    interpreter = ResultInterpreter(FakeClient(), model="fake-model")
    result = _make_query_result(
        [
            {"region": "North America", "revenue": 120, "orders": 4},
            {"region": "Europe", "revenue": 90, "orders": 2},
            {"region": "APAC", "revenue": 110, "orders": 3},
        ],
        columns=["region", "revenue", "orders"],
    )

    interpretation = interpreter.interpret(
        "Which region performed best?",
        "SELECT region, revenue, orders FROM sales ORDER BY revenue DESC",
        result,
    )

    prompt = captured["prompt"]
    assert captured["model"] == "fake-model"
    assert "Which region performed best?" in prompt
    assert "SELECT region, revenue, orders FROM sales ORDER BY revenue DESC" in prompt
    assert "Columns: region, revenue, orders" in prompt
    assert "North America | 120 | 4" in prompt
    assert "Europe | 90 | 2" in prompt
    assert "APAC | 110 | 3" in prompt
    assert "Computed aggregates from returned rows (Python):" in prompt
    assert "- revenue: min=90, max=120, sum=320" in prompt
    assert "- orders: min=2, max=4, sum=9" in prompt
    assert "Pre-computed ranking (highest to lowest by revenue" in prompt
    assert "critically, do not state any number that isn't directly present in or trivially derivable from the provided result data." in prompt.lower()
    assert interpretation.answer == "North America performed best, while Europe lagged behind."
    assert interpretation.raw_llm_output == "North America performed best, while Europe lagged behind."
    assert interpretation.computed_highlights is not None
    assert "rankings" in interpretation.computed_highlights
    assert "revenue" in interpretation.computed_highlights["rankings"]
    assert interpretation.computed_highlights["rankings"]["revenue"]["max"]["label"] == "North America"


def test_result_interpreter_handles_empty_result_special_case() -> None:
    calls: list[str] = []

    class FakeClient:
        def generate(self, prompt, model, system=None, temperature=0.0):
            calls.append(prompt)
            return "This should not be used."

    interpreter = ResultInterpreter(FakeClient(), model="fake-model")
    empty_result = _make_query_result([], columns=["region", "revenue"])

    prompt = interpreter.build_prompt(
        "Which region performed best?",
        "SELECT region, revenue FROM sales",
        empty_result,
    )
    interpretation = interpreter.interpret(
        "Which region performed best?",
        "SELECT region, revenue FROM sales",
        empty_result,
    )

    assert calls == []
    assert "No rows were returned." in prompt
    assert "No matching data was found." in interpretation.answer
    assert interpretation.raw_llm_output == "No matching data was found."
    assert interpretation.computed_highlights is None


def test_result_interpreter_summarizes_large_results_in_prompt() -> None:
    captured: dict[str, str] = {}

    class FakeClient:
        def generate(self, prompt, model, system=None, temperature=0.0):
            captured["prompt"] = prompt
            return "Higher values appear toward the end of the sample."

    interpreter = ResultInterpreter(FakeClient(), model="fake-model")
    rows = [
        {"item": f"item-{index}", "score": index}
        for index in range(1, 61)
    ]
    result = _make_query_result(rows, columns=["item", "score"], truncated=True)

    interpretation = interpreter.interpret(
        "Which items score highest?",
        "SELECT item, score FROM items ORDER BY score DESC",
        result,
    )

    prompt = captured["prompt"]
    assert "Sample rows (showing 5 of 60 returned):" in prompt
    assert "item-1 | 1" in prompt
    assert "item-5 | 5" in prompt
    assert "item-6 | 6" not in prompt
    assert "item-10 | 10" not in prompt
    assert "Pre-computed ranking (highest to lowest by score" in prompt
    assert "Pre-computed ranking (lowest to highest by score" in prompt
    assert "Computed aggregates from returned rows (Python):" in prompt
    assert "- score: min=1, max=60, sum=1830" in prompt
    assert "The result was capped" in prompt
    assert interpretation.computed_highlights is not None
    assert interpretation.computed_highlights["rankings"]["score"]["max"]["label"] == "item-60"
    assert interpretation.computed_highlights["rankings"]["score"]["min"]["label"] == "item-1"


def test_result_interpreter_prompt_includes_precomputed_ranking_for_shuffled_rows() -> None:
    captured: dict[str, str] = {}

    class FakeClient:
        def generate(self, prompt, model, system=None, temperature=0.0):
            captured["prompt"] = prompt
            return "Sub-Saharan Africa ranks highest."

    interpreter = ResultInterpreter(FakeClient(), model="fake-model")
    result = _make_query_result(
        [
            {"region": "Europe", "revenue": 33368932},
            {"region": "Central Asia", "revenue": 29000000},
            {"region": "Sub-Saharan Africa", "revenue": 39672031},
            {"region": "Asia Pacific", "revenue": 32000000},
            {"region": "North America", "revenue": 35200000},
            {"region": "Middle East", "revenue": 28000000},
            {"region": "Latin America", "revenue": 34000000},
        ],
        columns=["region", "revenue"],
    )

    interpretation = interpreter.interpret(
        "Which region has the highest revenue?",
        "SELECT region, revenue FROM sales",
        result,
    )

    prompt = captured["prompt"]
    assert "Pre-computed ranking (highest to lowest by revenue, top 5 of 7 rows): Sub-Saharan Africa (39672031), North America (35200000), Latin America (34000000), Europe (33368932), Asia Pacific (32000000)" in prompt
    assert "Pre-computed ranking (lowest to highest by revenue, bottom 5 of 7 rows): Middle East (28000000), Central Asia (29000000), Asia Pacific (32000000), Europe (33368932), Latin America (34000000)" in prompt
    assert interpretation.computed_highlights is not None
    assert interpretation.computed_highlights["rankings"]["revenue"]["max"]["label"] == "Sub-Saharan Africa"
    assert interpretation.computed_highlights["rankings"]["revenue"]["min"]["label"] == "Middle East"


def test_result_interpreter_prompt_uses_stable_order_for_ties() -> None:
    captured: dict[str, str] = {}

    class FakeClient:
        def generate(self, prompt, model, system=None, temperature=0.0):
            captured["prompt"] = prompt
            return "Alpha and Beta are tied."

    interpreter = ResultInterpreter(FakeClient(), model="fake-model")
    result = _make_query_result(
        [
            {"region": "Beta", "revenue": 200},
            {"region": "Zulu", "revenue": 100},
            {"region": "Alpha", "revenue": 200},
            {"region": "Gamma", "revenue": 199.5},
        ],
        columns=["region", "revenue"],
    )

    interpretation = interpreter.interpret(
        "Which region is on top?",
        "SELECT region, revenue FROM sales",
        result,
    )

    prompt = captured["prompt"]
    assert "Pre-computed ranking (highest to lowest by revenue, top 4 of 4 rows): Alpha (200), Beta (200), Gamma (199.5), Zulu (100)" in prompt
    assert interpretation.computed_highlights is not None
    assert interpretation.computed_highlights["rankings"]["revenue"]["highest_to_lowest"][0]["label"] == "Alpha"
    assert interpretation.computed_highlights["rankings"]["revenue"]["highest_to_lowest"][1]["label"] == "Beta"
    assert interpretation.computed_highlights["rankings"]["revenue"]["max"]["label"] == "Alpha"
    assert interpretation.computed_highlights["rankings"]["revenue"]["min"]["label"] == "Zulu"


def test_result_interpreter_route_preserves_data_when_llm_fails(tmp_path: Path) -> None:
    app = create_app()
    connection_id = _register_sqlite_connection(app, tmp_path)

    class FakeGenerator:
        def generate_sql(self, question, schema_info, dialect, few_shot_examples=None):
            return SQLGenerationResult(
                sql="SELECT id, name FROM items ORDER BY id",
                raw_llm_output="SELECT id, name FROM items ORDER BY id",
                model_used="fake-model",
                prompt_used="fake-prompt",
            )

    class FailingClient:
        def generate(self, prompt, model, system=None, temperature=0.0):
            raise OllamaUnreachableError("http://localhost:11434")

    app.state.sql_generator = FakeGenerator()
    app.state.result_interpreter = ResultInterpreter(FailingClient(), model="fake-model")

    with TestClient(app) as client:
        response = client.post(
            f"/connections/{connection_id}/query/ask",
            json={"question": "List items"},
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
    assert payload["warning"] is not None
    assert "Interpretation unavailable" in payload["warning"]


def test_result_interpreter_treats_single_row_all_null_as_no_data() -> None:
    calls: list[str] = []

    class FakeClient:
        def generate(self, prompt, model, system=None, temperature=0.0):
            calls.append(prompt)
            return "This should not be called."

    interpreter = ResultInterpreter(FakeClient(), model="fake-model")
    null_result = _make_query_result(
        [{"SUM(total_profit)": None}],
        columns=["SUM(total_profit)"],
    )

    interpretation = interpreter.interpret(
        "Show total profit by sales channel",
        "SELECT SUM(total_profit) FROM sales WHERE sales_channel = 'online'",
        null_result,
    )

    assert calls == []
    assert "No matching data was found." in interpretation.answer
    assert interpretation.computed_highlights is None


def test_result_interpreter_does_not_treat_genuine_zero_as_no_data() -> None:
    captured: dict[str, str] = {}

    class FakeClient:
        def generate(self, prompt, model, system=None, temperature=0.0):
            captured["prompt"] = prompt
            return "The count is zero."

    interpreter = ResultInterpreter(FakeClient(), model="fake-model")
    zero_result = _make_query_result(
        [{"COUNT(*)": 0}],
        columns=["COUNT(*)"],
    )

    interpretation = interpreter.interpret(
        "How many orders were placed?",
        "SELECT COUNT(*) FROM orders",
        zero_result,
    )

    assert "prompt" in captured
    assert interpretation.answer == "The count is zero."
    assert "Single value: COUNT(*) = 0" in captured["prompt"]
