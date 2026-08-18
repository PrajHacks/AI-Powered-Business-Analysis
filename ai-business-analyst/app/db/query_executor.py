from __future__ import annotations

import logging
import re
import threading
import time
from typing import Any

import sqlparse
from pydantic import BaseModel, ConfigDict
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from app.config import get_settings
from app.db.connection import ConnectionManager
from app.db.sql_safety import SQLSafetyValidator

logger = logging.getLogger(__name__)


class QueryExecutionError(RuntimeError):
    """Raised when a query cannot be executed successfully."""


class UnsafeQueryError(QueryExecutionError):
    """Raised when validation rejects a query before execution."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class QueryTimeoutError(QueryExecutionError):
    """Raised when query execution exceeds the configured timeout."""


class QueryResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    columns: list[str]
    rows: list[dict[str, Any]]
    row_count: int
    truncated: bool
    execution_time_ms: float


class QueryExecutor:
    def __init__(
        self,
        connection_manager: ConnectionManager,
        *,
        validator: SQLSafetyValidator | None = None,
        timeout_seconds: int | None = None,
        max_rows: int | None = None,
    ) -> None:
        settings = get_settings()
        self._connection_manager = connection_manager
        self._validator = validator or SQLSafetyValidator()
        self._timeout_seconds = timeout_seconds or settings.query_timeout_seconds
        self._max_rows = max_rows or settings.query_max_rows

    def execute(
        self,
        connection_id: str,
        sql: str,
        timeout_seconds: int | None = None,
        row_limit: int | None = None,
    ) -> QueryResult:
        validation = self._validator.validate(sql)
        if not validation.is_safe:
            raise UnsafeQueryError(validation.reason or "SQL was rejected.")

        normalized_sql = validation.normalized_sql or sql.strip()
        effective_timeout = timeout_seconds or self._timeout_seconds
        effective_row_limit = row_limit or self._max_rows

        engine = self._connection_manager.get_engine(connection_id)
        started_at = time.perf_counter()
        done_event = threading.Event()
        execution_state: dict[str, Any] = {
            "connection_ready": threading.Event(),
            "done_event": done_event,
            "dbapi_connection": None,
        }
        result_holder: dict[str, Any] = {}

        worker = threading.Thread(
            target=self._execute_in_worker,
            kwargs={
                "engine": engine,
                "sql": normalized_sql,
                "row_limit": effective_row_limit,
                "timeout_seconds": effective_timeout,
                "state": execution_state,
                "result_holder": result_holder,
            },
            daemon=True,
        )
        worker.start()
        completed = done_event.wait(timeout=effective_timeout)
        if not completed:
            self._interrupt_dbapi_connection(execution_state.get("dbapi_connection"))
            raise QueryTimeoutError(
                f"Query exceeded the timeout of {effective_timeout} seconds."
            )

        if "error" in result_holder:
            error = result_holder["error"]
            if isinstance(error, QueryExecutionError):
                raise error
            raise QueryExecutionError(str(error)) from None

        query_result = QueryResult(
            columns=result_holder["columns"],
            rows=result_holder["rows"],
            row_count=result_holder["row_count"],
            truncated=result_holder["truncated"],
            execution_time_ms=(time.perf_counter() - started_at) * 1000.0,
        )
        return query_result

    def _execute_in_worker(
        self,
        *,
        engine: Engine,
        sql: str,
        row_limit: int,
        timeout_seconds: int,
        state: dict[str, Any],
        result_holder: dict[str, Any],
    ) -> None:
        done_event = state["done_event"]
        try:
            executable_sql, applied_cap = self._apply_row_limit(sql, row_limit)
            with engine.connect() as connection:
                raw_connection = getattr(connection.connection, "driver_connection", None)
                if raw_connection is None:
                    raw_connection = connection.connection
                state["dbapi_connection"] = raw_connection
                state["connection_ready"].set()

                self._enforce_read_only(connection, engine)

                execution = None
                try:
                    if engine.dialect.name == "postgresql":
                        with connection.begin():
                            connection.exec_driver_sql(
                                f"SET LOCAL statement_timeout = {int(timeout_seconds * 1000)}"
                            )
                            execution = connection.exec_driver_sql(executable_sql)
                            columns = list(execution.keys())
                            rows = [dict(row) for row in execution.mappings().all()]
                    else:
                        execution = connection.exec_driver_sql(executable_sql)
                        columns = list(execution.keys())
                        rows = [dict(row) for row in execution.mappings().all()]
                finally:
                    if execution is not None:
                        try:
                            execution.close()
                        except Exception:
                            pass

            truncated = False
            if applied_cap and len(rows) > row_limit:
                truncated = True
                rows = rows[:row_limit]

            result_holder.update(
                {
                    "columns": columns,
                    "rows": rows,
                    "row_count": len(rows),
                    "truncated": truncated,
                }
            )
        except SQLAlchemyError as exc:
            result_holder["error"] = self._wrap_execution_error(exc)
        except QueryExecutionError as exc:
            result_holder["error"] = exc
        except Exception as exc:
            result_holder["error"] = QueryExecutionError(str(exc))
        finally:
            done_event.set()

    def _apply_row_limit(self, sql: str, row_limit: int) -> tuple[str, bool]:
        normalized_sql = sql.strip().rstrip(";").strip()
        if not normalized_sql:
            return normalized_sql, False

        parsed = sqlparse.parse(normalized_sql)
        if not parsed:
            return normalized_sql, False

        statement = parsed[0]
        limit_info = self._find_top_level_limit(statement.tokens)
        if limit_info is None:
            return f"{normalized_sql} LIMIT {row_limit + 1}", True

        limit_value_index, existing_limit = limit_info
        if existing_limit is not None and existing_limit <= row_limit:
            return normalized_sql, False

        if limit_value_index is None:
            return f"SELECT * FROM ({normalized_sql}) AS _ai_business_analyst_query LIMIT {row_limit + 1}", True

        rewritten_sql = self._rewrite_token_value(
            statement.tokens,
            limit_value_index,
            str(row_limit + 1),
        )
        return rewritten_sql, True

    def _find_top_level_limit(
        self,
        tokens: list[sqlparse.sql.Token],
    ) -> tuple[int | None, int | None] | None:
        for index, token in enumerate(tokens):
            if self._is_ignorable_token(token):
                continue
            if self._token_keyword(token) != "LIMIT":
                continue
            value_index = self._next_meaningful_index(tokens, index + 1)
            if value_index is None:
                return None, None
            value_token = tokens[value_index]
            try:
                return value_index, int(str(value_token.value).strip())
            except ValueError:
                return value_index, None
        return None

    def _next_meaningful_index(
        self,
        tokens: list[sqlparse.sql.Token],
        start_index: int,
    ) -> int | None:
        for index in range(start_index, len(tokens)):
            if not self._is_ignorable_token(tokens[index]):
                return index
        return None

    def _rewrite_token_value(
        self,
        tokens: list[sqlparse.sql.Token],
        token_index: int,
        replacement: str,
    ) -> str:
        pieces: list[str] = []
        for index, token in enumerate(tokens):
            if index == token_index:
                pieces.append(replacement)
            else:
                pieces.append(token.value)
        return "".join(pieces).strip()

    def _enforce_read_only(self, connection, engine: Engine) -> None:
        if engine.dialect.name == "sqlite":
            connection.exec_driver_sql("PRAGMA query_only = ON")
            return

        # Production deployments should still use a read-only database role/user
        # for defense in depth on PostgreSQL and MySQL.

    def _interrupt_dbapi_connection(self, dbapi_connection: Any) -> None:
        if dbapi_connection is None:
            return
        interrupt = getattr(dbapi_connection, "interrupt", None)
        if callable(interrupt):
            try:
                interrupt()
            except Exception:
                logger.debug("Unable to interrupt timed-out database connection.", exc_info=True)

    def _wrap_execution_error(self, exc: SQLAlchemyError) -> QueryExecutionError:
        message = self._sanitize_error_message(str(exc).strip())
        if not message:
            message = "Query execution failed."
        return QueryExecutionError(message)

    def _sanitize_error_message(self, message: str) -> str:
        redacted = re.sub(
            r"(?i)(://)([^:@/\s]+)(:[^@/\s]+)?@",
            r"\1***:***@",
            message,
        )
        return redacted

    def _is_ignorable_token(self, token: sqlparse.sql.Token) -> bool:
        return token.is_whitespace or token.ttype is not None and token.ttype in sqlparse.tokens.Comment

    def _token_keyword(self, token: sqlparse.sql.Token) -> str:
        normalized = getattr(token, "normalized", None)
        if isinstance(normalized, str):
            return normalized.upper()
        return str(token.value).strip().upper()
