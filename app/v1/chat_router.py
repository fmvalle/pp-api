"""Chatbot híbrido — rotas REST (/v1/chat + histórico)."""

from __future__ import annotations

import logging
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import AuthContext, get_auth_context
from app.db.session import get_db
from app.v1.bot_school_access import assert_bot_enabled, bot_enabled_for_context
from app.v1._scope import can_use_assistant, resolve_bot_audience
from app.v1.bot_llm import (
    is_llm_configured,
    llm_config_source,
    load_active_llm_config,
    normalize_llm_config,
    verify_llm_connection,
)
from app.v1.bot_local import load_active_intents
from app.v1.bot_service import list_conversations, list_messages, process_chat

router = APIRouter(tags=["v1-chat"])
logger = logging.getLogger(__name__)


async def _assert_assistant(ctx: AuthContext) -> None:
    if not can_use_assistant(ctx.role):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Apenas professor ou administrador da plataforma",
        )


class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=4000)


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    conversation_id: UUID | None = None
    classroom_id: UUID | None = None
    academic_year_id: UUID | None = None
    history: list[ChatMessage] = Field(default_factory=list, max_length=20)
    page_context: dict[str, Any] | None = None


class ChatResponse(BaseModel):
    reply: str
    mode: Literal["local", "llm", "stub"]
    source: Literal["local", "local_data", "llm", "stub"] = "stub"
    intent_key: str | None = None
    confidence: float | None = None
    llm_provider: str | None = None
    conversation_id: UUID
    suggestions: list[str] = Field(default_factory=list)
    context: dict[str, Any] | None = None


class ConversationSummary(BaseModel):
    id: UUID
    title: str
    classroom_id: UUID | None = None
    academic_year_id: UUID | None = None
    created_at: Any = None
    updated_at: Any = None
    last_message: str | None = None


class ConversationMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str
    source: str | None = None
    intent_key: str | None = None
    confidence: float | None = None
    llm_provider: str | None = None
    created_at: Any = None


class ChatStatusResponse(BaseModel):
    local_intents: int
    llm_configured: bool
    active_provider: str | None = None
    active_model: str | None = None
    config_source: Literal["db", "env", "none"] = "none"
    llm_reachable: bool = False
    llm_error: str | None = None
    mode: Literal["local", "llm", "stub"]
    bot_enabled: bool = False


@router.get("/chat/status", response_model=ChatStatusResponse)
async def chat_status_v1(
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
    classroom_id: UUID | None = None,
):
    await _assert_assistant(ctx)
    audience = resolve_bot_audience(ctx.role)
    intents = await load_active_intents(db, audience=audience)
    config = await load_active_llm_config(db)
    configured = is_llm_configured(config)
    source = llm_config_source(config)
    reachable, llm_error = await verify_llm_connection(config) if configured else (False, None)
    active = normalize_llm_config(config) if configured and config else None
    enabled = await bot_enabled_for_context(db, ctx, classroom_id=classroom_id)
    return ChatStatusResponse(
        local_intents=len(intents),
        llm_configured=configured,
        active_provider=str(active["provider"]) if active else None,
        active_model=str(active.get("model_name")) if active else None,
        config_source=source if source in ("db", "env") else "none",
        llm_reachable=reachable,
        llm_error=llm_error,
        mode="llm" if configured and reachable else ("local" if configured else "stub"),
        bot_enabled=enabled,
    )


@router.get("/chat/conversations", response_model=list[ConversationSummary])
async def chat_conversations_v1(
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    await _assert_assistant(ctx)
    rows = await list_conversations(db, ctx)
    return [
        ConversationSummary(
            id=row["id"],
            title=row["title"],
            classroom_id=row.get("classroom_id"),
            academic_year_id=row.get("academic_year_id"),
            created_at=row.get("created_at"),
            updated_at=row.get("updated_at"),
            last_message=row.get("last_message"),
        )
        for row in rows
    ]


@router.get("/chat/conversations/{conversation_id}/messages", response_model=list[ConversationMessage])
async def chat_conversation_messages_v1(
    conversation_id: UUID,
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    await _assert_assistant(ctx)
    try:
        rows = await list_messages(db, ctx, conversation_id)
    except ValueError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    return [
        ConversationMessage(
            role=row["role"],
            content=row["content"],
            source=row.get("source"),
            intent_key=row.get("intent_key"),
            confidence=float(row["confidence"]) if row.get("confidence") is not None else None,
            llm_provider=row.get("llm_provider"),
            created_at=row.get("created_at"),
        )
        for row in rows
    ]


@router.post("/chat", response_model=ChatResponse)
async def chat_v1(
    body: ChatRequest,
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Chatbot híbrido: fuzzy local → consulta SQL → LLM dinâmica (bot_settings)."""
    await _assert_assistant(ctx)
    await assert_bot_enabled(
        db,
        ctx,
        classroom_id=body.classroom_id,
        page_context=body.page_context,
    )

    history = [{"role": m.role, "content": m.content} for m in body.history]
    try:
        result = await process_chat(
            db,
            ctx,
            message=body.message,
            classroom_id=body.classroom_id,
            academic_year_id=body.academic_year_id,
            conversation_id=body.conversation_id,
            history=history,
            page_context=body.page_context,
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc

    await db.commit()

    return ChatResponse(
        reply=result["reply"],
        mode=result["mode"],
        source=result["source"],
        intent_key=result.get("intent_key"),
        confidence=result.get("confidence"),
        llm_provider=result.get("llm_provider"),
        conversation_id=UUID(result["conversation_id"]),
        suggestions=result.get("suggestions") or [],
        context=result.get("context"),
    )
