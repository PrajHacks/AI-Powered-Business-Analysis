from __future__ import annotations

import logging
from typing import Literal

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field

from app.db.connection import ConnectionNotFoundError
from app.feedback.feedback_store import FeedbackEntry, FeedbackStore

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/connections", tags=["feedback"])


class FeedbackCreateRequest(BaseModel):
    question: str = Field(min_length=1)
    generated_sql: str = Field(min_length=1)
    rating: Literal["up", "down"]
    comment: str | None = None


def _get_feedback_store(request: Request) -> FeedbackStore:
    store = getattr(request.app.state, "feedback_store", None)
    if store is None:
        return FeedbackStore()
    return store


@router.post(
    "/{connection_id}/feedback",
    response_model=FeedbackEntry,
    status_code=status.HTTP_201_CREATED,
    description="Record user thumbs up/down feedback on a question and generated SQL query.",
)
def record_feedback(
    connection_id: str,
    payload: FeedbackCreateRequest,
    request: Request,
) -> FeedbackEntry:
    connection_manager = getattr(request.app.state, "connection_manager", None)
    if connection_manager is not None:
        try:
            connection_manager.get_engine(connection_id)
        except ConnectionNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from None

    store = _get_feedback_store(request)
    return store.record_feedback(
        connection_id=connection_id,
        question=payload.question,
        sql=payload.generated_sql,
        rating=payload.rating,
        comment=payload.comment,
    )


@router.get(
    "/{connection_id}/feedback",
    response_model=list[FeedbackEntry],
    description="List all recorded feedback entries for a given connection.",
)
def get_feedback(
    connection_id: str,
    request: Request,
) -> list[FeedbackEntry]:
    connection_manager = getattr(request.app.state, "connection_manager", None)
    if connection_manager is not None:
        try:
            connection_manager.get_engine(connection_id)
        except ConnectionNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from None

    store = _get_feedback_store(request)
    return store.get_all_feedback(connection_id)
