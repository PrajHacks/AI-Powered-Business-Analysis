from __future__ import annotations

import io
import logging
import re
import warnings
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, TextIO
from uuid import uuid4

import pandas as pd
from pandas.errors import EmptyDataError
from sqlalchemy import DATE, DATETIME, INTEGER, REAL, Text, create_engine

from app.db.connection import cleanup_sqlite_artifacts

logger = logging.getLogger(__name__)

# Threshold: fraction of non-null values that must parse as dates for a
# column to be treated as a date column.  Mirrors the threshold used in
# app/charting/chart_selector.py so the two subsystems stay in sync.
_DATE_PARSE_THRESHOLD = 0.90


class CSVLoadError(RuntimeError):
    """Base exception for CSV loading failures."""


class CSVSourceNotFoundError(CSVLoadError):
    """Raised when the input CSV path does not exist."""


class EmptyCSVError(CSVLoadError):
    """Raised when the CSV has no data rows or no columns."""


class CSVSchemaError(CSVLoadError):
    """Raised when the CSV cannot be mapped to a usable schema."""


@dataclass(slots=True)
class CSVColumnInfo:
    original_name: str
    sanitized_name: str
    sqlite_type: str


@dataclass(slots=True)
class CSVLoadResult:
    connection_id: str
    sqlite_path: Path
    connection_string: str
    table_name: str
    row_count: int
    columns: tuple[CSVColumnInfo, ...]


_IDENTIFIER_RE = re.compile(r"[^0-9a-zA-Z_]+")
_CHUNK_SIZE = 50_000


def _sanitize_identifier(value: str, fallback: str = "column") -> str:
    cleaned = value.strip().lower()
    cleaned = _IDENTIFIER_RE.sub("_", cleaned)
    cleaned = re.sub(r"_+", "_", cleaned).strip("_")
    if not cleaned:
        cleaned = fallback
    if cleaned[0].isdigit():
        cleaned = f"{fallback}_{cleaned}"
    return cleaned


def _unique_sanitized_names(names: list[str]) -> list[str]:
    counts: defaultdict[str, int] = defaultdict(int)
    used_names: set[str] = set()
    sanitized: list[str] = []

    for original_name in names:
        base_name = _sanitize_identifier(original_name)
        counts[base_name] += 1
        candidate = base_name if counts[base_name] == 1 else f"{base_name}_{counts[base_name]}"

        while candidate in used_names:
            counts[base_name] += 1
            candidate = f"{base_name}_{counts[base_name]}"

        used_names.add(candidate)
        sanitized.append(candidate)

    return sanitized


def _sqlite_type_for_dtype(dtype: object) -> str:
    if pd.api.types.is_bool_dtype(dtype):
        return "INTEGER"
    if pd.api.types.is_integer_dtype(dtype):
        return "INTEGER"
    if pd.api.types.is_float_dtype(dtype):
        return "REAL"
    return "TEXT"


def _sqlite_type_for_sqlalchemy(dtype_name: str):
    if dtype_name == "INTEGER":
        return INTEGER()
    if dtype_name == "REAL":
        return REAL()
    if dtype_name == "DATE":
        return DATE()
    if dtype_name == "DATETIME":
        return DATETIME()
    return Text()


def _source_name(csv_source: str | Path | BinaryIO | TextIO, fallback: str) -> str:
    if isinstance(csv_source, (str, Path)):
        return Path(csv_source).stem or fallback

    source_name = getattr(csv_source, "name", "")
    if source_name:
        return Path(source_name).stem or fallback
    return fallback


def _copy_stream_to_path(source: BinaryIO | TextIO, destination: Path) -> None:
    if hasattr(source, "seek"):
        try:
            source.seek(0)
        except (OSError, io.UnsupportedOperation, AttributeError):
            pass

    with destination.open("wb") as target:
        while True:
            chunk = source.read(1024 * 1024)
            if not chunk:
                break
            if isinstance(chunk, str):
                chunk = chunk.encode("utf-8")
            target.write(chunk)


def _materialize_source(
    csv_source: str | Path | BinaryIO | TextIO,
    scratch_dir: Path,
    connection_id: str,
) -> tuple[Path, Path | None]:
    if isinstance(csv_source, (str, Path)):
        source_path = Path(csv_source)
        if not source_path.exists():
            raise CSVSourceNotFoundError(f"CSV file not found: {source_path}")
        if not source_path.is_file():
            raise CSVLoadError(f"CSV source is not a file: {source_path}")
        return source_path, None

    staged_source_path = scratch_dir / f"{connection_id}.source.csv"
    _copy_stream_to_path(csv_source, staged_source_path)
    return staged_source_path, staged_source_path


def _detect_date_column(series: pd.Series) -> str | None:
    """Return 'DATE', 'DATETIME', or None for a TEXT/object pandas Series.

    Detection logic (mirrors chart_selector.py's text-date detection):
    1. Skip columns where the pandas dtype is not object (strings already
       handled by _sqlite_type_for_dtype for numeric types).
    2. Attempt pd.to_datetime() with errors='coerce' on the non-null values.
    3. If >= _DATE_PARSE_THRESHOLD of non-null values parse successfully,
       the column is a date column.
    4. Distinguish DATE vs DATETIME by whether any parsed value has a
       non-zero time component.

    NOTE ON AMBIGUITY: We use dayfirst=False (US-style M/D/YYYY).  This is
    a heuristic that cannot be correct for all datasets — "3/4/2020" could
    be March 4 (US) or April 3 (ISO/European).  A production system should
    ask the user to confirm the date format rather than guessing.
    """
    if not pd.api.types.is_object_dtype(series):
        return None  # numeric/bool columns are already classified

    non_null = series.dropna()
    if non_null.empty:
        return None

    # Suppress the pandas UserWarning about format inference for mixed formats.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        parsed = pd.to_datetime(non_null, errors="coerce", dayfirst=False, format="mixed")

    valid = parsed.dropna()
    if len(non_null) == 0 or (len(valid) / len(non_null)) < _DATE_PARSE_THRESHOLD:
        return None

    has_time = ((valid.dt.hour != 0) | (valid.dt.minute != 0) | (valid.dt.second != 0)).any()
    return "DATETIME" if has_time else "DATE"


def _infer_schema(
    source_path: Path,
) -> tuple[list[str], list[str], list[CSVColumnInfo], int, set[str]]:
    """Return (original_columns, sanitized_columns, column_info, row_count, date_columns).

    date_columns is the set of *original* column names classified as DATE or DATETIME.
    We need a full-file scan to reliably detect date columns before deciding
    the SQLite schema, so this function buffers all TEXT-typed column values
    for the date-detection pass.
    """
    original_columns: list[str] | None = None
    inferred_types: dict[str, str] = {}
    row_count = 0
    # Accumulate values for TEXT columns to run date detection after the full scan.
    text_column_samples: dict[str, list[pd.Series]] = defaultdict(list)

    try:
        reader = pd.read_csv(source_path, chunksize=_CHUNK_SIZE, encoding="utf-8-sig")
        for chunk in reader:
            if original_columns is None:
                original_columns = list(chunk.columns)
            elif list(chunk.columns) != original_columns:
                raise CSVSchemaError(
                    "CSV columns changed while reading chunks, which is not supported."
                )

            row_count += len(chunk.index)
            for column_name in original_columns:
                inferred = _sqlite_type_for_dtype(chunk[column_name].dtype)
                current = inferred_types.get(column_name)
                if current is None:
                    inferred_types[column_name] = inferred
                elif current == "TEXT" or inferred == "TEXT":
                    inferred_types[column_name] = "TEXT"
                elif current == "REAL" or inferred == "REAL":
                    inferred_types[column_name] = "REAL"
                else:
                    inferred_types[column_name] = "INTEGER"

                if inferred_types[column_name] == "TEXT":
                    text_column_samples[column_name].append(chunk[column_name])

    except EmptyDataError:
        raise EmptyCSVError("The uploaded CSV is empty.") from None

    if original_columns is None or row_count == 0:
        raise EmptyCSVError("The uploaded CSV is empty.")

    # Second pass: run date detection on the accumulated TEXT column values.
    date_columns: set[str] = set()
    for col_name, chunks in text_column_samples.items():
        full_series = pd.concat(chunks, ignore_index=True)
        detected = _detect_date_column(full_series)
        if detected is not None:
            inferred_types[col_name] = detected
            date_columns.add(col_name)

    sanitized_columns = _unique_sanitized_names(original_columns)
    column_info = [
        CSVColumnInfo(
            original_name=original_name,
            sanitized_name=sanitized_name,
            sqlite_type=inferred_types.get(original_name, "TEXT"),
        )
        for original_name, sanitized_name in zip(original_columns, sanitized_columns)
    ]
    return original_columns, sanitized_columns, column_info, row_count, date_columns


def _normalize_date_column(
    series: pd.Series,
    sqlite_type: str,
    column_name: str,
) -> pd.Series:
    """Normalize a date/datetime series to Python date/datetime objects.

    Values that fail to parse are coerced to None (NULL in SQLite) rather
    than crashing the load.  A warning is logged if any values are dropped.

    NOTE ON AMBIGUITY: dayfirst=False (US M/D/YYYY convention) — see
    _detect_date_column() for the full ambiguity discussion.
    """
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        parsed = pd.to_datetime(series, errors="coerce", dayfirst=False, format="mixed")

    failed_count = parsed.isna().sum() - series.isna().sum()
    if failed_count > 0:
        logger.warning(
            "Column %r: %d value(s) could not be parsed as dates and were set to NULL.",
            column_name,
            failed_count,
        )

    if sqlite_type == "DATE":
        # SQLAlchemy's SQLite DATE dialect requires Python datetime.date objects.
        return parsed.apply(lambda ts: ts.date() if pd.notna(ts) else None)
    else:
        # DATETIME: return pandas Timestamps (subclass of datetime.datetime),
        # which SQLAlchemy's SQLite DATETIME dialect accepts directly.
        return parsed


def load_csv_to_sqlite(
    csv_source: str | Path | BinaryIO | TextIO,
    *,
    scratch_dir: str | Path,
    table_name: str | None = None,
) -> CSVLoadResult:
    """Load a CSV into a temporary SQLite database file.

    Date-like text columns (detected via pandas date parsing with a >90%
    success threshold) are normalized to ISO-8601 format before writing
    so that SQLite's strftime() and date functions work correctly.
    """

    connection_id = f"csv_{uuid4().hex}"
    scratch_path = Path(scratch_dir)
    scratch_path.mkdir(parents=True, exist_ok=True)

    source_path, staged_source_path = _materialize_source(csv_source, scratch_path, connection_id)
    sqlite_path = scratch_path / f"{connection_id}.sqlite"
    resolved_table_name = _sanitize_identifier(
        table_name or _source_name(csv_source, "csv_data"),
        fallback="csv_data",
    )

    try:
        original_columns, sanitized_columns, column_info, row_count, date_columns = _infer_schema(
            source_path
        )

        # Build a mapping from sanitized name -> CSVColumnInfo for the chunk loop.
        sanitized_to_info = {
            col.sanitized_name: col for col in column_info
        }

        engine = create_engine(
            f"sqlite+pysqlite:///{sqlite_path.resolve().as_posix()}",
            connect_args={"check_same_thread": False},
        )

        dtype_mapping = {
            sanitized_name: _sqlite_type_for_sqlalchemy(column.sqlite_type)
            for sanitized_name, column in zip(sanitized_columns, column_info)
        }

        # Build a mapping from sanitized name -> original name for date columns
        # so we can look up sqlite_type during normalization.
        sanitized_date_cols: dict[str, str] = {
            col.sanitized_name: col.sqlite_type
            for col in column_info
            if col.original_name in date_columns
        }

        reader = pd.read_csv(source_path, chunksize=_CHUNK_SIZE, encoding="utf-8-sig")
        for index, chunk in enumerate(reader):
            chunk = chunk.rename(columns=dict(zip(original_columns, sanitized_columns)))

            # Normalize date/datetime columns in-place within the chunk.
            for san_col, sqlite_type in sanitized_date_cols.items():
                chunk[san_col] = _normalize_date_column(
                    chunk[san_col],
                    sqlite_type,
                    san_col,
                )

            chunk.to_sql(
                resolved_table_name,
                con=engine,
                if_exists="replace" if index == 0 else "append",
                index=False,
                dtype=dtype_mapping,
            )

        engine.dispose()
    except Exception:
        cleanup_sqlite_artifacts(sqlite_path)
        raise
    finally:
        if staged_source_path is not None:
            try:
                staged_source_path.unlink()
            except FileNotFoundError:
                pass

    return CSVLoadResult(
        connection_id=connection_id,
        sqlite_path=sqlite_path,
        connection_string=f"sqlite+pysqlite:///{sqlite_path.resolve().as_posix()}",
        table_name=resolved_table_name,
        row_count=row_count,
        columns=tuple(column_info),
    )
