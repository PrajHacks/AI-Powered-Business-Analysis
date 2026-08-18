from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.sql.sqltypes import (
    BIGINT,
    BINARY,
    BOOLEAN,
    DATE,
    DATETIME,
    DECIMAL,
    FLOAT,
    INTEGER,
    JSON,
    REAL,
    SMALLINT,
    TIMESTAMP,
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Enum,
    Float,
    Integer,
    LargeBinary,
    Numeric,
    SmallInteger,
    String,
    Text,
    Unicode,
    UnicodeText,
)

from app.db.connection import ConnectionManager, ConnectionNotFoundError


class SchemaIntrospectionError(RuntimeError):
    """Raised when schema introspection fails."""


class ColumnInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    type: Literal["text", "integer", "float", "boolean", "date", "datetime", "unknown"]
    nullable: bool
    primary_key: bool = False


class ForeignKeyInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    columns: list[str] = Field(default_factory=list)
    referred_table: str
    referred_columns: list[str] = Field(default_factory=list)
    referred_schema: str | None = None


class TableInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    kind: Literal["table", "view"]
    columns: list[ColumnInfo] = Field(default_factory=list)
    primary_key_columns: list[str] = Field(default_factory=list)
    foreign_keys: list[ForeignKeyInfo] = Field(default_factory=list)
    row_count: int | None = None
    row_count_is_estimate: bool = False


class SchemaInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    connection_id: str
    generated_at: datetime
    tables: list[TableInfo] = Field(default_factory=list)

    def to_llm_context(self) -> str:
        if not self.tables:
            return "No tables or views found."

        lines: list[str] = []
        for table in self.tables:
            row_label = ""
            if table.row_count is not None:
                row_label = f" rows{'~' if table.row_count_is_estimate else '='}{table.row_count}"

            lines.append(f"{table.kind} {table.name}{row_label}")

            fk_map: dict[str, list[str]] = {}
            for fk in table.foreign_keys:
                fk_label = f"{fk.referred_table}"
                if fk.referred_columns:
                    fk_label = f"{fk_label}.{','.join(fk.referred_columns)}"
                for column_name in fk.columns:
                    fk_map.setdefault(column_name, []).append(fk_label)

            for column in table.columns:
                parts = [column.name, column.type]
                if column.primary_key:
                    parts.append("pk")
                if not column.nullable:
                    parts.append("notnull")
                if column.name in fk_map:
                    parts.append("fk->" + "|".join(fk_map[column.name]))
                lines.append("  " + " ".join(parts))

            for fk in table.foreign_keys:
                columns = ",".join(fk.columns) or "?"
                referred = fk.referred_table
                if fk.referred_columns:
                    referred = f"{referred}({','.join(fk.referred_columns)})"
                lines.append(f"  fk {columns}->{referred}")

        return "\n".join(lines)


@dataclass(slots=True)
class _SchemaCacheEntry:
    schema_info: SchemaInfo
    cached_at: datetime


def _is_internal_object_name(name: str) -> bool:
    lowered = name.lower()
    return lowered.startswith("sqlite_") or lowered.startswith("sqlalchemy_")


def _normalize_sqlalchemy_type(type_obj: object | None) -> str:
    if type_obj is None:
        return "unknown"

    if isinstance(type_obj, (Boolean, BOOLEAN)):
        return "boolean"
    if isinstance(type_obj, (Date, DATE)):
        return "date"
    if isinstance(type_obj, (DateTime, DATETIME, TIMESTAMP)):
        return "datetime"
    if isinstance(type_obj, (Integer, SMALLINT, BIGINT, SmallInteger, BigInteger, INTEGER)):
        return "integer"
    if isinstance(type_obj, (Float, REAL, FLOAT, Numeric, DECIMAL)):
        return "float"
    if isinstance(type_obj, (String, Text, Unicode, UnicodeText, Enum, JSON)):
        return "text"
    if isinstance(type_obj, (LargeBinary, BINARY)):
        return "unknown"

    type_name = type(type_obj).__name__.lower()
    if "bool" in type_name:
        return "boolean"
    if "date" in type_name and "time" not in type_name:
        return "date"
    if "time" in type_name or "timestamp" in type_name:
        return "datetime"
    if any(token in type_name for token in ("int", "serial")):
        return "integer"
    if any(token in type_name for token in ("float", "real", "double", "numeric", "decimal")):
        return "float"
    if any(token in type_name for token in ("char", "text", "clob", "string", "enum", "json", "uuid")):
        return "text"
    return "unknown"


class SchemaIntrospector:
    def __init__(
        self,
        connection_manager: ConnectionManager,
        *,
        cache_ttl_seconds: int = 600,
        row_count_sample_limit: int = 1_000,
    ) -> None:
        self._connection_manager = connection_manager
        self._cache_ttl = timedelta(seconds=cache_ttl_seconds)
        self._row_count_sample_limit = row_count_sample_limit
        self._cache: dict[str, _SchemaCacheEntry] = {}

    def invalidate(self, connection_id: str) -> None:
        self._cache.pop(connection_id, None)

    def refresh(self, connection_id: str) -> SchemaInfo:
        self.invalidate(connection_id)
        return self.get_schema(connection_id, refresh=True)

    def get_schema(self, connection_id: str, *, refresh: bool = False) -> SchemaInfo:
        if not refresh:
            cached = self._cache.get(connection_id)
            if cached is not None and not self._is_expired(cached.cached_at):
                return cached.schema_info
            if cached is not None and self._is_expired(cached.cached_at):
                self.invalidate(connection_id)

        schema_info = self._introspect(connection_id)
        self._cache[connection_id] = _SchemaCacheEntry(
            schema_info=schema_info,
            cached_at=datetime.now(timezone.utc),
        )
        return schema_info

    def _is_expired(self, cached_at: datetime) -> bool:
        return datetime.now(timezone.utc) - cached_at >= self._cache_ttl

    def _introspect(self, connection_id: str) -> SchemaInfo:
        engine = self._connection_manager.get_engine(connection_id)
        inspector = inspect(engine)

        table_names = [
            name
            for name in inspector.get_table_names()
            if not _is_internal_object_name(name)
        ]
        view_names = [
            name
            for name in inspector.get_view_names()
            if not _is_internal_object_name(name)
        ]

        tables: list[TableInfo] = []
        for name in table_names:
            tables.append(self._build_table_info(inspector, engine, name, kind="table"))
        for name in view_names:
            tables.append(self._build_table_info(inspector, engine, name, kind="view"))

        return SchemaInfo(
            connection_id=connection_id,
            generated_at=datetime.now(timezone.utc),
            tables=tables,
        )

    def _build_table_info(self, inspector, engine: Engine, name: str, *, kind: str) -> TableInfo:
        try:
            raw_columns = inspector.get_columns(name)
        except SQLAlchemyError as exc:
            raise SchemaIntrospectionError(
                f"Unable to inspect columns for {kind} '{name}'."
            ) from None

        try:
            pk_constraint = inspector.get_pk_constraint(name) or {}
        except SQLAlchemyError:
            pk_constraint = {}
        try:
            foreign_key_rows = inspector.get_foreign_keys(name) or []
        except SQLAlchemyError:
            foreign_key_rows = []

        primary_key_columns = list(pk_constraint.get("constrained_columns") or [])
        columns = [
            ColumnInfo(
                name=column["name"],
                type=_normalize_sqlalchemy_type(column.get("type")),
                nullable=bool(column.get("nullable", True)),
                primary_key=column["name"] in primary_key_columns,
            )
            for column in raw_columns
        ]

        foreign_keys = [
            ForeignKeyInfo(
                name=fk.get("name"),
                columns=list(fk.get("constrained_columns") or []),
                referred_table=fk.get("referred_table") or "",
                referred_columns=list(fk.get("referred_columns") or []),
                referred_schema=fk.get("referred_schema"),
            )
            for fk in foreign_key_rows
        ]

        row_count, row_count_is_estimate = self._count_rows(engine, name)
        return TableInfo(
            name=name,
            kind=kind,  # type: ignore[arg-type]
            columns=columns,
            primary_key_columns=primary_key_columns,
            foreign_keys=foreign_keys,
            row_count=row_count,
            row_count_is_estimate=row_count_is_estimate,
        )

    def _count_rows(self, engine: Engine, table_name: str) -> tuple[int | None, bool]:
        quoted_table = engine.dialect.identifier_preparer.quote(table_name)
        query = text(
            f"SELECT COUNT(*) FROM (SELECT 1 FROM {quoted_table} LIMIT :row_limit)"
        )
        try:
            with engine.connect() as connection:
                result = connection.execute(
                    query, {"row_limit": self._row_count_sample_limit + 1}
                ).scalar_one()
        except SQLAlchemyError:
            return None, False

        if result is None:
            return None, False

        row_count = int(result)
        if row_count > self._row_count_sample_limit:
            return self._row_count_sample_limit, True
        return row_count, False

