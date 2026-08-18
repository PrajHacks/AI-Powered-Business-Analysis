from __future__ import annotations

import logging
import re
from typing import Iterable

import sqlparse
from pydantic import BaseModel, ConfigDict
from sqlparse import tokens as T
from sqlparse.sql import Function, Identifier, IdentifierList, Parenthesis, Statement

logger = logging.getLogger(__name__)


class ValidationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    is_safe: bool
    reason: str | None = None
    normalized_sql: str | None = None


class SQLSafetyValidator:
    _allowed_start_keywords = {"SELECT", "WITH"}
    _forbidden_keywords = {
        "ATTACH",
        "ALTER",
        "BEGIN",
        "CALL",
        "COMMIT",
        "CREATE",
        "DELETE",
        "DETACH",
        "DROP",
        "EXEC",
        "EXECUTE",
        "GRANT",
        "INSERT",
        "INTO",
        "MERGE",
        "PRAGMA",
        "REINDEX",
        "REPLACE",
        "REVOKE",
        "ROLLBACK",
        "TRUNCATE",
        "UPDATE",
        "VACUUM",
        "VALUES",
    }
    _statement_start_keywords = _allowed_start_keywords | _forbidden_keywords
    _ignorable_comment_followups = {
        "SELECT",
        "WITH",
        "ATTACH",
        "ALTER",
        "BEGIN",
        "CALL",
        "COMMIT",
        "CREATE",
        "DELETE",
        "DETACH",
        "DROP",
        "EXEC",
        "EXECUTE",
        "GRANT",
        "INSERT",
        "INTO",
        "MERGE",
        "PRAGMA",
        "REINDEX",
        "REPLACE",
        "REVOKE",
        "ROLLBACK",
        "TRUNCATE",
        "UPDATE",
        "VACUUM",
        "VALUES",
    }
    _aggregate_functions = {
        "SUM",
        "AVG",
        "COUNT",
        "MIN",
        "MAX",
        "TOTAL",
        "GROUP_CONCAT",
        "STRING_AGG",
        "ARRAY_AGG",
        "STDDEV",
        "VARIANCE",
        "MEDIAN",
    }

    def validate(self, sql: str) -> ValidationResult:
        raw_sql = sql.strip()
        if not raw_sql:
            return self._reject(sql, "SQL is empty.")

        statements = [statement.strip() for statement in sqlparse.split(sql) if statement.strip()]
        if len(statements) != 1:
            return self._reject(sql, "Only a single SELECT statement is allowed.")

        statement_text = statements[0]
        parsed_statements = sqlparse.parse(statement_text)
        if len(parsed_statements) != 1:
            return self._reject(sql, "SQL must parse as a single statement.")

        statement = parsed_statements[0]
        first_token = self._first_meaningful_top_level_token(statement.tokens)
        if first_token is None:
            return self._reject(sql, "SQL is empty.")

        first_keyword = self._token_keyword(first_token)
        if first_keyword not in self._allowed_start_keywords:
            return self._reject(sql, "Only a single SELECT statement is allowed.")

        if self._contains_suspicious_comment_followup(statement.tokens):
            return self._reject(sql, "Suspicious comment-based statement chaining was detected.")

        forbidden_keyword = self._find_forbidden_keyword(statement)
        if forbidden_keyword is not None:
            return self._reject(sql, f"Disallowed SQL keyword '{forbidden_keyword}'.")

        if first_keyword == "WITH" and not self._has_top_level_select(statement.tokens):
            return self._reject(sql, "CTE statements must ultimately resolve to a SELECT query.")

        group_by_error = self._validate_group_by(statement)
        if group_by_error is not None:
            return self._reject(sql, group_by_error)

        normalized_sql = sqlparse.format(
            statement_text,
            strip_comments=True,
            reindent=False,
        ).strip().rstrip(";").strip()
        if not normalized_sql:
            return self._reject(sql, "SQL is empty.")

        return ValidationResult(
            is_safe=True,
            reason=None,
            normalized_sql=normalized_sql,
        )

    def _reject(
        self,
        sql: str,
        reason: str,
    ) -> ValidationResult:
        logger.warning("Rejected SQL query: %s | reason=%s", sql.strip(), reason)
        return ValidationResult(is_safe=False, reason=reason, normalized_sql=None)

    def _find_forbidden_keyword(self, statement: sqlparse.sql.Statement) -> str | None:
        for token in statement.flatten():
            if self._is_ignorable_token(token):
                continue
            keyword = self._token_keyword(token)
            if keyword in self._forbidden_keywords:
                return keyword
            if token.ttype is not None and (
                token.ttype in T.Keyword.DML or token.ttype in T.Keyword.DDL
            ) and keyword not in self._allowed_start_keywords:
                return keyword
        return None

    def _contains_suspicious_comment_followup(
        self,
        tokens: Iterable[sqlparse.sql.Token],
    ) -> bool:
        token_list = list(tokens)
        for index, token in enumerate(token_list):
            if not self._is_comment_token(token):
                continue
            next_token = self._next_meaningful_token(token_list, index + 1)
            if next_token is None:
                continue
            if self._token_keyword(next_token) in self._ignorable_comment_followups:
                return True
        return False

    def _has_top_level_select(self, tokens: Iterable[sqlparse.sql.Token]) -> bool:
        for token in tokens:
            if self._is_ignorable_token(token):
                continue
            if self._token_keyword(token) == "SELECT":
                return True
        return False

    def _first_meaningful_top_level_token(
        self,
        tokens: Iterable[sqlparse.sql.Token],
    ) -> sqlparse.sql.Token | None:
        for token in tokens:
            if not self._is_ignorable_token(token):
                return token
        return None

    def _next_meaningful_token(
        self,
        tokens: list[sqlparse.sql.Token],
        start_index: int,
    ) -> sqlparse.sql.Token | None:
        for token in tokens[start_index:]:
            if not self._is_ignorable_token(token):
                return token
        return None

    def _is_ignorable_token(self, token: sqlparse.sql.Token) -> bool:
        return token.is_whitespace or self._is_comment_token(token) or token.ttype is T.Punctuation

    def _is_comment_token(self, token: sqlparse.sql.Token) -> bool:
        return token.ttype is not None and token.ttype in T.Comment

    def _token_keyword(self, token: sqlparse.sql.Token) -> str:
        normalized = getattr(token, "normalized", None)
        if isinstance(normalized, str):
            return normalized.upper()
        return str(token.value).strip().upper()

    def _validate_group_by(self, statement: sqlparse.sql.Statement) -> str | None:
        return self._check_group_by_recursively(statement)

    def _check_group_by_recursively(self, token: sqlparse.sql.Token) -> str | None:
        if isinstance(token, (Statement, Parenthesis)):
            err = self._check_group_by_in_statement(list(token.tokens))
            if err is not None:
                return err
        if hasattr(token, "tokens"):
            for child in token.tokens:
                err = self._check_group_by_recursively(child)
                if err is not None:
                    return err
        return None

    def _check_group_by_in_statement(self, tokens: list[sqlparse.sql.Token]) -> str | None:
        group_by_idx = None
        for idx, t in enumerate(tokens):
            if not t.is_whitespace and self._token_keyword(t) == "GROUP BY":
                group_by_idx = idx
                break

        if group_by_idx is None:
            return None

        select_items = self._extract_select_items(tokens)
        gb_items = self._extract_group_by_items(tokens, group_by_idx)
        gb_keys, gb_positions = self._get_group_by_keys(gb_items)

        for pos_idx, item in enumerate(select_items, start=1):
            if pos_idx in gb_positions:
                continue
            if self._is_aggregate_expression(item):
                continue
            if self._is_literal_or_constant(item):
                continue

            if item.ttype is T.Wildcard or item.value.strip() == "*":
                return (
                    "Query selects wildcard '*' with GROUP BY without aggregate function - "
                    "results would be arbitrary. Rewrite using an aggregate function."
                )

            item_keys = self._get_item_keys(item)
            if not (item_keys & gb_keys):
                real_name = item.get_real_name() if isinstance(item, Identifier) else None
                col_name = real_name if real_name else item.value.strip()
                col_name = re.sub(r"^[`\"\[]|[`\"\]]$", "", col_name)
                return (
                    f"Query selects non-aggregated column '{col_name}' with GROUP BY - "
                    "results would be arbitrary. Rewrite using an aggregate function."
                )

        all_select_keys: set[str] = set()
        for s_item in select_items:
            all_select_keys.update(self._get_item_keys(s_item))

        for gb_item in gb_items:
            if gb_item.ttype in (T.Literal.Number.Integer,):
                try:
                    pos = int(gb_item.value.strip())
                    if 1 <= pos <= len(select_items):
                        continue
                except ValueError:
                    pass

            gb_item_keys = self._get_item_keys(gb_item)
            if not (gb_item_keys & all_select_keys):
                real_name = gb_item.get_real_name() if isinstance(gb_item, Identifier) else None
                col_name = real_name if real_name else gb_item.value.strip()
                col_name = re.sub(r"^[`\"\[]|[`\"\]]$", "", col_name)
                return (
                    f"Query groups by '{col_name}' but does not include it in the SELECT list - "
                    f"results cannot be identified. Add '{col_name}' to the SELECT list."
                )

        return None

    def _extract_select_items(
        self,
        tokens: list[sqlparse.sql.Token],
    ) -> list[sqlparse.sql.Token]:
        select_tokens: list[sqlparse.sql.Token] = []
        in_select = False
        for t in tokens:
            if t.is_whitespace:
                continue
            kw = self._token_keyword(t)
            if kw == "SELECT":
                in_select = True
                continue
            if kw == "DISTINCT" and in_select:
                continue
            if kw in (
                "FROM",
                "WHERE",
                "GROUP BY",
                "HAVING",
                "ORDER BY",
                "LIMIT",
                "WINDOW",
                "UNION",
                "EXCEPT",
                "INTERSECT",
            ):
                in_select = False
                continue
            if in_select:
                select_tokens.append(t)

        items: list[sqlparse.sql.Token] = []
        for token in select_tokens:
            if isinstance(token, IdentifierList):
                for ident in token.get_identifiers():
                    items.append(ident)
            elif token.ttype is not T.Punctuation or token.value == "*":
                items.append(token)
        return items

    def _extract_group_by_items(
        self,
        tokens: list[sqlparse.sql.Token],
        group_by_idx: int,
    ) -> list[sqlparse.sql.Token]:
        gb_tokens: list[sqlparse.sql.Token] = []
        for t in tokens[group_by_idx + 1:]:
            if t.is_whitespace:
                continue
            kw = self._token_keyword(t)
            if kw in (
                "HAVING",
                "ORDER BY",
                "LIMIT",
                "OFFSET",
                "FETCH",
                "WINDOW",
                "UNION",
                "EXCEPT",
                "INTERSECT",
                ";",
            ):
                break
            if t.ttype is T.Punctuation and t.value == ";":
                break
            gb_tokens.append(t)

        items: list[sqlparse.sql.Token] = []
        for token in gb_tokens:
            if isinstance(token, IdentifierList):
                for ident in token.get_identifiers():
                    items.append(ident)
            elif token.ttype is not T.Punctuation:
                items.append(token)
        return items

    def _get_group_by_keys(
        self,
        gb_items: list[sqlparse.sql.Token],
    ) -> tuple[set[str], set[int]]:
        keys: set[str] = set()
        positions: set[int] = set()
        for item in gb_items:
            raw_val = item.value.strip()
            keys.add(raw_val.lower())
            keys.add(self._normalize_expr(raw_val))

            if item.ttype in (T.Literal.Number.Integer,):
                try:
                    positions.add(int(item.value.strip()))
                except ValueError:
                    pass
            elif isinstance(item, Identifier):
                real_name = item.get_real_name()
                if real_name:
                    keys.add(real_name.lower())
                    keys.add(self._normalize_expr(real_name))
                parent_name = item.get_parent_name()
                if parent_name and real_name:
                    keys.add(f"{parent_name.lower()}.{real_name.lower()}")
                    keys.add(self._normalize_expr(f"{parent_name}.{real_name}"))
                alias = item.get_alias()
                if alias:
                    keys.add(alias.lower())
                    keys.add(self._normalize_expr(alias))
        return keys, positions

    def _get_item_keys(self, item: sqlparse.sql.Token) -> set[str]:
        raw_val = item.value.strip()
        keys = {raw_val.lower(), self._normalize_expr(raw_val)}

        if isinstance(item, Identifier):
            real_name = item.get_real_name()
            if real_name:
                keys.add(real_name.lower())
                keys.add(self._normalize_expr(real_name))
            parent_name = item.get_parent_name()
            if parent_name and real_name:
                keys.add(f"{parent_name.lower()}.{real_name.lower()}")
                keys.add(self._normalize_expr(f"{parent_name}.{real_name}"))
            alias = item.get_alias()
            if alias:
                keys.add(alias.lower())
                keys.add(self._normalize_expr(alias))

            sub_tokens_without_alias: list[str] = []
            for sub in item.tokens:
                if sub.is_whitespace:
                    continue
                if getattr(sub, "normalized", sub.value).upper() == "AS":
                    break
                if alias and sub.value.strip() == alias:
                    break
                sub_tokens_without_alias.append(sub.value)
            if sub_tokens_without_alias:
                expr_str = "".join(sub_tokens_without_alias).strip()
                keys.add(expr_str.lower())
                keys.add(self._normalize_expr(expr_str))

        return keys

    def _normalize_expr(self, expr: str) -> str:
        cleaned = re.sub(r'[`"\[\]]', "", expr)
        return re.sub(r"\s+", "", cleaned).lower()

    def _is_aggregate_expression(self, token: sqlparse.sql.Token) -> bool:
        if isinstance(token, Function):
            fn_name = token.get_real_name() or token.get_name()
            if fn_name and fn_name.upper() in self._aggregate_functions:
                return True
            first = [
                t
                for t in token.tokens
                if not t.is_whitespace and t.ttype is not T.Punctuation
            ]
            if first and self._token_keyword(first[0]) in self._aggregate_functions:
                return True
        if hasattr(token, "tokens"):
            return any(self._is_aggregate_expression(child) for child in token.tokens)
        return False

    def _is_literal_or_constant(self, token: sqlparse.sql.Token) -> bool:
        if token.ttype in (
            T.Literal.Number.Integer,
            T.Literal.Number.Float,
            T.Literal.String.Single,
            T.Literal.String.Symbol,
            T.Literal.String,
            T.Keyword.Null,
        ):
            return True
        val_upper = token.value.strip().upper()
        if val_upper in (
            "NULL",
            "TRUE",
            "FALSE",
            "CURRENT_DATE",
            "CURRENT_TIME",
            "CURRENT_TIMESTAMP",
        ):
            return True
        return False
