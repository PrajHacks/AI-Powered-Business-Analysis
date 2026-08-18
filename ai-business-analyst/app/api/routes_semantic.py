from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field

from app.db.connection import ConnectionNotFoundError
from app.semantic.glossary import ColumnGlossaryEntry, SemanticGlossary, TableGlossaryEntry

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/connections", tags=["semantic"])


class ColumnUpdatePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    description: str | None = None
    synonyms: list[str] | None = None


class TableUpdatePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    description: str | None = None
    columns: dict[str, ColumnUpdatePayload] | None = None


def _get_semantic_glossary(request: Request) -> SemanticGlossary:
    glossary = getattr(request.app.state, "semantic_glossary", None)
    if glossary is None:
        raise RuntimeError("Semantic glossary is not configured on the application.")
    return glossary


@router.post(
    "/{connection_id}/semantic/generate",
    response_model=dict[str, TableGlossaryEntry],
    description="Generate or refresh draft business glossary for a connection.",
)
def generate_semantic_glossary(
    connection_id: str,
    request: Request,
) -> dict[str, TableGlossaryEntry]:
    connection_manager = getattr(request.app.state, "connection_manager", None)
    introspector = getattr(request.app.state, "schema_introspector", None)
    executor = getattr(request.app.state, "query_executor", None)
    client = getattr(request.app.state, "ollama_client", None)
    glossary_store = _get_semantic_glossary(request)

    if connection_manager is None or introspector is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Application state is incomplete.",
        )

    try:
        connection_manager.get_engine(connection_id)
    except ConnectionNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from None

    try:
        return glossary_store.generate_draft(
            connection_id=connection_id,
            schema_introspector=introspector,
            query_executor=executor,
            ollama_client=client,
        )
    except Exception as exc:
        logger.exception("Failed to generate semantic glossary for connection %s", connection_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to generate semantic glossary: {exc}",
        ) from None


@router.get(
    "/{connection_id}/semantic",
    response_model=dict[str, TableGlossaryEntry],
    description="Get current business glossary for a connection.",
)
def get_semantic_glossary(
    connection_id: str,
    request: Request,
) -> dict[str, TableGlossaryEntry]:
    connection_manager = getattr(request.app.state, "connection_manager", None)
    if connection_manager is not None:
        try:
            connection_manager.get_engine(connection_id)
        except ConnectionNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from None

    glossary_store = _get_semantic_glossary(request)
    glossary = glossary_store.get_glossary(connection_id)
    if glossary is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No semantic glossary found for connection '{connection_id}'. Call /semantic/generate first.",
        )
    return glossary


@router.patch(
    "/{connection_id}/semantic/{table_name}",
    response_model=TableGlossaryEntry,
    description="Update table-level and/or column-level descriptions and synonyms.",
)
def update_table_glossary(
    connection_id: str,
    table_name: str,
    payload: TableUpdatePayload,
    request: Request,
) -> TableGlossaryEntry:
    connection_manager = getattr(request.app.state, "connection_manager", None)
    if connection_manager is not None:
        try:
            connection_manager.get_engine(connection_id)
        except ConnectionNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from None

    glossary_store = _get_semantic_glossary(request)
    glossary = glossary_store.get_glossary(connection_id)
    if glossary is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No semantic glossary found for connection '{connection_id}'. Call /semantic/generate first.",
        )

    if payload.description is not None:
        glossary_store.update_entry(
            connection_id=connection_id,
            table_name=table_name,
            column_name=None,
            description=payload.description,
        )

    if payload.columns:
        for col_name, col_update in payload.columns.items():
            glossary_store.update_entry(
                connection_id=connection_id,
                table_name=table_name,
                column_name=col_name,
                description=col_update.description,
                synonyms=col_update.synonyms,
            )

    updated_glossary = glossary_store.get_glossary(connection_id)
    if updated_glossary is None or table_name not in updated_glossary:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Table '{table_name}' not found in glossary for connection '{connection_id}'.",
        )

    return updated_glossary[table_name]
