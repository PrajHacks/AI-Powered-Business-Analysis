from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import Column, Integer, MetaData, String, Table, create_engine

from app.charting.chart_selector import ChartSelector
from app.db.query_executor import QueryResult
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
        execution_time_ms=10.0,
    )


def _register_sqlite_connection(app, tmp_path: Path) -> str:
    sqlite_path = tmp_path / "chart_test.sqlite"
    engine = create_engine(f"sqlite+pysqlite:///{sqlite_path.resolve().as_posix()}")
    metadata = MetaData()

    sales = Table(
        "sales",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("region", String(50), nullable=False),
        Column("revenue", Integer, nullable=False),
    )
    metadata.create_all(engine)

    with engine.begin() as connection:
        connection.execute(
            sales.insert(),
            [
                {"id": 1, "region": "North America", "revenue": 1200},
                {"id": 2, "region": "Europe", "revenue": 950},
            ],
        )

    engine.dispose()

    return app.state.connection_manager.register_connection(
        name="Chart DB",
        connection_string=f"sqlite+pysqlite:///{sqlite_path.resolve().as_posix()}",
    )


def test_date_and_numeric_selects_line_chart_sorted_chronologically() -> None:
    selector = ChartSelector()
    result = _make_query_result(
        [
            {"order_date": "2024-03-01", "total_sales": 300},
            {"order_date": "2024-01-01", "total_sales": 100},
            {"order_date": "2024-02-01", "total_sales": 200},
        ],
        columns=["order_date", "total_sales"],
    )

    spec = selector.select_chart(result)

    assert spec is not None
    assert spec.chart_type == "line"
    assert "time series" in spec.reasoning.lower() or "chronologically" in spec.reasoning.lower()

    fig_dict = json.loads(spec.plotly_figure_json)
    assert "data" in fig_dict
    assert "layout" in fig_dict
    trace = fig_dict["data"][0]
    assert trace["mode"] == "lines+markers"
    assert trace["x"] == ["2024-01-01", "2024-02-01", "2024-03-01"]
    assert trace["y"] == [100, 200, 300]


def test_category_and_numeric_selects_bar_chart_sorted_descending() -> None:
    selector = ChartSelector()
    result = _make_query_result(
        [
            {"region": "APAC", "revenue": 50},
            {"region": "EMEA", "revenue": 150},
            {"region": "North America", "revenue": 100},
        ],
        columns=["region", "revenue"],
    )

    spec = selector.select_chart(result)

    assert spec is not None
    assert spec.chart_type == "bar"
    assert "descending" in spec.reasoning.lower()

    fig_dict = json.loads(spec.plotly_figure_json)
    trace = fig_dict["data"][0]
    assert trace["type"] == "bar"
    assert trace["x"] == ["EMEA", "North America", "APAC"]
    assert trace["y"] == [150, 100, 50]


def test_two_numeric_columns_selects_scatter_plot() -> None:
    selector = ChartSelector()
    result = _make_query_result(
        [
            {"advertising_spend": 1000, "revenue": 5000},
            {"advertising_spend": 2000, "revenue": 9500},
            {"advertising_spend": 1500, "revenue": 7200},
        ],
        columns=["advertising_spend", "revenue"],
    )

    spec = selector.select_chart(result)

    assert spec is not None
    assert spec.chart_type == "scatter"
    assert "scatter" in spec.reasoning.lower()

    fig_dict = json.loads(spec.plotly_figure_json)
    trace = fig_dict["data"][0]
    assert trace["mode"] == "markers"
    assert trace["x"] == [1000, 2000, 1500]
    assert trace["y"] == [5000, 9500, 7200]


def test_category_and_multiple_numeric_columns_selects_grouped_bar_chart() -> None:
    selector = ChartSelector()
    result = _make_query_result(
        [
            {"channel": "Online", "revenue": 5000, "profit": 1200},
            {"channel": "Retail", "revenue": 3500, "profit": 800},
        ],
        columns=["channel", "revenue", "profit"],
    )

    spec = selector.select_chart(result)

    assert spec is not None
    assert spec.chart_type == "grouped_bar"
    assert "grouped bar" in spec.reasoning.lower()

    fig_dict = json.loads(spec.plotly_figure_json)
    assert len(fig_dict["data"]) == 2
    assert fig_dict["layout"]["barmode"] == "group"
    assert fig_dict["data"][0]["name"] == "revenue"
    assert fig_dict["data"][1]["name"] == "profit"


def test_high_cardinality_category_returns_none_with_cardinality_reasoning() -> None:
    selector = ChartSelector(max_categories=50)
    rows = [{"category": f"cat_{i}", "metric": i * 10} for i in range(100)]
    result = _make_query_result(rows, columns=["category", "metric"])

    spec = selector.select_chart(result)

    assert spec is None
    assert selector.last_reasoning is not None
    assert "cardinality" in selector.last_reasoning.lower() or "too many" in selector.last_reasoning.lower()


def test_zero_rows_returns_none() -> None:
    selector = ChartSelector()
    result = _make_query_result([], columns=["region", "revenue"])

    spec = selector.select_chart(result)

    assert spec is None
    assert selector.last_reasoning is not None
    assert "no data" in selector.last_reasoning.lower() or "0 rows" in selector.last_reasoning.lower()


def test_single_row_single_column_returns_none() -> None:
    selector = ChartSelector()
    result = _make_query_result([{"COUNT(*)": 42}], columns=["COUNT(*)"])

    spec = selector.select_chart(result)

    assert spec is None
    assert selector.last_reasoning is not None
    assert "single scalar value" in selector.last_reasoning.lower() or "scalar" in selector.last_reasoning.lower()


def test_order_id_column_excluded_from_numeric_detection() -> None:
    selector = ChartSelector()
    # If order_id were treated as numeric, this 2-column result would become a scatter plot.
    # Because order_id is excluded from numeric metrics, it is classified as a category,
    # producing a bar chart of revenue by order_id.
    result = _make_query_result(
        [
            {"order_id": 101, "revenue": 500},
            {"order_id": 102, "revenue": 800},
            {"order_id": 103, "revenue": 300},
        ],
        columns=["order_id", "revenue"],
    )

    spec = selector.select_chart(result)

    assert spec is not None
    assert spec.chart_type == "bar"
    assert spec.chart_type != "scatter"


def test_two_id_columns_return_none() -> None:
    selector = ChartSelector()
    # Two ID columns and no numeric metric columns should return None.
    result = _make_query_result(
        [
            {"order_id": 1, "customer_id": 10},
            {"order_id": 2, "customer_id": 20},
        ],
        columns=["order_id", "customer_id"],
    )

    spec = selector.select_chart(result)

    assert spec is None


def test_text_dates_correctly_detected_as_time_dimension() -> None:
    selector = ChartSelector()
    result = _make_query_result(
        [
            {"created_at": "2024-01-15", "count": 12},
            {"created_at": "2024-01-16", "count": 18},
            {"created_at": "2024-01-17", "count": 15},
        ],
        columns=["created_at", "count"],
    )

    spec = selector.select_chart(result)

    assert spec is not None
    assert spec.chart_type == "line"
    assert spec.chart_type != "bar"


def test_plotly_figure_json_is_valid_parseable_json() -> None:
    selector = ChartSelector()
    result = _make_query_result(
        [
            {"category": "Alpha", "value": 10},
            {"category": "Beta", "value": 20},
        ],
        columns=["category", "value"],
    )

    spec = selector.select_chart(result)

    assert spec is not None
    parsed = json.loads(spec.plotly_figure_json)
    assert isinstance(parsed, dict)
    assert "data" in parsed
    assert "layout" in parsed


def test_ask_route_includes_chart_spec(tmp_path: Path) -> None:
    app = create_app()
    connection_id = _register_sqlite_connection(app, tmp_path)

    class FakeGenerator:
        def generate_valid_sql(self, question, schema_info, validator=None, dialect="sqlite", few_shot_examples=None, max_attempts=None):
            return SQLGenerationResult(
                sql="SELECT region, revenue FROM sales ORDER BY revenue DESC",
                raw_llm_output="SELECT region, revenue FROM sales ORDER BY revenue DESC",
                model_used="fake-model",
                prompt_used="fake-prompt",
                validation_passed=True,
                rejection_reason=None,
                attempts=1,
            )

    app.state.sql_generator = FakeGenerator()

    with TestClient(app) as client:
        # Default chart=True
        response = client.post(
            f"/connections/{connection_id}/query/ask",
            json={"question": "Show revenue by region", "interpret": False},
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["chart"] is not None
        assert payload["chart"]["chart_type"] == "bar"
        assert "plotly_figure_json" in payload["chart"]

        # Explicit chart=False
        response_no_chart = client.post(
            f"/connections/{connection_id}/query/ask",
            json={"question": "Show revenue by region", "interpret": False, "chart": False},
        )
        assert response_no_chart.status_code == 200
        payload_no_chart = response_no_chart.json()
        assert payload_no_chart["chart"] is None
