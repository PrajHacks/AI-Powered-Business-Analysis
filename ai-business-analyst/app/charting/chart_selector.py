from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any
import warnings

import pandas as pd
import plotly.graph_objects as go
from pydantic import BaseModel, ConfigDict

from app.db.query_executor import QueryResult


class ChartSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    chart_type: str
    plotly_figure_json: str
    reasoning: str


class ChartSelector:
    """Infers an appropriate chart type deterministically from QueryResult data shape.

    Known limitations and heuristics:
    - Extreme outliers: Numeric columns with extreme outliers may visually compress smaller
      values in bar and line charts. Full automated outlier scaling is not applied.
    - ID column heuristic: Columns named exactly 'id' or ending with '_id' (case-insensitive)
      are excluded from numeric-metric detection and treated as non-metric dimensions.
      This naming heuristic prevents IDs from being misinterpreted as quantitative metrics.
    - Duplicate categories: Duplicate category values are preserved in returned row order
      without silent grouping or deduplication.
    """

    def __init__(self, *, max_categories: int = 50) -> None:
        self.max_categories = max(1, max_categories)
        self.last_reasoning: str | None = None

    def select_chart(self, result: QueryResult) -> ChartSpec | None:
        # Rule 1: 0 rows -> return None (nothing to chart)
        if result.row_count == 0 or not result.rows:
            self.last_reasoning = "No data rows returned to chart."
            return None

        # Rule 2: 1 row, 1 column -> return None (single scalar value)
        if result.row_count == 1 and len(result.columns) == 1:
            self.last_reasoning = "Single scalar value does not require a chart."
            return None

        columns = result.columns or []
        if not columns:
            self.last_reasoning = "QueryResult has no columns."
            return None

        date_cols, numeric_cols, category_cols = self._classify_columns(result)

        # Detection Rule 1: Time series (1 date column + 1 numeric column)
        if len(date_cols) == 1 and len(numeric_cols) == 1 and len(category_cols) == 0:
            return self._build_line_chart(result, date_cols[0], numeric_cols[0])

        # Detection Rule 2: Category + single numeric column
        if len(category_cols) == 1 and len(numeric_cols) == 1 and len(date_cols) == 0:
            cat_col = category_cols[0]
            num_col = numeric_cols[0]
            distinct_cats = len({row.get(cat_col) for row in result.rows})
            if distinct_cats > self.max_categories:
                self.last_reasoning = (
                    f"Too many distinct categories ({distinct_cats} > {self.max_categories}) "
                    f"in column '{cat_col}' for a readable bar chart (cardinality limit exceeded)."
                )
                return None
            return self._build_bar_chart(result, cat_col, num_col, distinct_cats)

        # Detection Rule 3: Two numeric columns (no categorical/date column)
        if len(numeric_cols) == 2 and len(category_cols) == 0 and len(date_cols) == 0:
            return self._build_scatter_chart(result, numeric_cols[0], numeric_cols[1])

        # Detection Rule 4: One categorical column + multiple numeric columns
        if len(category_cols) == 1 and len(numeric_cols) >= 2 and len(date_cols) == 0:
            cat_col = category_cols[0]
            distinct_cats = len({row.get(cat_col) for row in result.rows})
            if distinct_cats > self.max_categories:
                self.last_reasoning = (
                    f"Too many distinct categories ({distinct_cats} > {self.max_categories}) "
                    f"in column '{cat_col}' for a readable grouped bar chart (cardinality limit exceeded)."
                )
                return None
            return self._build_grouped_bar_chart(result, cat_col, numeric_cols, distinct_cats)

        # Fallback: Everything else (too many columns, no clear structure, etc.)
        self.last_reasoning = (
            f"No chart pattern matched data shape: {len(date_cols)} date column(s), "
            f"{len(numeric_cols)} numeric metric column(s), {len(category_cols)} categorical column(s)."
        )
        return None

    def _classify_columns(
        self,
        result: QueryResult,
    ) -> tuple[list[str], list[str], list[str]]:
        date_cols: list[str] = []
        numeric_cols: list[str] = []
        category_cols: list[str] = []

        for col in result.columns:
            values = [row.get(col) for row in result.rows]
            is_date, _ = self._try_parse_dates(col, values)
            if is_date:
                date_cols.append(col)
            elif self._is_numeric_column(col, values):
                numeric_cols.append(col)
            else:
                category_cols.append(col)

        return date_cols, numeric_cols, category_cols

    def _is_id_column(self, col_name: str) -> bool:
        # Heuristic: exclude columns ending in "_id" or named "id" (case-insensitive) from numeric metrics.
        # Note: this is a naming heuristic and not foolproof, but prevents surrogate keys/foreign keys
        # from being incorrectly charted as quantitative measures.
        name = col_name.strip().lower()
        return name == "id" or name.endswith("_id")

    def _is_numeric_column(self, col_name: str, values: list[Any]) -> bool:
        if self._is_id_column(col_name):
            return False

        non_null = [v for v in values if v is not None]
        if not non_null:
            return False

        numeric_count = sum(1 for v in non_null if self._is_numeric_val(v))
        return (numeric_count / len(non_null)) >= 0.9

    def _is_numeric_val(self, val: Any) -> bool:
        if isinstance(val, bool):
            return False
        if isinstance(val, (int, float, Decimal)):
            return True
        if isinstance(val, str):
            try:
                float(val.strip())
                return True
            except (ValueError, TypeError):
                return False
        return False

    def _try_parse_dates(
        self,
        col_name: str,
        values: list[Any],
    ) -> tuple[bool, list[Any] | None]:
        non_null = [v for v in values if v is not None]
        if not non_null:
            return False, None

        if all(isinstance(v, (date, datetime, pd.Timestamp)) for v in non_null):
            parsed = [pd.to_datetime(v) if v is not None else None for v in values]
            return True, parsed

        if all(isinstance(v, (str, date, datetime, pd.Timestamp)) for v in non_null):
            first_str = str(non_null[0]).strip()
            if first_str.isdigit() and len(first_str) < 4:
                return False, None
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    series = pd.Series(values)
                    converted = pd.to_datetime(series, format="mixed", errors="coerce")
                    valid_count = converted.notna().sum()
                    if (valid_count / len(non_null)) >= 0.9:
                        return True, converted.tolist()
            except Exception:
                return False, None

        return False, None

    def _build_line_chart(
        self,
        result: QueryResult,
        date_col: str,
        num_col: str,
    ) -> ChartSpec:
        _, parsed_dates = self._try_parse_dates(date_col, [row.get(date_col) for row in result.rows])
        if parsed_dates is None:
            parsed_dates = [pd.to_datetime(row.get(date_col), errors="coerce") for row in result.rows]

        indexed_rows = list(zip(parsed_dates, result.rows))
        indexed_rows.sort(
            key=lambda pair: (
                pair[0] is None or pd.isna(pair[0]),
                pair[0] if pair[0] is not None and not pd.isna(pair[0]) else pd.Timestamp.min,
            )
        )

        x_vals = [row.get(date_col) for _, row in indexed_rows]
        y_vals = [row.get(num_col) for _, row in indexed_rows]

        fig = go.Figure(
            data=[
                go.Scatter(
                    x=x_vals,
                    y=y_vals,
                    mode="lines+markers",
                    name=num_col,
                )
            ],
            layout=go.Layout(
                title=f"{num_col} over {date_col}",
                xaxis_title=date_col,
                yaxis_title=num_col,
                template="plotly_white",
            ),
        )
        reasoning = (
            f"Detected a date column ({date_col}) and a numeric column ({num_col}) "
            f"- showing as a time series line chart sorted chronologically."
        )
        self.last_reasoning = reasoning
        return ChartSpec(
            chart_type="line",
            plotly_figure_json=fig.to_json(),
            reasoning=reasoning,
        )

    def _build_bar_chart(
        self,
        result: QueryResult,
        cat_col: str,
        num_col: str,
        distinct_count: int,
    ) -> ChartSpec:
        def _to_float(v: Any) -> float:
            if v is None:
                return float("-inf")
            try:
                return float(v)
            except (ValueError, TypeError):
                return float("-inf")

        sorted_rows = sorted(
            result.rows,
            key=lambda r: _to_float(r.get(num_col)),
            reverse=True,
        )

        x_vals = [row.get(cat_col) for row in sorted_rows]
        y_vals = [row.get(num_col) for row in sorted_rows]

        fig = go.Figure(
            data=[
                go.Bar(
                    x=x_vals,
                    y=y_vals,
                    name=num_col,
                )
            ],
            layout=go.Layout(
                title=f"{num_col} by {cat_col}",
                xaxis_title=cat_col,
                yaxis_title=num_col,
                template="plotly_white",
            ),
        )
        reasoning = (
            f"Detected a category column ({cat_col}) with {distinct_count} distinct values "
            f"and a numeric column ({num_col}) - showing as a bar chart sorted descending by value."
        )
        self.last_reasoning = reasoning
        return ChartSpec(
            chart_type="bar",
            plotly_figure_json=fig.to_json(),
            reasoning=reasoning,
        )

    def _build_scatter_chart(
        self,
        result: QueryResult,
        num_col1: str,
        num_col2: str,
    ) -> ChartSpec:
        x_vals = [row.get(num_col1) for row in result.rows]
        y_vals = [row.get(num_col2) for row in result.rows]

        fig = go.Figure(
            data=[
                go.Scatter(
                    x=x_vals,
                    y=y_vals,
                    mode="markers",
                )
            ],
            layout=go.Layout(
                title=f"{num_col2} vs {num_col1}",
                xaxis_title=num_col1,
                yaxis_title=num_col2,
                template="plotly_white",
            ),
        )
        reasoning = (
            f"Detected two numeric columns ({num_col1}, {num_col2}) without categorical dimensions "
            f"- showing as a scatter plot."
        )
        self.last_reasoning = reasoning
        return ChartSpec(
            chart_type="scatter",
            plotly_figure_json=fig.to_json(),
            reasoning=reasoning,
        )

    def _build_grouped_bar_chart(
        self,
        result: QueryResult,
        cat_col: str,
        numeric_cols: list[str],
        distinct_count: int,
    ) -> ChartSpec:
        x_vals = [row.get(cat_col) for row in result.rows]

        traces = [
            go.Bar(
                name=col,
                x=x_vals,
                y=[row.get(col) for row in result.rows],
            )
            for col in numeric_cols
        ]

        fig = go.Figure(
            data=traces,
            layout=go.Layout(
                title=f"Metrics by {cat_col}",
                xaxis_title=cat_col,
                barmode="group",
                template="plotly_white",
            ),
        )
        reasoning = (
            f"Detected a category column ({cat_col}) and {len(numeric_cols)} numeric columns "
            f"({', '.join(numeric_cols)}) - showing as a grouped bar chart."
        )
        self.last_reasoning = reasoning
        return ChartSpec(
            chart_type="grouped_bar",
            plotly_figure_json=fig.to_json(),
            reasoning=reasoning,
        )
