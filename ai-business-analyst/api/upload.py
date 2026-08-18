"""Vercel Serverless Function: CSV upload and schema introspection.

Stateless endpoint that accepts a multipart/form-data CSV upload or raw CSV,
normalizes dates, ingests into an in-memory SQLite database, and returns the
introspected schema in JSON format.
"""

from __future__ import annotations

import email
import io
import json
import re
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler

import pandas as pd

_MAX_BODY_SIZE = int(4.5 * 1024 * 1024)  # 4.5 MB Vercel payload limit
_DATE_PARSE_THRESHOLD = 0.90
_IDENTIFIER_RE = re.compile(r"[^0-9a-zA-Z_]+")


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


def _normalize_and_detect_dates(series: pd.Series) -> tuple[pd.Series, str]:
    if not pd.api.types.is_string_dtype(series.dtype) and not pd.api.types.is_object_dtype(series.dtype):
        return series, "OTHER"

    non_null = series.dropna().astype(str).str.strip()
    if non_null.empty:
        return series, "TEXT"

    try:
        parsed = pd.to_datetime(non_null, errors="coerce", format="mixed")
    except Exception:
        return series, "TEXT"

    valid_ratio = float(parsed.notna().mean())
    if valid_ratio < _DATE_PARSE_THRESHOLD:
        return series, "TEXT"

    has_time = any(t.hour != 0 or t.minute != 0 or t.second != 0 for t in parsed.dropna())
    sqlite_type = "DATETIME" if has_time else "DATE"

    full_parsed = pd.to_datetime(series, errors="coerce", format="mixed")
    if has_time:
        normalized = full_parsed.dt.strftime("%Y-%m-%d %H:%M:%S")
    else:
        normalized = full_parsed.dt.strftime("%Y-%m-%d")

    return normalized, sqlite_type


def process_csv(csv_bytes: bytes, original_filename: str = "dataset.csv", custom_name: str = "") -> dict:
    if len(csv_bytes) > _MAX_BODY_SIZE:
        raise ValueError("File size exceeds the 4.5MB serverless limit.")

    table_name = _sanitize_identifier(custom_name or original_filename.split(".")[0], fallback="dataset")

    try:
        df = pd.read_csv(io.BytesIO(csv_bytes))
    except Exception as exc:
        raise ValueError(f"Failed to parse CSV: {exc}") from exc

    if df.empty or len(df.columns) == 0:
        raise ValueError("The uploaded CSV has no rows or columns.")

    sanitized_cols = _unique_sanitized_names(list(df.columns))
    df.columns = sanitized_cols

    column_types: dict[str, str] = {}
    for col in df.columns:
        series, dtype_kind = _normalize_and_detect_dates(df[col])
        df[col] = series
        if dtype_kind in ("DATE", "DATETIME"):
            column_types[col] = dtype_kind.lower()
        elif pd.api.types.is_bool_dtype(df[col].dtype):
            column_types[col] = "boolean"
        elif pd.api.types.is_integer_dtype(df[col].dtype):
            column_types[col] = "integer"
        elif pd.api.types.is_float_dtype(df[col].dtype):
            column_types[col] = "float"
        else:
            column_types[col] = "text"

    # Verify ingestion into SQLite in-memory engine
    conn = sqlite3.connect(":memory:")
    df.to_sql(table_name, conn, index=False)
    conn.close()

    # Introspect schema structure
    columns_info = []
    for col in df.columns:
        columns_info.append({
            "name": col,
            "type": column_types.get(col, "text"),
            "nullable": bool(df[col].isna().any()),
            "primary_key": False,
        })

    schema_info = {
        "connection_id": "demo-csv-session",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "tables": [
            {
                "name": table_name,
                "kind": "table",
                "columns": columns_info,
                "primary_key_columns": [],
                "foreign_keys": [],
                "row_count": len(df),
                "row_count_is_estimate": False,
            }
        ],
    }

    return {
        "connection_id": "demo-csv-session",
        "name": custom_name or original_filename,
        "table_name": table_name,
        "row_count": len(df),
        "columns": columns_info,
        "schema": schema_info,
    }


class handler(BaseHTTPRequestHandler):
    def _send_json(self, status_code: int, data: dict) -> None:
        payload = json.dumps(data).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(payload)

    def do_OPTIONS(self) -> None:
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_POST(self) -> None:
        try:
            content_length_header = self.headers.get("Content-Length")
            if not content_length_header:
                self._send_json(400, {"detail": "Missing Content-Length header."})
                return

            content_length = int(content_length_header)
            if content_length > _MAX_BODY_SIZE:
                self._send_json(413, {"detail": f"File size exceeds {_MAX_BODY_SIZE / (1024*1024):.1f}MB limit."})
                return

            body = self.rfile.read(content_length)
            content_type = self.headers.get("Content-Type", "")

            csv_bytes = b""
            filename = "dataset.csv"
            dataset_name = ""

            if "multipart/form-data" in content_type:
                # Wrap with HTTP header for email parser
                msg_bytes = f"Content-Type: {content_type}\r\n\r\n".encode("latin1") + body
                msg = email.message_from_bytes(msg_bytes)

                for part in msg.walk():
                    if part.is_multipart():
                        continue
                    cd = part.get("Content-Disposition", "")
                    if "filename=" in cd:
                        filename = part.get_filename() or "dataset.csv"
                        csv_bytes = part.get_payload(decode=True) or b""
                    elif 'name="name"' in cd:
                        raw_name = part.get_payload(decode=True)
                        if raw_name:
                            dataset_name = raw_name.decode("utf-8", errors="ignore").strip()
            else:
                # Direct raw CSV upload
                csv_bytes = body

            if not csv_bytes:
                self._send_json(400, {"detail": "No CSV file data found in request."})
                return

            result = process_csv(csv_bytes, original_filename=filename, custom_name=dataset_name)
            self._send_json(200, result)

        except ValueError as val_err:
            self._send_json(400, {"detail": str(val_err)})
        except Exception as exc:
            self._send_json(500, {"detail": f"Internal server error: {exc}"})
