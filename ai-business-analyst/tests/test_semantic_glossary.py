from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Column, Integer, MetaData, String, Table, create_engine

from app.db.introspection import ColumnInfo, SchemaInfo, TableInfo
from app.llm.sql_generator import SQLGenerator
from app.main import create_app
from app.semantic.glossary import (
    ColumnGlossaryEntry,
    SemanticGlossary,
    TableGlossaryEntry,
)


def _make_fake_schema_info(connection_id: str = "conn-1") -> SchemaInfo:
    return SchemaInfo(
        connection_id=connection_id,
        generated_at=datetime.now(timezone.utc),
        tables=[
            TableInfo(
                name="sales",
                kind="table",
                columns=[
                    ColumnInfo(name="id", type="integer", nullable=False, primary_key=True),
                    ColumnInfo(name="region", type="text", nullable=False),
                    ColumnInfo(name="revenue", type="integer", nullable=False),
                ],
                primary_key_columns=["id"],
                foreign_keys=[],
                row_count=10,
            )
        ],
    )


def _register_sqlite_connection(app, tmp_path: Path) -> str:
    sqlite_path = tmp_path / "semantic_test.sqlite"
    engine = create_engine(f"sqlite+pysqlite:///{sqlite_path.resolve().as_posix()}")
    metadata = MetaData()
    Table(
        "sales",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("region", String(50)),
        Column("revenue", Integer),
    )
    metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(
            Table("sales", metadata).insert(),
            [
                {"id": 1, "region": "North", "revenue": 100},
                {"id": 2, "region": "South", "revenue": 200},
            ],
        )
    engine.dispose()
    return app.state.connection_manager.register_connection(
        name="Semantic DB",
        connection_string=f"sqlite+pysqlite:///{sqlite_path.resolve().as_posix()}",
    )


class TestSemanticGlossaryUnit:
    def test_generate_draft_happy_path(self) -> None:
        schema_introspector = MagicMock()
        schema_introspector.get_schema.return_value = _make_fake_schema_info("conn-1")

        mock_client = MagicMock()
        mock_client.generate.return_value = json.dumps(
            {
                "description": "Sales transactions and performance records",
                "columns": {
                    "id": {"description": "Primary key identifier", "synonyms": ["sale_id", "id"]},
                    "region": {"description": "Sales territory or geography", "synonyms": ["territory", "zone"]},
                    "revenue": {"description": "Total monetary intake in USD", "synonyms": ["sales", "turnover"]},
                },
            }
        )

        glossary = SemanticGlossary(ollama_client=mock_client)
        result = glossary.generate_draft("conn-1", schema_introspector)

        assert "sales" in result
        table = result["sales"]
        assert table.table_name == "sales"
        assert table.description == "Sales transactions and performance records"
        assert table.is_auto_generated is True
        assert len(table.columns) == 3

        assert table.columns["id"].description == "Primary key identifier"
        assert table.columns["id"].is_auto_generated is True
        assert table.columns["id"].synonyms == ["sale_id", "id"]

        assert table.columns["region"].description == "Sales territory or geography"
        assert table.columns["region"].is_auto_generated is True

        assert table.columns["revenue"].description == "Total monetary intake in USD"
        assert table.columns["revenue"].is_auto_generated is True

    def test_generate_draft_malformed_json_fallback(self) -> None:
        schema_introspector = MagicMock()
        schema_introspector.get_schema.return_value = _make_fake_schema_info("conn-1")

        mock_client = MagicMock()
        mock_client.generate.return_value = "This is not valid JSON at all!"

        glossary = SemanticGlossary(ollama_client=mock_client)
        result = glossary.generate_draft("conn-1", schema_introspector)

        assert "sales" in result
        table = result["sales"]
        assert "sales" in table.description
        assert table.is_auto_generated is True
        assert len(table.columns) == 3

        for col_name, col_entry in table.columns.items():
            assert col_entry.is_auto_generated is True
            assert col_name in col_entry.description

    def test_generate_draft_partial_response_omitted_column_fallback(self) -> None:
        schema_introspector = MagicMock()
        schema_introspector.get_schema.return_value = _make_fake_schema_info("conn-1")

        mock_client = MagicMock()
        # Model only returns 'region', omits 'id' and 'revenue'
        mock_client.generate.return_value = json.dumps(
            {
                "description": "Sales table",
                "columns": {
                    "region": {"description": "Sales geographic region", "synonyms": ["geo"]},
                },
            }
        )

        glossary = SemanticGlossary(ollama_client=mock_client)
        result = glossary.generate_draft("conn-1", schema_introspector)

        table = result["sales"]
        assert table.columns["region"].description == "Sales geographic region"
        assert table.columns["region"].synonyms == ["geo"]

        # Omitted columns have generic fallback
        assert "id" in table.columns["id"].description
        assert table.columns["id"].is_auto_generated is True
        assert "revenue" in table.columns["revenue"].description
        assert table.columns["revenue"].is_auto_generated is True

    def test_update_entry_column_only_affects_that_column(self) -> None:
        glossary = SemanticGlossary()
        schema_introspector = MagicMock()
        schema_introspector.get_schema.return_value = _make_fake_schema_info("conn-1")

        mock_client = MagicMock()
        mock_client.generate.return_value = json.dumps(
            {
                "description": "Auto table description",
                "columns": {
                    "id": {"description": "Auto ID"},
                    "region": {"description": "Auto Region"},
                    "revenue": {"description": "Auto Revenue"},
                },
            }
        )
        glossary.generate_draft("conn-1", schema_introspector, ollama_client=mock_client)

        # Update only 'revenue' column
        updated_table = glossary.update_entry(
            "conn-1",
            table_name="sales",
            column_name="revenue",
            description="Manually curated gross revenue",
            synonyms=["sales_rev", "turnover"],
        )

        # Table-level flag unchanged
        assert updated_table.is_auto_generated is True
        assert updated_table.description == "Auto table description"

        # Sibling columns unchanged
        assert updated_table.columns["id"].is_auto_generated is True
        assert updated_table.columns["id"].description == "Auto ID"
        assert updated_table.columns["region"].is_auto_generated is True

        # Target column updated and marked manual
        assert updated_table.columns["revenue"].is_auto_generated is False
        assert updated_table.columns["revenue"].description == "Manually curated gross revenue"
        assert updated_table.columns["revenue"].synonyms == ["sales_rev", "turnover"]

    def test_regenerate_preserves_manually_edited_entries(self) -> None:
        schema_introspector = MagicMock()
        schema_introspector.get_schema.return_value = _make_fake_schema_info("conn-1")

        mock_client = MagicMock()
        mock_client.generate.return_value = json.dumps(
            {
                "description": "Auto Draft V1",
                "columns": {
                    "id": {"description": "Auto ID V1"},
                    "region": {"description": "Auto Region V1"},
                    "revenue": {"description": "Auto Revenue V1"},
                },
            }
        )

        glossary = SemanticGlossary(ollama_client=mock_client)
        glossary.generate_draft("conn-1", schema_introspector)

        # Manually update 'revenue' and table description
        glossary.update_entry("conn-1", "sales", description="Manual Table Desc")
        glossary.update_entry(
            "conn-1",
            "sales",
            column_name="revenue",
            description="Manual Revenue Desc",
            synonyms=["manual_syn"],
        )

        # Simulate second generation with new AI outputs
        mock_client.generate.return_value = json.dumps(
            {
                "description": "Auto Draft V2 - Should be ignored for table",
                "columns": {
                    "id": {"description": "Auto ID V2 - Updated"},
                    "region": {"description": "Auto Region V2 - Updated"},
                    "revenue": {"description": "Auto Revenue V2 - Should be ignored"},
                },
            }
        )

        result_v2 = glossary.generate_draft("conn-1", schema_introspector)
        table_v2 = result_v2["sales"]

        # Manual edits are preserved
        assert table_v2.description == "Manual Table Desc"
        assert table_v2.is_auto_generated is False
        assert table_v2.columns["revenue"].description == "Manual Revenue Desc"
        assert table_v2.columns["revenue"].is_auto_generated is False
        assert table_v2.columns["revenue"].synonyms == ["manual_syn"]

        # Auto-generated columns are refreshed
        assert table_v2.columns["id"].description == "Auto ID V2 - Updated"
        assert table_v2.columns["id"].is_auto_generated is True
        assert table_v2.columns["region"].description == "Auto Region V2 - Updated"
        assert table_v2.columns["region"].is_auto_generated is True

    def test_to_llm_context_returns_empty_string_when_no_glossary(self) -> None:
        glossary = SemanticGlossary()
        context = glossary.to_llm_context("non-existent-conn")
        assert context == ""

    def test_to_llm_context_formats_glossary_compactly(self) -> None:
        glossary = SemanticGlossary()
        schema_introspector = MagicMock()
        schema_introspector.get_schema.return_value = _make_fake_schema_info("conn-1")

        mock_client = MagicMock()
        mock_client.generate.return_value = json.dumps(
            {
                "description": "Sales data",
                "columns": {
                    "id": {"description": "Order ID"},
                    "region": {"description": "Sales Region", "synonyms": ["territory"]},
                    "revenue": {"description": "Revenue in USD", "synonyms": ["sales", "income"]},
                },
            }
        )
        glossary.generate_draft("conn-1", schema_introspector, ollama_client=mock_client)
        context = glossary.to_llm_context("conn-1")

        assert "Business Glossary:" in context
        assert "Table 'sales': Sales data" in context
        assert "Column 'region': Sales Region (synonyms: territory)" in context
        assert "Column 'revenue': Revenue in USD (synonyms: sales, income)" in context


class TestSQLGeneratorSemanticPrompt:
    def test_prompt_includes_semantic_context_and_anti_synonym_rule(self) -> None:
        client = MagicMock()
        gen = SQLGenerator(client, model="test-model")
        schema = _make_fake_schema_info("conn-1")

        semantic_context = "Business Glossary:\nTable 'sales': Sales records\n  - Column 'revenue': Total revenue (synonyms: sales, turnover)"

        prompt = gen.build_prompt(
            question="What is total sales by region?",
            schema_info=schema,
            dialect="sqlite",
            semantic_context=semantic_context,
        )

        # Raw schema present
        assert "Schema:" in prompt
        # Semantic context present
        assert "Business Glossary / Semantic Context" in prompt
        assert "Total revenue (synonyms: sales, turnover)" in prompt
        # Anti-synonym substitution rule present
        assert "Never use business glossary terms or synonyms as column names in the generated SQL" in prompt


class TestSemanticRoutes:
    def test_get_before_generate_returns_404(self, tmp_path: Path) -> None:
        app = create_app()
        conn_id = _register_sqlite_connection(app, tmp_path)

        with TestClient(app) as client:
            response = client.get(f"/connections/{conn_id}/semantic")
            assert response.status_code == 404

    def test_generate_route_and_patch_update(self, tmp_path: Path) -> None:
        app = create_app()
        conn_id = _register_sqlite_connection(app, tmp_path)

        # Mock Ollama generate
        app.state.ollama_client.generate = MagicMock(
            return_value=json.dumps(
                {
                    "description": "Initial table description",
                    "columns": {
                        "id": {"description": "Sale ID"},
                        "region": {"description": "Store region"},
                        "revenue": {"description": "Sales revenue"},
                    },
                }
            )
        )

        with TestClient(app) as client:
            # 1. Generate draft
            post_res = client.post(f"/connections/{conn_id}/semantic/generate")
            assert post_res.status_code == 200
            data = post_res.json()
            assert "sales" in data
            assert data["sales"]["description"] == "Initial table description"
            assert data["sales"]["columns"]["revenue"]["description"] == "Sales revenue"

            # 2. Get glossary
            get_res = client.get(f"/connections/{conn_id}/semantic")
            assert get_res.status_code == 200
            assert get_res.json()["sales"]["description"] == "Initial table description"

            # 3. Patch only column description
            patch_col_res = client.patch(
                f"/connections/{conn_id}/semantic/sales",
                json={
                    "columns": {
                        "revenue": {
                            "description": "Gross Revenue in Dollars",
                            "synonyms": ["turnover", "gross_sales"],
                        }
                    }
                },
            )
            assert patch_col_res.status_code == 200
            patched_table = patch_col_res.json()
            # Table description remains untouched
            assert patched_table["description"] == "Initial table description"
            assert patched_table["is_auto_generated"] is True
            # Column description updated
            assert patched_table["columns"]["revenue"]["description"] == "Gross Revenue in Dollars"
            assert patched_table["columns"]["revenue"]["is_auto_generated"] is False
            assert patched_table["columns"]["revenue"]["synonyms"] == ["turnover", "gross_sales"]

            # 4. Patch only table description
            patch_tbl_res = client.patch(
                f"/connections/{conn_id}/semantic/sales",
                json={"description": "Updated sales master table"},
            )
            assert patch_tbl_res.status_code == 200
            patched_table2 = patch_tbl_res.json()
            assert patched_table2["description"] == "Updated sales master table"
            assert patched_table2["is_auto_generated"] is False
            # Column description preserved
            assert patched_table2["columns"]["revenue"]["description"] == "Gross Revenue in Dollars"
            assert patched_table2["columns"]["revenue"]["is_auto_generated"] is False
