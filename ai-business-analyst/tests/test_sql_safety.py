from __future__ import annotations

import pytest

from app.db.sql_safety import SQLSafetyValidator


def test_sql_safety_accepts_plain_select() -> None:
    validator = SQLSafetyValidator()

    result = validator.validate("SELECT * FROM customers")

    assert result.is_safe is True
    assert result.reason is None
    assert result.normalized_sql == "SELECT * FROM customers"


def test_sql_safety_accepts_join_group_by_and_aggregate() -> None:
    validator = SQLSafetyValidator()

    result = validator.validate(
        """
        SELECT c.id, COUNT(o.id) AS order_count
        FROM customers c
        JOIN orders o ON o.customer_id = c.id
        GROUP BY c.id
        """
    )

    assert result.is_safe is True
    assert result.reason is None
    assert "JOIN" in result.normalized_sql
    assert "GROUP BY" in result.normalized_sql


def test_sql_safety_accepts_with_cte() -> None:
    validator = SQLSafetyValidator()

    result = validator.validate(
        """
        WITH recent_orders AS (
            SELECT id, customer_id FROM orders
        )
        SELECT * FROM recent_orders
        """
    )

    assert result.is_safe is True
    assert result.reason is None
    assert result.normalized_sql is not None
    assert result.normalized_sql.lstrip().upper().startswith("WITH")


@pytest.mark.parametrize(
    "sql",
    [
        "DROP TABLE customers",
        "DELETE FROM customers",
        "UPDATE customers SET name = 'A'",
        "INSERT INTO customers (id, name) VALUES (1, 'A')",
        "ALTER TABLE customers ADD COLUMN email TEXT",
        "CREATE TABLE scratch (id INTEGER)",
        "PRAGMA journal_mode = WAL",
        "TRUNCATE TABLE customers",
    ],
)
def test_sql_safety_rejects_mutating_or_ddl_statements(sql: str) -> None:
    validator = SQLSafetyValidator()

    result = validator.validate(sql)

    assert result.is_safe is False
    assert result.reason is not None
    assert result.normalized_sql is None


def test_sql_safety_rejects_stacked_statements() -> None:
    validator = SQLSafetyValidator()

    result = validator.validate("SELECT 1; DROP TABLE users")

    assert result.is_safe is False
    assert "single" in result.reason.lower()


def test_sql_safety_rejects_comment_based_obfuscation() -> None:
    validator = SQLSafetyValidator()

    result = validator.validate("SELECT 1 -- hide the next statement\nDROP TABLE users")

    assert result.is_safe is False
    assert result.reason is not None


def test_sql_safety_accepts_unusual_whitespace_and_casing() -> None:
    validator = SQLSafetyValidator()

    result = validator.validate("   sElEcT * from x   ")

    assert result.is_safe is True
    assert result.normalized_sql == "sElEcT * from x"


def test_sql_safety_rejects_non_aggregated_column_in_group_by() -> None:
    validator = SQLSafetyValidator()

    result = validator.validate(
        "SELECT total_profit, sales_channel FROM sales GROUP BY sales_channel"
    )

    assert result.is_safe is False
    assert result.reason is not None
    assert "total_profit" in result.reason
    assert "non-aggregated column" in result.reason
    assert "GROUP BY" in result.reason
    assert result.normalized_sql is None


def test_sql_safety_accepts_correctly_aggregated_group_by() -> None:
    validator = SQLSafetyValidator()

    result = validator.validate(
        "SELECT sales_channel, SUM(total_profit) FROM sales GROUP BY sales_channel"
    )

    assert result.is_safe is True
    assert result.reason is None
    assert result.normalized_sql is not None


def test_sql_safety_accepts_multiple_group_by_columns() -> None:
    validator = SQLSafetyValidator()

    result = validator.validate(
        "SELECT sales_channel, region, SUM(total_profit) FROM sales GROUP BY sales_channel, region"
    )

    assert result.is_safe is True
    assert result.reason is None
    assert result.normalized_sql is not None


def test_sql_safety_accepts_query_without_group_by() -> None:
    validator = SQLSafetyValidator()

    result = validator.validate("SELECT total_profit, sales_channel FROM sales")

    assert result.is_safe is True
    assert result.reason is None
    assert result.normalized_sql == "SELECT total_profit, sales_channel FROM sales"


def test_sql_safety_rejects_partially_unaggregated_select_with_group_by() -> None:
    validator = SQLSafetyValidator()

    result = validator.validate(
        """
        SELECT c.id, c.name, COUNT(o.id) AS order_count
        FROM customers c
        JOIN orders o ON o.customer_id = c.id
        GROUP BY c.id
        """
    )

    assert result.is_safe is False
    assert result.reason is not None
    assert "name" in result.reason
    assert "non-aggregated column" in result.reason
    assert result.normalized_sql is None


def test_sql_safety_rejects_group_by_column_omitted_from_select() -> None:
    validator = SQLSafetyValidator()

    result = validator.validate(
        "SELECT SUM(total_cost) AS total_expenses, SUM(total_revenue) AS total_revenue FROM sales GROUP BY item_type"
    )

    assert result.is_safe is False
    assert result.reason is not None
    assert "item_type" in result.reason
    assert "does not include it in the SELECT list" in result.reason
    assert result.normalized_sql is None


def test_sql_safety_accepts_group_by_column_present_in_select() -> None:
    validator = SQLSafetyValidator()

    result = validator.validate(
        "SELECT item_type, SUM(total_cost) AS total_expenses FROM sales GROUP BY item_type"
    )

    assert result.is_safe is True
    assert result.reason is None
    assert result.normalized_sql is not None


def test_sql_safety_accepts_multi_column_group_by_all_in_select() -> None:
    validator = SQLSafetyValidator()

    result = validator.validate(
        "SELECT region, item_type, SUM(total_cost) AS total_expenses FROM sales GROUP BY region, item_type"
    )

    assert result.is_safe is True
    assert result.reason is None
    assert result.normalized_sql is not None


def test_sql_safety_rejects_multi_column_group_by_partially_missing_from_select() -> None:
    validator = SQLSafetyValidator()

    result = validator.validate(
        "SELECT region, SUM(total_cost) AS total_expenses FROM sales GROUP BY region, item_type"
    )

    assert result.is_safe is False
    assert result.reason is not None
    assert "item_type" in result.reason
    assert "does not include it in the SELECT list" in result.reason
    assert result.normalized_sql is None

