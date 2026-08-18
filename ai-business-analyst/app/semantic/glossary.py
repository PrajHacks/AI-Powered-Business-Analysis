from __future__ import annotations

import json
import logging
import re
import threading
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.config import get_settings
from app.db.introspection import SchemaInfo, SchemaIntrospector
from app.db.query_executor import QueryExecutor
from app.llm.ollama_client import OllamaClient

logger = logging.getLogger(__name__)


class ColumnGlossaryEntry(BaseModel):
    """Semantic description and metadata for a database column."""

    model_config = ConfigDict(extra="forbid")

    column_name: str
    description: str
    is_auto_generated: bool = True
    synonyms: list[str] = Field(default_factory=list)


class TableGlossaryEntry(BaseModel):
    """Semantic description and metadata for a database table."""

    model_config = ConfigDict(extra="forbid")

    table_name: str
    description: str
    is_auto_generated: bool = True
    columns: dict[str, ColumnGlossaryEntry] = Field(default_factory=dict)


class SemanticGlossary:
    """Thread-safe in-memory semantic glossary store scoped per connection_id."""

    def __init__(
        self,
        ollama_client: OllamaClient | None = None,
        *,
        model: str | None = None,
    ) -> None:
        self._store: dict[str, dict[str, TableGlossaryEntry]] = {}
        self._lock = threading.Lock()
        self._client = ollama_client
        self._model = model or get_settings().ollama_model

    def get_glossary(self, connection_id: str) -> dict[str, TableGlossaryEntry] | None:
        """Return the dictionary of table glossary entries for a connection, or None."""
        with self._lock:
            if connection_id not in self._store:
                return None
            # Return a copy of the mapping
            return dict(self._store[connection_id])

    def update_entry(
        self,
        connection_id: str,
        table_name: str,
        column_name: str | None = None,
        description: str | None = None,
        synonyms: list[str] | None = None,
    ) -> TableGlossaryEntry:
        """Update description or synonyms for a table or column.

        Setting a description/synonyms here sets is_auto_generated=False on that
        specific entry without affecting sibling or parent/child entries.
        """
        with self._lock:
            conn_glossary = self._store.setdefault(connection_id, {})
            table_entry = conn_glossary.get(table_name)
            if table_entry is None:
                table_entry = TableGlossaryEntry(
                    table_name=table_name,
                    description=f"Table '{table_name}'",
                    is_auto_generated=True,
                    columns={},
                )
                conn_glossary[table_name] = table_entry

            if column_name is not None:
                col_entry = table_entry.columns.get(column_name)
                if col_entry is None:
                    col_entry = ColumnGlossaryEntry(
                        column_name=column_name,
                        description=f"Column '{column_name}'",
                        is_auto_generated=True,
                        synonyms=[],
                    )
                    table_entry.columns[column_name] = col_entry

                if description is not None:
                    col_entry.description = description.strip()
                    col_entry.is_auto_generated = False
                if synonyms is not None:
                    col_entry.synonyms = [s.strip() for s in synonyms if s.strip()]
                    col_entry.is_auto_generated = False
            else:
                if description is not None:
                    table_entry.description = description.strip()
                    table_entry.is_auto_generated = False

            return table_entry

    def generate_draft(
        self,
        connection_id: str,
        schema_introspector: SchemaIntrospector,
        query_executor: QueryExecutor | None = None,
        ollama_client: OllamaClient | None = None,
        model: str | None = None,
    ) -> dict[str, TableGlossaryEntry]:
        """Generate a draft glossary for all tables/columns in the connection's schema.

        Calls Ollama once per table with table/column metadata and sample values.
        Manual edits (is_auto_generated=False) are preserved across regenerations.
        """
        client = ollama_client or self._client
        if client is None:
            client = OllamaClient(
                base_url=get_settings().ollama_base_url,
                timeout_seconds=get_settings().ollama_timeout_seconds,
            )
        active_model = model or self._model

        schema_info: SchemaInfo = schema_introspector.get_schema(connection_id)

        with self._lock:
            existing_conn_glossary = self._store.get(connection_id, {})

        new_table_entries: dict[str, TableGlossaryEntry] = {}

        for table in schema_info.tables:
            table_name = table.name
            existing_table = existing_conn_glossary.get(table_name)

            # Sample values for each column
            samples = self._sample_table_values(
                connection_id=connection_id,
                table_name=table_name,
                columns=[col.name for col in table.columns],
                query_executor=query_executor,
            )

            # Build prompt for Ollama
            prompt = self._build_table_glossary_prompt(
                table_name=table_name,
                columns=table.columns,
                samples=samples,
            )

            # Call Ollama
            raw_response = ""
            try:
                raw_response = client.generate(
                    prompt=prompt,
                    model=active_model,
                    system="You are a data analyst generating business glossary documentation. Return only valid JSON.",
                    temperature=0.0,
                )
            except Exception as exc:
                logger.warning(
                    "Ollama call failed while generating glossary for table '%s': %s",
                    table_name,
                    exc,
                )

            # Parse response
            parsed_data = self._parse_glossary_json(raw_response)

            # Table description
            gen_table_desc = parsed_data.get("description")
            if not isinstance(gen_table_desc, str) or not gen_table_desc.strip():
                gen_table_desc = f"Table containing {table_name} records."

            # If existing table was manually edited, preserve manual description
            if existing_table is not None and not existing_table.is_auto_generated:
                final_table_desc = existing_table.description
                final_table_auto = False
            else:
                final_table_desc = gen_table_desc.strip()
                final_table_auto = True

            # Process columns
            columns_data = parsed_data.get("columns", {})
            if not isinstance(columns_data, dict):
                columns_data = {}

            final_columns: dict[str, ColumnGlossaryEntry] = {}
            for col in table.columns:
                col_name = col.name
                existing_col = (
                    existing_table.columns.get(col_name) if existing_table else None
                )

                if existing_col is not None and not existing_col.is_auto_generated:
                    # Preserve manual edit
                    final_columns[col_name] = existing_col
                else:
                    # Use parsed or fallback
                    col_info = columns_data.get(col_name)
                    if isinstance(col_info, dict):
                        col_desc = col_info.get("description")
                        if not isinstance(col_desc, str) or not col_desc.strip():
                            col_desc = f"Column '{col_name}' ({col.type})"
                        col_synonyms = col_info.get("synonyms")
                        if not isinstance(col_synonyms, list):
                            col_synonyms = []
                        else:
                            col_synonyms = [
                                str(s).strip() for s in col_synonyms if str(s).strip()
                            ]
                    elif isinstance(col_info, str) and col_info.strip():
                        col_desc = col_info.strip()
                        col_synonyms = []
                    else:
                        col_desc = f"Column '{col_name}' ({col.type})"
                        col_synonyms = []

                    final_columns[col_name] = ColumnGlossaryEntry(
                        column_name=col_name,
                        description=col_desc.strip(),
                        is_auto_generated=True,
                        synonyms=col_synonyms,
                    )

            new_table_entries[table_name] = TableGlossaryEntry(
                table_name=table_name,
                description=final_table_desc,
                is_auto_generated=final_table_auto,
                columns=final_columns,
            )

        with self._lock:
            self._store[connection_id] = new_table_entries
            return dict(self._store[connection_id])

    def to_llm_context(self, connection_id: str) -> str:
        """Return a compact plain-text business glossary block for prompt injection.

        Returns empty string if no glossary has been generated for connection_id.
        """
        with self._lock:
            glossary = self._store.get(connection_id)
            if not glossary:
                return ""

            lines: list[str] = ["Business Glossary:"]
            for table_name, table_entry in glossary.items():
                lines.append(f"Table '{table_name}': {table_entry.description}")
                for col_name, col_entry in table_entry.columns.items():
                    synonym_str = ""
                    if col_entry.synonyms:
                        synonym_str = f" (synonyms: {', '.join(col_entry.synonyms)})"
                    lines.append(
                        f"  - Column '{col_name}': {col_entry.description}{synonym_str}"
                    )

            return "\n".join(lines)

    def _sample_table_values(
        self,
        connection_id: str,
        table_name: str,
        columns: list[str],
        query_executor: QueryExecutor | None,
    ) -> dict[str, list[Any]]:
        if query_executor is None:
            return {}

        samples: dict[str, list[Any]] = {}
        for col in columns:
            # Query up to 5 distinct non-null values
            # Using simple double-quoted or plain SQL safe select
            safe_col = f'"{col}"' if not col.startswith('"') else col
            safe_table = f'"{table_name}"' if not table_name.startswith('"') else table_name
            sql = f"SELECT DISTINCT {safe_col} AS val FROM {safe_table} WHERE {safe_col} IS NOT NULL LIMIT 5"
            try:
                res = query_executor.execute(connection_id, sql)
                values = [r["val"] for r in res.rows if r.get("val") is not None]
                # Keep values short and printable
                formatted_values = []
                for v in values:
                    sv = str(v)
                    if len(sv) > 50:
                        sv = sv[:47] + "..."
                    formatted_values.append(sv)
                samples[col] = formatted_values
            except Exception:
                samples[col] = []
        return samples

    def _build_table_glossary_prompt(
        self,
        table_name: str,
        columns: list[Any],
        samples: dict[str, list[Any]],
    ) -> str:
        lines = [
            "Generate business-friendly plain-English descriptions and common synonyms for the following database table and its columns.",
            f"Table name: {table_name}",
            "Columns:",
        ]
        for col in columns:
            col_samples = samples.get(col.name, [])
            sample_str = f", sample values: {col_samples}" if col_samples else ""
            lines.append(f"- {col.name} ({col.type}){sample_str}")

        lines.extend(
            [
                "",
                "Return a JSON object with this exact structure:",
                "{",
                '  "description": "Plain English description of what this table represents",',
                '  "columns": {',
                '    "<column_name>": {',
                '      "description": "Clear plain English description of what this column measures or stores",',
                '      "synonyms": ["common_term1", "common_term2"]',
                "    }",
                "  }",
                "}",
                "Output JSON ONLY. No markdown fences, no explanatory text.",
            ]
        )
        return "\n".join(lines)

    def _parse_glossary_json(self, raw: str) -> dict[str, Any]:
        if not raw:
            return {}

        text = raw.strip()
        # Strip code fences if present
        fence_match = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL | re.IGNORECASE)
        if fence_match:
            text = fence_match.group(1).strip()

        # Find outer json block { ... }
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            text = text[start : end + 1]

        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass

        return {}
