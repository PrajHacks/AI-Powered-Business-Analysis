from __future__ import annotations

import re
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

from app.config import get_settings
from app.db.introspection import SchemaInfo
from app.db.sql_safety import SQLSafetyValidator
from app.llm.ollama_client import OllamaClient

if TYPE_CHECKING:
    from app.conversation.memory import ConversationTurn


class SQLGenerationError(RuntimeError):
    """Base exception for SQL generation failures."""


class SQLGenerationParseError(SQLGenerationError):
    """Raised when an LLM response cannot be parsed into SQL."""


class SQLGenerationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sql: str
    raw_llm_output: str
    model_used: str
    prompt_used: str
    validation_passed: bool = True
    rejection_reason: str | None = None
    attempts: int = 1


class SQLGenerator:
    def __init__(
        self,
        client: OllamaClient,
        *,
        model: str | None = None,
    ) -> None:
        self.client = client
        self.model = model or get_settings().ollama_model

    def build_prompt(
        self,
        question: str,
        schema_info: SchemaInfo,
        dialect: str,
        few_shot_examples: list[dict] | None = None,
        conversation_history: list[ConversationTurn] | None = None,
        prompt_window: int | None = None,
        semantic_context: str | None = None,
    ) -> str:
        normalized_dialect = dialect.strip().lower() or "unknown"
        lines: list[str] = [
            "You generate read-only SQL SELECT queries.",
            f"Target SQL dialect: {normalized_dialect}",
            "Rules:",
            "- Output ONLY the SQL query.",
            "- No prose, no markdown fences, no code blocks.",
            "- Read-only queries only. Never use DDL or DML.",
            "- Use only tables and columns that exist in the provided schema.",
            "- Use only actual column names from the Schema in SQL queries. Never use business glossary terms or synonyms as column names in the generated SQL.",
            "- Prefer explicit column lists over SELECT *.",
            "- If the query uses GROUP BY, every selected column that is not in the GROUP BY clause MUST be wrapped in an aggregate function (SUM, AVG, COUNT, MIN, MAX). Never select a raw non-aggregated column alongside GROUP BY.",
            "- If the query uses GROUP BY, the SELECT list MUST include every column that appears in the GROUP BY clause (so results can be identified/labeled), in addition to the required aggregate functions for other columns. Never group by a column without also selecting it.",
            "- When a question asks for a breakdown 'by' a SPECIFIC column or concept (e.g. 'total X by Y', 'X grouped by Y', 'X per Y'), you MUST use GROUP BY on that column and return all groups. Group ONLY by that column/concept - do not add additional GROUP BY columns that were not requested, even if they exist in the schema or business glossary, unless the question explicitly asks for a breakdown by multiple dimensions (e.g. 'by region and item type'). Do NOT filter to a single guessed value with WHERE instead of grouping.",
            "- When filtering text/string columns with WHERE, always use case-insensitive comparison to avoid missing rows due to casing differences. See dialect-specific guidance below for the correct syntax.",
            "- Add a reasonable LIMIT when the question does not require a full aggregate result.",
            "Dialect-specific guidance:",
            self._dialect_guidance(normalized_dialect),
        ]

        if few_shot_examples:
            lines.append("Examples:")
            lines.extend(self._format_examples(few_shot_examples))

        # Include a bounded window of recent conversation turns so the model
        # can resolve references like 'break that down by region' or 'now show
        # it as a percentage' against the prior question/SQL/result.
        if conversation_history:
            history_block = self._build_history_context(
                conversation_history,
                window=prompt_window if prompt_window is not None else get_settings().conversation_prompt_window,
            )
            if history_block:
                lines.append(history_block)

        lines.append("Schema:")
        lines.append(schema_info.to_llm_context())
        if semantic_context and semantic_context.strip():
            lines.append("Business Glossary / Semantic Context (supplemental business context; column names in generated SQL MUST still match the Schema):")
            lines.append(semantic_context.strip())
        lines.append("Question:")
        lines.append(question.strip())
        lines.append("SQL:")
        return "\n".join(lines)

    def build_retry_prompt(
        self,
        question: str,
        schema_info: SchemaInfo,
        dialect: str,
        rejected_sql: str,
        rejection_reason: str,
        few_shot_examples: list[dict] | None = None,
        conversation_history: list[ConversationTurn] | None = None,
        prompt_window: int | None = None,
        semantic_context: str | None = None,
    ) -> str:
        normalized_dialect = dialect.strip().lower() or "unknown"
        lines: list[str] = [
            "You generate read-only SQL SELECT queries.",
            f"Target SQL dialect: {normalized_dialect}",
            "Rules:",
            "- Output ONLY the SQL query.",
            "- No prose, no markdown fences, no code blocks.",
            "- Read-only queries only. Never use DDL or DML.",
            "- Use only tables and columns that exist in the provided schema.",
            "- Use only actual column names from the Schema in SQL queries. Never use business glossary terms or synonyms as column names in the generated SQL.",
            "- Prefer explicit column lists over SELECT *.",
            "- If the query uses GROUP BY, every selected column that is not in the GROUP BY clause MUST be wrapped in an aggregate function (SUM, AVG, COUNT, MIN, MAX). Never select a raw non-aggregated column alongside GROUP BY.",
            "- If the query uses GROUP BY, the SELECT list MUST include every column that appears in the GROUP BY clause (so results can be identified/labeled), in addition to the required aggregate functions for other columns. Never group by a column without also selecting it.",
            "- When a question asks for a breakdown 'by' a SPECIFIC column or concept (e.g. 'total X by Y', 'X grouped by Y', 'X per Y'), you MUST use GROUP BY on that column and return all groups. Group ONLY by that column/concept - do not add additional GROUP BY columns that were not requested, even if they exist in the schema or business glossary, unless the question explicitly asks for a breakdown by multiple dimensions (e.g. 'by region and item type'). Do NOT filter to a single guessed value with WHERE instead of grouping.",
            "- When filtering text/string columns with WHERE, always use case-insensitive comparison to avoid missing rows due to casing differences. See dialect-specific guidance below for the correct syntax.",
            "- Add a reasonable LIMIT when the question does not require a full aggregate result.",
            "Dialect-specific guidance:",
            self._dialect_guidance(normalized_dialect),
        ]

        if few_shot_examples:
            lines.append("Examples:")
            lines.extend(self._format_examples(few_shot_examples))

        if conversation_history:
            history_block = self._build_history_context(
                conversation_history,
                window=prompt_window if prompt_window is not None else get_settings().conversation_prompt_window,
            )
            if history_block:
                lines.append(history_block)

        lines.append("Schema:")
        lines.append(schema_info.to_llm_context())
        if semantic_context and semantic_context.strip():
            lines.append("Business Glossary / Semantic Context (supplemental business context; column names in generated SQL MUST still match the Schema):")
            lines.append(semantic_context.strip())
        lines.append("Question:")
        lines.append(question.strip())
        lines.append("Previous rejected SQL:")
        lines.append(rejected_sql.strip())
        lines.append("Validation rejection reason:")
        lines.append(rejection_reason.strip())
        lines.append("Correction instruction:")
        lines.append(
            "Fix ONLY the identified problem above and return the corrected SQL query. "
            "Do not output any explanation, markdown formatting, or prose."
        )
        lines.append("SQL:")
        return "\n".join(lines)

    def generate_sql(
        self,
        question: str,
        schema_info: SchemaInfo,
        dialect: str,
        few_shot_examples: list[dict] | None = None,
        conversation_history: list[ConversationTurn] | None = None,
        prompt_window: int | None = None,
        semantic_context: str | None = None,
    ) -> SQLGenerationResult:
        prompt = self.build_prompt(
            question=question,
            schema_info=schema_info,
            dialect=dialect,
            few_shot_examples=few_shot_examples,
            conversation_history=conversation_history,
            prompt_window=prompt_window,
            semantic_context=semantic_context,
        )
        raw_output = self.client.generate(
            prompt=prompt,
            model=self.model,
            system=None,
            temperature=0.0,
        )
        sql = self._extract_sql(raw_output)
        return SQLGenerationResult(
            sql=sql,
            raw_llm_output=raw_output,
            model_used=self.model,
            prompt_used=prompt,
            validation_passed=True,
            rejection_reason=None,
            attempts=1,
        )

    def generate_valid_sql(
        self,
        question: str,
        schema_info: SchemaInfo,
        validator: SQLSafetyValidator | None = None,
        dialect: str = "sqlite",
        few_shot_examples: list[dict] | None = None,
        max_attempts: int | None = None,
        conversation_history: list[ConversationTurn] | None = None,
        prompt_window: int | None = None,
        semantic_context: str | None = None,
    ) -> SQLGenerationResult:
        if isinstance(validator, str) and dialect == "sqlite":
            dialect = validator
            validator = None

        active_validator = validator or SQLSafetyValidator()
        if max_attempts is None:
            max_attempts = get_settings().sql_generation_max_attempts
        max_attempts = max(1, max_attempts)

        last_result: SQLGenerationResult | None = None
        last_sql: str | None = None
        last_reason: str | None = None

        for attempt in range(1, max_attempts + 1):
            if attempt == 1:
                prompt = self.build_prompt(
                    question=question,
                    schema_info=schema_info,
                    dialect=dialect,
                    few_shot_examples=few_shot_examples,
                    conversation_history=conversation_history,
                    prompt_window=prompt_window,
                    semantic_context=semantic_context,
                )
            else:
                prompt = self.build_retry_prompt(
                    question=question,
                    schema_info=schema_info,
                    dialect=dialect,
                    rejected_sql=last_sql or "",
                    rejection_reason=last_reason or "Safety validation failed.",
                    few_shot_examples=few_shot_examples,
                    conversation_history=conversation_history,
                    prompt_window=prompt_window,
                    semantic_context=semantic_context,
                )

            raw_output = self.client.generate(
                prompt=prompt,
                model=self.model,
                system=None,
                temperature=0.0,
            )
            sql = self._extract_sql(raw_output)
            validation = active_validator.validate(sql)

            if validation.is_safe:
                return SQLGenerationResult(
                    sql=validation.normalized_sql or sql,
                    raw_llm_output=raw_output,
                    model_used=self.model,
                    prompt_used=prompt,
                    validation_passed=True,
                    rejection_reason=None,
                    attempts=attempt,
                )

            last_sql = sql
            last_reason = validation.reason or "Generated SQL failed safety validation."
            last_result = SQLGenerationResult(
                sql=sql,
                raw_llm_output=raw_output,
                model_used=self.model,
                prompt_used=prompt,
                validation_passed=False,
                rejection_reason=last_reason,
                attempts=attempt,
            )

        if last_result is not None:
            return last_result

        raise SQLGenerationError("No SQL generation attempts were made.")

    def _build_history_context(
        self,
        history: list[ConversationTurn],
        window: int,
    ) -> str:
        """Format the most-recent *window* turns into a compact prompt block.

        Only the last *window* turns are emitted even if more are stored, to
        keep token usage bounded.  Turns are ordered oldest-first so the model
        reads them in chronological order, with the most-recent turn closest
        to the current question.
        """
        if not history:
            return ""
        # Take the last `window` turns (most recent).
        recent = history[-window:] if len(history) > window else history
        lines: list[str] = ["Conversation history (most recent last):"]
        for i, turn in enumerate(recent, start=1):
            cols = turn.query_result_summary.get("columns", [])
            row_count = turn.query_result_summary.get("row_count", "?")
            lines.append(f"[Turn {i}]")
            lines.append(f"Previous question: {turn.question}")
            lines.append(f"Previous SQL: {turn.generated_sql}")
            lines.append(f"Previous result had columns: {cols}, row_count: {row_count}")
        return "\n".join(lines)

    def _dialect_guidance(self, dialect: str) -> str:
        normalized_dialect = dialect.strip().lower()
        if normalized_dialect == "sqlite":
            return (
                "SQLite date/time guidance: use strftime('%Y', col), strftime('%m', col), "
                "strftime('%d', col), etc. for date parts. "
                "SQLite case-insensitive text filtering: use col = 'value' COLLATE NOCASE "
                "or LOWER(col) = LOWER('value') for text comparisons in WHERE clauses."
            )
        if normalized_dialect == "postgresql":
            return (
                "PostgreSQL date/time guidance: EXTRACT(part FROM col) or date_trunc() are fine. "
                "PostgreSQL case-insensitive text filtering: use ILIKE instead of = for "
                "case-insensitive text matching, or LOWER(col) = LOWER('value')."
            )
        if normalized_dialect == "mysql":
            return (
                "MySQL date/time guidance: YEAR(col), MONTH(col), DAY(col), etc. "
                "MySQL case-insensitive text filtering: MySQL string comparisons are "
                "case-insensitive by default with most collations, but use LOWER(col) = "
                "LOWER('value') when unsure."
            )
        return (
            f"{normalized_dialect or 'unknown'} date/time guidance: use dialect-native "
            "date/time functions and avoid assuming another SQL dialect."
        )

    def _format_examples(self, few_shot_examples: list[dict]) -> list[str]:
        formatted: list[str] = []
        for index, example in enumerate(few_shot_examples, start=1):
            question = example.get("question")
            sql = example.get("sql")
            if not isinstance(question, str) or not isinstance(sql, str):
                raise SQLGenerationError(
                    "Few-shot examples must include string 'question' and 'sql' fields."
                )
            formatted.extend(
                [
                    f"Example {index}:",
                    f"Question: {question.strip()}",
                    f"SQL: {sql.strip()}",
                ]
            )
        return formatted

    def _extract_sql(self, raw_output: str) -> str:
        candidate = raw_output.strip()
        candidate = self._strip_code_fences(candidate)
        candidate = candidate.strip()

        sql_start = re.search(r"(?is)\b(with|select)\b", candidate)
        if sql_start is not None:
            candidate = candidate[sql_start.start() :].strip()

        candidate = candidate.rstrip("`").strip()
        candidate = self._trim_after_terminal_semicolon(candidate)
        candidate = self._trim_trailing_prose(candidate)

        if not re.search(r"(?is)^\s*(with|select)\b", candidate):
            raise SQLGenerationParseError(
                "Ollama did not return a recognizable SQL SELECT query."
            )

        if not self._looks_like_sql(candidate):
            raise SQLGenerationParseError(
                "Ollama returned text that could not be parsed as SQL."
            )

        return candidate

    def _strip_code_fences(self, text: str) -> str:
        fence_match = re.search(r"```(?:sql)?\s*(.*?)\s*```", text, flags=re.IGNORECASE | re.DOTALL)
        if fence_match is not None:
            return fence_match.group(1).strip()
        return text

    def _trim_after_terminal_semicolon(self, text: str) -> str:
        semicolons = [match.start() for match in re.finditer(r";", text)]
        if not semicolons:
            return text

        last_semicolon = semicolons[-1]
        tail = text[last_semicolon + 1 :].strip()
        if not tail:
            return text[: last_semicolon + 1].strip()

        if re.match(r"(?is)^(here|sure|note|i am|i'm|the query|below|question|explanation)\b", tail):
            return text[: last_semicolon + 1].strip()

        return text

    def _trim_trailing_prose(self, text: str) -> str:
        lines = text.splitlines()
        if len(lines) <= 1:
            return text.strip()

        kept_lines: list[str] = []
        sql_seen = False
        for line in lines:
            stripped = line.strip()
            if not stripped:
                if sql_seen:
                    kept_lines.append(line)
                continue

            if self._is_sqlish_line(stripped):
                kept_lines.append(line)
                sql_seen = True
                continue

            if sql_seen:
                break

            kept_lines.append(line)

        return "\n".join(kept_lines).strip()

    def _is_sqlish_line(self, stripped_line: str) -> bool:
        lower = stripped_line.lower()
        if lower.startswith(("here", "sure", "note", "the query", "question", "explanation", "let me", "i can")):
            return False

        if re.match(
            r"(?is)^(select|with|from|where|group\s+by|order\s+by|having|limit|offset|join|left\s+join|right\s+join|inner\s+join|outer\s+join|on|and|or|union|except|intersect|case|when|then|else|end|distinct|fetch|qualify|returning)\b",
            lower,
        ):
            return True

        if stripped_line.startswith((",", ")", "(")) or stripped_line.endswith(","):
            return True

        if re.fullmatch(r"[\w\.\(\)\s=><!+\-*/%,'\"`]+", stripped_line) and any(
            char.isalpha() for char in stripped_line
        ):
            return True

        return False

    def _looks_like_sql(self, text: str) -> bool:
        if not re.search(r"(?is)^\s*(with|select)\b", text):
            return False
        if not re.search(r"(?is)\bfrom\b", text):
            return False
        if re.search(r"(?is)\b(insert|update|delete|drop|alter|create|truncate|merge|grant|revoke)\b", text):
            return False
        return True
