from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request, status

from app.db.connection import ConnectionNotFoundError
from app.db.introspection import SchemaIntrospectionError, SchemaInfo

router = APIRouter(prefix="/connections", tags=["schema"])


def _get_introspector(request: Request):
    introspector = getattr(request.app.state, "schema_introspector", None)
    if introspector is None:
        raise RuntimeError("Schema introspector is not configured on the application.")
    return introspector


@router.get("/{connection_id}/schema", response_model=SchemaInfo)
def get_schema(
    connection_id: str,
    request: Request,
    refresh: bool = Query(default=False),
) -> SchemaInfo:
    introspector = _get_introspector(request)
    try:
        return introspector.get_schema(connection_id, refresh=refresh)
    except ConnectionNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from None
    except SchemaIntrospectionError as exc:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)) from None


@router.post("/{connection_id}/schema/refresh", response_model=SchemaInfo)
def refresh_schema(connection_id: str, request: Request) -> SchemaInfo:
    introspector = _get_introspector(request)
    try:
        return introspector.refresh(connection_id)
    except ConnectionNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from None
    except SchemaIntrospectionError as exc:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)) from None

