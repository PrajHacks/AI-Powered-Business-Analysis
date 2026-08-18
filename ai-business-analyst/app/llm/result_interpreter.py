from __future__ import annotations

from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation
from numbers import Number
from typing import Any

from pydantic import BaseModel, ConfigDict

from app.config import get_settings
from app.db.query_executor import QueryResult
from app.llm.ollama_client import OllamaClient, OllamaError


class ResultInterpretationError(RuntimeError):
    """Raised when the interpretation step cannot produce an answer."""


class InterpretationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    answer: str
    raw_llm_output: str
    computed_highlights: dict[str, Any] | None = None


class ResultInterpreter:
    def __init__(
        self,
        client: OllamaClient,
        *,
        model: str | None = None,
        sample_row_limit: int = 5,
        ranking_row_limit: int = 5,
    ) -> None:
        self.client = client
        self.model = model or get_settings().ollama_model
        self.sample_row_limit = max(1, sample_row_limit)
        self.ranking_row_limit = max(1, ranking_row_limit)

    def interpret(
        self,
        question: str,
        sql: str,
        result: QueryResult,
        semantic_context: str | None = None,
    ) -> InterpretationResult:
        if result.row_count == 0 or not result.rows:
            answer = "No matching data was found."
            return InterpretationResult(
                answer=answer,
                raw_llm_output=answer,
                computed_highlights=None,
            )

        if self._is_all_null_result(result):
            answer = "No matching data was found."
            return InterpretationResult(
                answer=answer,
                raw_llm_output=answer,
                computed_highlights=None,
            )

        prompt, computed_highlights = self._build_prompt_components(
            question, sql, result, semantic_context=semantic_context
        )
        try:
            raw_output = self.client.generate(
                prompt=prompt,
                model=self.model,
                system=None,
                temperature=0.0,
            )
        except OllamaError as exc:
            raise ResultInterpretationError(f"Ollama interpretation failed: {exc}") from None

        answer = raw_output.strip()
        if not answer:
            raise ResultInterpretationError("Ollama returned an empty interpretation.")

        return InterpretationResult(
            answer=answer,
            raw_llm_output=raw_output,
            computed_highlights=computed_highlights,
        )

    def build_prompt(
        self,
        question: str,
        sql: str,
        result: QueryResult,
        semantic_context: str | None = None,
    ) -> str:
        prompt, _ = self._build_prompt_components(
            question, sql, result, semantic_context=semantic_context
        )
        return prompt

    def _build_prompt_components(
        self,
        question: str,
        sql: str,
        result: QueryResult,
        semantic_context: str | None = None,
    ) -> tuple[str, dict[str, Any] | None]:
        question_text = question.strip() or "(no question provided)"
        sql_text = sql.strip() or "(no SQL provided)"
        summary_lines, computed_highlights = self._format_result_summary(result)

        lines: list[str] = [
            "You explain SQL query results in plain English.",
        ]
        if semantic_context and semantic_context.strip():
            lines.extend(
                [
                    "Business Glossary / Semantic Context:",
                    semantic_context.strip(),
                ]
            )
        lines.extend(
            [
                "Question:",
                question_text,
                "SQL executed for context only. Do not mention SQL or table names in the answer:",
                sql_text,
                "Result data:",
                *summary_lines,
                "Instructions:",
                "- Answer in plain English in 2-4 sentences.",
                "- Focus on what the data means for the person asking. Don't just restate numbers; mention trends, notable outliers, or comparisons only when the provided data supports them.",
                "- Use business-friendly terminology from the Business Glossary when appropriate.",
                "- Do not show SQL or table names in the answer.",
                "- Do not say 'the query returned' or similar meta-commentary.",
                "- Critically, do not state any number that isn't directly present in or trivially derivable from the provided result data.",
                "- Do not invent statistics, trends, or comparisons that are not supported by the rows below.",
            ]
        )
        if computed_highlights and computed_highlights.get("rankings"):
            # Local/smaller models can still ignore these instructions, so this answer is assistive
            # context rather than an authoritative source of truth.
            lines.extend(
                [
                    "- Pre-computed rankings below are ground truth derived in Python from the full returned rows.",
                    "- If you mention highest, lowest, top, or bottom, your claim must match the matching pre-computed ranking exactly.",
                    "- Do not infer rankings from the sample table or from row order.",
                    "- Ties are already resolved in the pre-computed ranking order; do not re-rank them yourself.",
                ]
            )
        if result.truncated:
            lines.append(
                "- The result was capped, so make that clear and treat the rows below as a partial view."
            )
        if result.row_count == 0 or not result.rows:
            lines.extend(
                [
                    "Special case:",
                    "- No rows were returned.",
                    "- Say clearly that no matching data was found.",
                    "- Do not speculate or invent missing values.",
                ]
            )
        elif self._is_single_value_result(result):
            lines.extend(
                [
                    "Special case:",
                    "- This is a single value result.",
                    "- Keep the answer especially concise, ideally one short sentence.",
                ]
            )
        return "\n".join(lines), computed_highlights

    def _format_result_summary(self, result: QueryResult) -> tuple[list[str], dict[str, Any] | None]:
        lines: list[str] = []
        computed_highlights: dict[str, Any] = {
            "row_count": result.row_count,
            "columns": list(result.columns or []),
        }
        columns = result.columns or []
        lines.append(f"Columns: {', '.join(columns) if columns else '(none)'}")
        lines.append(f"Rows returned: {result.row_count}")

        if result.row_count == 0 or not result.rows:
            lines.append("No rows were returned.")
            return lines, None

        if self._is_single_value_result(result):
            column = result.columns[0]
            value = result.rows[0].get(column)
            formatted_value = self._format_value(value)
            lines.append(f"Single value: {column} = {formatted_value}")
            computed_highlights["single_value"] = {
                "column": column,
                "value": formatted_value,
            }
            return lines, computed_highlights

        ranking_lines, ranking_highlights = self._compute_rankings(result)
        if ranking_lines:
            lines.extend(ranking_lines)
            computed_highlights["rankings"] = ranking_highlights

        sample_rows = result.rows[: self.sample_row_limit]
        shown_count = len(sample_rows)
        total_count = len(result.rows)
        computed_highlights["sample_rows"] = [
            {column: self._format_value(row.get(column)) for column in columns}
            for row in sample_rows
        ]
        lines.append(
            f"Sample rows (showing {shown_count} of {total_count} returned):"
        )
        lines.extend(self._format_table(sample_rows, columns))

        aggregates = self._compute_numeric_aggregates(result)
        if aggregates:
            lines.append("Computed aggregates from returned rows (Python):")
            for column, stats in aggregates.items():
                lines.append(
                    f"- {column}: min={stats['min']}, max={stats['max']}, sum={stats['sum']}"
                )
            computed_highlights["aggregates"] = aggregates

        return lines, computed_highlights

    def _format_table(self, rows: list[dict[str, Any]], columns: list[str]) -> list[str]:
        if not rows:
            return []

        lines = [" | ".join(columns)]
        for row in rows:
            values = [self._format_value(row.get(column)) for column in columns]
            lines.append(" | ".join(values))
        return lines

    def _compute_numeric_aggregates(self, result: QueryResult) -> dict[str, dict[str, str]]:
        aggregates: dict[str, dict[str, str]] = {}
        for column in result.columns:
            values = [
                value
                for value in (row.get(column) for row in result.rows)
                if self._is_numeric_value(value)
            ]
            if not values:
                continue

            numeric_values = [Decimal(str(value)) for value in values]
            aggregates[column] = {
                "min": self._format_number(min(numeric_values)),
                "max": self._format_number(max(numeric_values)),
                "sum": self._format_number(sum(numeric_values, Decimal("0"))),
            }
        return aggregates

    def _compute_rankings(self, result: QueryResult) -> tuple[list[str], dict[str, Any]]:
        ranking_lines: list[str] = []
        ranking_highlights: dict[str, Any] = {}
        columns = result.columns or []

        for column in columns:
            ranking = self._rank_numeric_column(result.rows, columns, column)
            if ranking is None:
                continue

            total_count = ranking["count"]
            top_entries = ranking["highest_to_lowest"][: self.ranking_row_limit]
            bottom_entries = ranking["lowest_to_highest"][: self.ranking_row_limit]
            top_count = min(self.ranking_row_limit, total_count)
            bottom_count = min(self.ranking_row_limit, total_count)

            ranking_highlights[column] = ranking
            ranking_lines.append(
                f"Pre-computed ranking (highest to lowest by {column}, top {top_count} of {total_count} rows): {self._format_ranking_entries(top_entries)}"
            )
            ranking_lines.append(
                f"Pre-computed ranking (lowest to highest by {column}, bottom {bottom_count} of {total_count} rows): {self._format_ranking_entries(bottom_entries)}"
            )
            ranking_lines.append(
                f"Pre-computed extrema (Python) for {column}: max={ranking['max']['label']} ({ranking['max']['value']}), min={ranking['min']['label']} ({ranking['min']['value']})"
            )

        return ranking_lines, ranking_highlights

    def _rank_numeric_column(
        self,
        rows: list[dict[str, Any]],
        columns: list[str],
        metric_column: str,
    ) -> dict[str, Any] | None:
        ranked_rows: list[dict[str, Any]] = []

        for index, row in enumerate(rows):
            numeric_value = self._coerce_numeric_value(row.get(metric_column))
            if numeric_value is None:
                continue

            ranked_rows.append(
                {
                    "numeric_value": numeric_value,
                    "label": self._build_row_label(row, columns, metric_column, index),
                    "value": self._format_value(row.get(metric_column)),
                    "row_index": index,
                }
            )

        if len(ranked_rows) < 2:
            return None

        ranked_desc = sorted(
            ranked_rows,
            key=lambda item: (
                -item["numeric_value"],
                item["label"].casefold(),
                item["row_index"],
            ),
        )
        ranked_asc = sorted(
            ranked_rows,
            key=lambda item: (
                item["numeric_value"],
                item["label"].casefold(),
                item["row_index"],
            ),
        )

        def to_public_entry(item: dict[str, Any]) -> dict[str, Any]:
            return {
                "label": item["label"],
                "value": item["value"],
                "row_index": item["row_index"],
            }

        return {
            "sort_column": metric_column,
            "count": len(ranked_rows),
            "highest_to_lowest": [to_public_entry(item) for item in ranked_desc],
            "lowest_to_highest": [to_public_entry(item) for item in ranked_asc],
            "max": to_public_entry(ranked_desc[0]),
            "min": to_public_entry(ranked_desc[-1]),
            "tie_break": "label_then_row_index",
        }

    def _build_row_label(
        self,
        row: dict[str, Any],
        columns: list[str],
        metric_column: str,
        row_index: int,
    ) -> str:
        text_parts: list[str] = []
        fallback_parts: list[str] = []

        for column in columns:
            if column == metric_column:
                continue

            value = row.get(column)
            if value is None:
                continue

            formatted = self._format_value(value)
            if not formatted or formatted == "NULL":
                continue

            if self._is_numeric_value(value):
                fallback_parts.append(formatted)
            else:
                text_parts.append(formatted)

        parts = text_parts or fallback_parts
        if parts:
            return " / ".join(parts[:3])
        return f"row {row_index + 1}"

    def _format_ranking_entries(self, entries: list[dict[str, Any]]) -> str:
        return ", ".join(
            f"{entry['label']} ({entry['value']})" for entry in entries
        )

    def _coerce_numeric_value(self, value: Any) -> Decimal | None:
        if not self._is_numeric_value(value):
            return None

        try:
            decimal_value = value if isinstance(value, Decimal) else Decimal(str(value))
        except (InvalidOperation, ValueError, TypeError):
            return None

        if not decimal_value.is_finite():
            return None
        return decimal_value

    def _is_single_value_result(self, result: QueryResult) -> bool:
        return result.row_count == 1 and len(result.columns) == 1 and bool(result.rows)

    def _is_all_null_result(self, result: QueryResult) -> bool:
        if result.row_count != 1 or not result.rows:
            return False
        row = result.rows[0]
        return all(row.get(col) is None for col in result.columns)

    def _is_numeric_value(self, value: Any) -> bool:
        return isinstance(value, Number) and not isinstance(value, bool)

    def _format_value(self, value: Any) -> str:
        if value is None:
            return "NULL"
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, Decimal):
            return self._format_number(value)
        if isinstance(value, int):
            return str(value)
        if isinstance(value, float):
            return f"{value:.15g}"
        if isinstance(value, (datetime, date, time)):
            return value.isoformat()
        text = str(value).replace("\n", " ").replace("\r", " ").strip()
        if len(text) > 80:
            return text[:77] + "..."
        return text

    def _format_number(self, value: Decimal) -> str:
        normalized = value.normalize()
        text = format(normalized, "f")
        if "." in text:
            text = text.rstrip("0").rstrip(".")
        return text or "0"
