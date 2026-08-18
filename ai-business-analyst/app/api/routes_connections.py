from __future__ import annotations

from pathlib import Path
from datetime import datetime

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile, status
from pydantic import BaseModel, ConfigDict, Field

from app.config import Settings
from app.db.connection import (
    ConnectionAlreadyExistsError,
    ConnectionManager,
    ConnectionManagerError,
    ConnectionNotFoundError,
    ConnectionRegistrationError,
    cleanup_sqlite_artifacts,
)
from app.db.csv_loader import CSVLoadError, load_csv_to_sqlite

router = APIRouter(prefix="/connections", tags=["connections"])


class DatabaseConnectionRequest(BaseModel):
    connection_string: str = Field(min_length=1)
    name: str = Field(min_length=1)


class ConnectionIdResponse(BaseModel):
    connection_id: str


class ConnectionSummaryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    connection_id: str
    name: str
    dialect: str
    created_at: datetime


def _get_connection_manager(request: Request) -> ConnectionManager:
    manager = getattr(request.app.state, "connection_manager", None)
    if manager is None:
        raise RuntimeError("Connection manager is not configured on the application.")
    return manager


def _get_settings(request: Request) -> Settings:
    settings = getattr(request.app.state, "settings", None)
    if settings is None:
        raise RuntimeError("Application settings are not configured.")
    return settings


@router.post("/database", response_model=ConnectionIdResponse)
def register_database_connection(
    payload: DatabaseConnectionRequest,
    request: Request,
) -> ConnectionIdResponse:
    manager = _get_connection_manager(request)
    try:
        connection_id = manager.register_connection(
            name=payload.name,
            connection_string=payload.connection_string,
        )
    except ConnectionRegistrationError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from None

    return ConnectionIdResponse(connection_id=connection_id)


@router.post("/csv", response_model=ConnectionIdResponse)
async def register_csv_connection(
    request: Request,
    name: str = Form(..., min_length=1),
    file: UploadFile = File(...),
) -> ConnectionIdResponse:
    settings = _get_settings(request)
    manager = _get_connection_manager(request)

    try:
        load_result = load_csv_to_sqlite(
            file.file,
            scratch_dir=settings.scratch_data_dir,
            table_name=Path(file.filename).stem if file.filename else None,
        )
        manager.register_connection(
            name=name,
            connection_string=load_result.connection_string,
            connection_id=load_result.connection_id,
            cleanup_path=load_result.sqlite_path,
        )
    except CSVLoadError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from None
    except ConnectionRegistrationError as exc:
        cleanup_sqlite_artifacts(load_result.sqlite_path)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from None
    finally:
        await file.close()

    return ConnectionIdResponse(connection_id=load_result.connection_id)


@router.get("", response_model=list[ConnectionSummaryResponse])
def list_connections(request: Request) -> list[ConnectionSummaryResponse]:
    manager = _get_connection_manager(request)
    return [ConnectionSummaryResponse.model_validate(item) for item in manager.list_connections()]


@router.delete("/{connection_id}", response_model=ConnectionIdResponse)
def delete_connection(
    connection_id: str,
    request: Request,
) -> ConnectionIdResponse:
    manager = _get_connection_manager(request)
    try:
        manager.remove_connection(connection_id)
    except ConnectionNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from None

    return ConnectionIdResponse(connection_id=connection_id)
