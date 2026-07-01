"""Orquestração do chatbot híbrido + persistência."""

from __future__ import annotations

import logging
from typing import Any, Literal
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import AuthContext
from app.v1._academic_year import resolve_academic_year_id
from app.v1._sql import execute, fetch_all, fetch_one
from app.v1.assistant_context import load_assistant_context, suggestions_for_audience
from app.v1.bot_data import looks_like_factual_question, try_data_query
from app.v1.bot_page_context import try_page_context_data_query
from app.v1.bot_llm import (
    format_llm_error,
    generate_dynamic_llm_reply,
    is_llm_configured,
    load_active_llm_config,
)
from app.v1.bot_local import load_active_intents, match_local_intent, should_use_local_intent
from app.v1._scope import is_teacher_like, resolve_bot_audience

logger = logging.getLogger(__name__)

ChatMode = Literal["local", "llm", "stub"]
ChatSource = Literal["local", "local_data", "llm", "stub"]


async def ensure_conversation(
    db: AsyncSession,
    ctx: AuthContext,
    *,
    conversation_id: UUID | None,
    classroom_id: UUID | None,
    academic_year_id: UUID | None,
    first_message: str | None = None,
) -> UUID:
    if conversation_id:
        row = await fetch_one(
            db,
            """
            SELECT id FROM bot_conversations
            WHERE id = CAST(:id AS uuid) AND profile_id = CAST(:pid AS uuid)
            LIMIT 1
            """,
            {"id": str(conversation_id), "pid": str(ctx.active_profile_id)},
        )
        if not row:
            raise ValueError("Conversa não encontrada")
        return conversation_id

    effective_ay = await resolve_academic_year_id(db, academic_year_id)
    new_id = uuid4()
    title = (first_message or "Nova conversa").strip()[:80] or "Nova conversa"
    await execute(
        db,
        """
        INSERT INTO bot_conversations (id, profile_id, classroom_id, academic_year_id, title)
        VALUES (
          CAST(:id AS uuid),
          CAST(:pid AS uuid),
          CAST(:cid AS uuid),
          CAST(:ay AS uuid),
          :title
        )
        """,
        {
            "id": str(new_id),
            "pid": str(ctx.active_profile_id),
            "cid": str(classroom_id) if classroom_id else None,
            "ay": str(effective_ay),
            "title": title,
        },
    )
    return new_id


async def save_message(
    db: AsyncSession,
    *,
    conversation_id: UUID,
    role: str,
    content: str,
    source: ChatSource,
    intent_key: str | None = None,
    confidence: float | None = None,
    llm_provider: str | None = None,
) -> None:
    await execute(
        db,
        """
        INSERT INTO bot_messages (
          id, conversation_id, role, content, source, intent_key, confidence, llm_provider
        ) VALUES (
          CAST(:id AS uuid),
          CAST(:cid AS uuid),
          :role,
          :content,
          :source,
          :intent_key,
          :confidence,
          :provider
        )
        """,
        {
            "id": str(uuid4()),
            "cid": str(conversation_id),
            "role": role,
            "content": content,
            "source": source,
            "intent_key": intent_key,
            "confidence": confidence,
            "provider": llm_provider,
        },
    )
    await execute(
        db,
        """
        UPDATE bot_conversations
        SET updated_at = now()
        WHERE id = CAST(:cid AS uuid)
        """,
        {"cid": str(conversation_id)},
    )


async def list_conversations(
    db: AsyncSession,
    ctx: AuthContext,
    *,
    limit: int = 30,
) -> list[dict[str, Any]]:
    return await fetch_all(
        db,
        """
        SELECT
          c.id,
          c.title,
          c.classroom_id,
          c.academic_year_id,
          c.created_at,
          c.updated_at,
          (
            SELECT m.content
            FROM bot_messages m
            WHERE m.conversation_id = c.id
            ORDER BY m.created_at DESC
            LIMIT 1
          ) AS last_message
        FROM bot_conversations c
        WHERE c.profile_id = CAST(:pid AS uuid)
        ORDER BY c.updated_at DESC
        LIMIT :limit
        """,
        {"pid": str(ctx.active_profile_id), "limit": limit},
    )


async def list_messages(
    db: AsyncSession,
    ctx: AuthContext,
    conversation_id: UUID,
) -> list[dict[str, Any]]:
    owner = await fetch_one(
        db,
        """
        SELECT id FROM bot_conversations
        WHERE id = CAST(:id AS uuid) AND profile_id = CAST(:pid AS uuid)
        LIMIT 1
        """,
        {"id": str(conversation_id), "pid": str(ctx.active_profile_id)},
    )
    if not owner:
        raise ValueError("Conversa não encontrada")

    return await fetch_all(
        db,
        """
        SELECT role, content, source, intent_key, confidence, llm_provider, created_at
        FROM bot_messages
        WHERE conversation_id = CAST(:cid AS uuid)
        ORDER BY created_at ASC
        """,
        {"cid": str(conversation_id)},
    )


async def process_chat(
    db: AsyncSession,
    ctx: AuthContext,
    *,
    message: str,
    classroom_id: UUID | None,
    academic_year_id: UUID | None,
    conversation_id: UUID | None,
    history: list[dict[str, str]],
    page_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    context = await load_assistant_context(
        db,
        ctx,
        classroom_id=classroom_id,
        academic_year_id=academic_year_id,
    )
    if page_context:
        context["page_context"] = page_context
    audience = resolve_bot_audience(ctx.role)

    conv_id = await ensure_conversation(
        db,
        ctx,
        conversation_id=conversation_id,
        classroom_id=classroom_id,
        academic_year_id=academic_year_id,
        first_message=message,
    )
    await save_message(db, conversation_id=conv_id, role="user", content=message, source="local")

    mode: ChatMode = "stub"
    source: ChatSource = "stub"
    intent_key: str | None = None
    confidence: float | None = None
    llm_provider: str | None = None
    reply: str = ""

    # 0) Fatos do relatório aberto (page_context) — prioridade sobre SQL genérico e LLM
    if page_context and is_teacher_like(ctx.role):
        page_result = try_page_context_data_query(message, page_context)
        if page_result:
            reply = page_result.reply
            mode = "local"
            source = "local_data"
            intent_key = page_result.intent_key
            confidence = page_result.confidence

    # 1) Fatos com SQL determinístico (professor) — só quando há handler confiável
    data_question = looks_like_factual_question(message) and is_teacher_like(ctx.role)
    if mode == "stub" and data_question:
        data_result = await try_data_query(
            db,
            ctx,
            message=message,
            classroom_id=classroom_id,
            academic_year_id=academic_year_id,
            page_context=page_context,
        )
        if data_result and data_result.intent_key != "data_unavailable":
            reply = data_result.reply
            mode = "local"
            source = "local_data"
            intent_key = data_result.intent_key
            confidence = data_result.confidence

    # 2) Navegação estática (alta confiança, não interpretativa)
    if mode == "stub":
        intents = await load_active_intents(db, audience=audience)
        local = match_local_intent(message, intents)
        if local and should_use_local_intent(local):
            reply = local.reply
            mode = "local"
            source = "local"
            intent_key = local.intent_key
            confidence = local.confidence

    # 3) LLM — orientação pedagógica e análise com data_pack
    if mode == "stub":
        llm_config = await load_active_llm_config(db)
        if is_llm_configured(llm_config):
            try:
                reply, llm_provider = await generate_dynamic_llm_reply(
                    db,
                    user_message=message,
                    context=context,
                    history=history,
                )
                mode = "llm"
                source = "llm"
            except Exception as exc:
                err = format_llm_error(exc)
                logger.warning("bot.llm fallback: %s", err)
                reply = _fallback_stub(message, context)
                if llm_config:
                    reply += (
                        f"\n\n_(IA indisponível: {err}. "
                        "Verifique provedor/modelo em /admin/bot ou BOT_LLM_SOURCE=env.)_"
                    )
                mode = "stub"
                source = "stub"
        else:
            reply = _fallback_stub(message, context)
            mode = "stub"
            source = "stub"

    await save_message(
        db,
        conversation_id=conv_id,
        role="assistant",
        content=reply,
        source=source,
        intent_key=intent_key,
        confidence=confidence,
        llm_provider=llm_provider,
    )

    logger.info(
        "bot.chat mode=%s source=%s intent=%s teacher=%s conv=%s",
        mode,
        source,
        intent_key,
        ctx.active_profile_id,
        conv_id,
    )

    return {
        "reply": reply,
        "mode": mode,
        "source": source,
        "intent_key": intent_key,
        "confidence": confidence,
        "llm_provider": llm_provider,
        "conversation_id": str(conv_id),
        "suggestions": suggestions_for_audience(ctx.role),
        "context": context,
    }


def _fallback_stub(message: str, context: dict[str, Any]) -> str:
    from app.v1.assistant_context import build_stub_reply

    reply, _ = build_stub_reply(message, context)
    return reply
