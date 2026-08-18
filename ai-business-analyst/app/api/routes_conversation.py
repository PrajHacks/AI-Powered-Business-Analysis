from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request, Response, status

from app.conversation.memory import ConversationMemory

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/conversations", tags=["conversations"])


def _get_memory(request: Request) -> ConversationMemory:
    memory = getattr(request.app.state, "conversation_memory", None)
    if memory is None:
        raise RuntimeError("ConversationMemory is not configured on the application.")
    return memory


@router.delete(
    "/{conversation_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    description="Clear all stored history for a conversation thread.",
)
def clear_conversation(conversation_id: str, request: Request) -> Response:
    memory = _get_memory(request)
    if not memory.exists(conversation_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Conversation '{conversation_id}' not found or already expired.",
        )
    memory.clear(conversation_id)
    logger.info("Conversation '%s' cleared by explicit DELETE request.", conversation_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
