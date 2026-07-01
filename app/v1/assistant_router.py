"""Assistente pedagógico do professor (Avaliador) — compatível com /v1/chat."""

from __future__ import annotations

import logging
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import AuthContext, get_auth_context
from app.db.session import get_db
from app.v1._scope import can_use_assistant, resolve_bot_audience
from app.v1.bot_llm import (
    is_llm_configured,
    llm_config_source,
    load_active_llm_config,
    normalize_llm_config,
    verify_llm_connection,
)
from app.v1.bot_local import load_active_intents
from app.v1.bot_service import process_chat

router = APIRouter(tags=["v1-assistant"])
logger = logging.getLogger(__name__)


class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=4000)


class AssistantChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    conversation_id: UUID | None = None
    classroom_id: UUID | None = None
    academic_year_id: UUID | None = None
    history: list[ChatMessage] = Field(default_factory=list, max_length=20)
    page_context: dict[str, Any] | None = None


class AssistantChatResponse(BaseModel):
    reply: str
    mode: Literal["local", "llm", "stub", "openai"] = "stub"
    source: Literal["local", "local_data", "llm", "stub"] = "stub"
    intent_key: str | None = None
    confidence: float | None = None
    llm_provider: str | None = None
    conversation_id: UUID | None = None
    suggestions: list[str] = Field(default_factory=list)
    context: dict[str, Any] | None = None


class AssistantStatusResponse(BaseModel):
    mode: Literal["local", "llm", "stub", "openai"]
    llm_enabled: bool
    local_intents: int = 0
    active_provider: str | None = None
    config_source: Literal["db", "env", "none"] = "none"
    llm_reachable: bool = False
    llm_error: str | None = None


async def _assert_assistant(ctx: AuthContext) -> None:
    if not can_use_assistant(ctx.role):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Apenas professor ou administrador da plataforma",
        )


def _legacy_mode(mode: str, provider: str | None) -> Literal["local", "llm", "stub", "openai"]:
    if mode == "llm" and provider == "openai":
        return "openai"
    if mode in ("local", "llm", "stub"):
        return mode  # type: ignore[return-value]
    return "stub"


@router.get("/teacher/assistant/status", response_model=AssistantStatusResponse)
async def teacher_assistant_status_v1(
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    await _assert_assistant(ctx)
    audience = resolve_bot_audience(ctx.role)
    intents = await load_active_intents(db, audience=audience)
    config = await load_active_llm_config(db)
    configured = is_llm_configured(config)
    source = llm_config_source(config)
    reachable, llm_error = await verify_llm_connection(config) if configured else (False, None)
    active = normalize_llm_config(config) if configured and config else None
    provider = str(active["provider"]) if active else None
    mode: Literal["local", "llm", "stub", "openai"] = "llm" if configured and reachable else "local"
    if configured and provider == "openai":
        mode = "openai"
    return AssistantStatusResponse(
        mode=mode,
        llm_enabled=configured,
        local_intents=len(intents),
        active_provider=provider,
        config_source=source if source in ("db", "env") else "none",
        llm_reachable=reachable,
        llm_error=llm_error,
    )


@router.post("/teacher/assistant/chat", response_model=AssistantChatResponse)
async def teacher_assistant_chat_v1(
    body: AssistantChatRequest,
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Alias legado do POST /v1/chat."""
    await _assert_assistant(ctx)
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

    provider = result.get("llm_provider")
    mode = _legacy_mode(str(result["mode"]), provider)

    return AssistantChatResponse(
        reply=result["reply"],
        mode=mode,
        source=result["source"],
        intent_key=result.get("intent_key"),
        confidence=result.get("confidence"),
        llm_provider=provider,
        conversation_id=UUID(result["conversation_id"]),
        suggestions=result.get("suggestions") or [],
        context=result.get("context"),
    )
