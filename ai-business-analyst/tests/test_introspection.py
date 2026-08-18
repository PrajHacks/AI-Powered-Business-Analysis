from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

from sqlalchemy import Boolean, Column, Date, DateTime, Float, ForeignKey, Integer, MetaData, String, Table, create_engine, text

from app.db.connection import ConnectionManager
from app.db.introspection import SchemaInfo, SchemaIntrospector


def _create_sample_schema(sqlite_path: Path) -> str:
    engine = create_engine(f"sqlite+pysqlite:///{sqlite_path.resolve().as_posix()}")
    metadata = MetaData()

    customers = Table(
        "customers",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("name", String(50), nullable=False),
        Column("is_active", Boolean, nullable=False, default=True),
        Column("joined_on", Date, nullable=True),
    )

    orders = Table(
        "orders",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("customer_id", Integer, ForeignKey("customers.id"), nullable=False),
        Column("total_amount", Float, nullable=False),
        Column("placed_at", DateTime, nullable=False),
    )

    order_notes = Table(
        "order_notes",
        metadata,
        Column("note_id", Integer, primary_key=True),
        Column("order_id", Integer, ForeignKey("orders.id"), nullable=False),
        Column("note_text", String(200), nullable=True),
    )

    metadata.create_all(engine)

    with engine.begin() as connection:
        connection.execute(
            customers.insert(),
            [
                {"id": 1, "name": "Alice", "is_active": True, "joined_on": date(2026, 1, 1)},
                {"id": 2, "name": "Bob", "is_active": False, "joined_on": date(2026, 2, 1)},
            ],
        )
        connection.execute(
            orders.insert(),
            [
                {
                    "id": 10,
                    "customer_id": 1,
                    "total_amount": 12.5,
                    "placed_at": datetime(2026, 3, 1, 10, 0, 0),
                },
                {
                    "id": 11,
                    "customer_id": 1,
                    "total_amount": 20.0,
                    "placed_at": datetime(2026, 3, 2, 11, 30, 0),
                },
                {
                    "id": 12,
                    "customer_id": 2,
                    "total_amount": 7.25,
                    "placed_at": datetime(2026, 3, 3, 9, 15, 0),
                },
            ],
        )
        connection.execute(
            order_notes.insert(),
            [
                {"note_id": 100, "order_id": 10, "note_text": "First note"},
                {"note_id": 101, "order_id": 12, "note_text": None},
            ],
        )
        connection.exec_driver_sql(
            """
            CREATE VIEW customer_order_counts AS
            SELECT
                c.id AS customer_id,
                c.name AS customer_name,
                COUNT(o.id) AS order_count
            FROM customers AS c
            LEFT JOIN orders AS o ON o.customer_id = c.id
            GROUP BY c.id, c.name
            """
        )

    engine.dispose()
    return f"sqlite+pysqlite:///{sqlite_path.resolve().as_posix()}"


def test_schema_introspector_builds_schema_info(tmp_path: Path) -> None:
    sqlite_path = tmp_path / "schema.sqlite"
    connection_string = _create_sample_schema(sqlite_path)

    manager = ConnectionManager()
    connection_id = manager.register_connection(
        name="Schema DB",
        connection_string=connection_string,
    )
    introspector = SchemaIntrospector(manager, cache_ttl_seconds=600, row_count_sample_limit=100)

    schema_info = introspector.get_schema(connection_id)

    assert isinstance(schema_info, SchemaInfo)
    assert schema_info.connection_id == connection_id
    assert {table.name for table in schema_info.tables} == {
        "customers",
        "orders",
        "order_notes",
        "customer_order_counts",
    }

    customers = next(table for table in schema_info.tables if table.name == "customers")
    assert customers.kind == "table"
    assert customers.primary_key_columns == ["id"]
    assert customers.row_count == 2
    assert customers.row_count_is_estimate is False
    assert [column.type for column in customers.columns] == [
        "integer",
        "text",
        "boolean",
        "date",
    ]

    orders = next(table for table in schema_info.tables if table.name == "orders")
    assert orders.kind == "table"
    assert orders.primary_key_columns == ["id"]
    assert orders.row_count == 3
    assert any(fk.referred_table == "customers" for fk in orders.foreign_keys)
    assert any(column.primary_key for column in orders.columns)
    assert any(column.type == "datetime" for column in orders.columns)

    view_info = next(table for table in schema_info.tables if table.name == "customer_order_counts")
    assert view_info.kind == "view"
    assert view_info.primary_key_columns == []
    assert view_info.row_count == 2

    llm_context = schema_info.to_llm_context()
    assert llm_context.strip()
    for token in [
        "customers",
        "orders",
        "order_notes",
        "customer_order_counts",
        "customer_name",
        "total_amount",
        "note_text",
    ]:
        assert token in llm_context


def test_schema_introspector_uses_cache_and_invalidate(tmp_path: Path, monkeypatch) -> None:
    sqlite_path = tmp_path / "cache.sqlite"
    connection_string = _create_sample_schema(sqlite_path)

    manager = ConnectionManager()
    connection_id = manager.register_connection(
        name="Cache DB",
        connection_string=connection_string,
    )
    introspector = SchemaIntrospector(manager, cache_ttl_seconds=600, row_count_sample_limit=100)

    import app.db.introspection as introspection_module

    original_inspect = introspection_module.inspect
    call_count = {"value": 0}

    def spy_inspect(*args, **kwargs):
        call_count["value"] += 1
        return original_inspect(*args, **kwargs)

    monkeypatch.setattr(introspection_module, "inspect", spy_inspect)

    first = introspector.get_schema(connection_id)
    second = introspector.get_schema(connection_id)

    assert first == second
    assert call_count["value"] == 1

    introspector.invalidate(connection_id)
    third = introspector.get_schema(connection_id)

    assert third.connection_id == first.connection_id
    assert third.tables == first.tables
    assert call_count["value"] == 2


def test_schema_introspector_refresh_forces_fresh_introspection(tmp_path: Path, monkeypatch) -> None:
    sqlite_path = tmp_path / "refresh.sqlite"
    connection_string = _create_sample_schema(sqlite_path)

    manager = ConnectionManager()
    connection_id = manager.register_connection(
        name="Refresh DB",
        connection_string=connection_string,
    )
    introspector = SchemaIntrospector(manager, cache_ttl_seconds=600, row_count_sample_limit=100)

    import app.db.introspection as introspection_module

    original_inspect = introspection_module.inspect
    call_count = {"value": 0}

    def spy_inspect(*args, **kwargs):
        call_count["value"] += 1
        return original_inspect(*args, **kwargs)

    monkeypatch.setattr(introspection_module, "inspect", spy_inspect)

    introspector.get_schema(connection_id)
    introspector.refresh(connection_id)

    assert call_count["value"] == 2
