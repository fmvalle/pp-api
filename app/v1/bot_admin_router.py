"""Administração do chatbot híbrido (bot_settings, bot_intents)."""

from __future__ import annotations

import json
import logging
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import AuthContext, get_auth_context
from app.db.session import get_db
from app.v1._scope import is_admin_like
from app.v1._sql import execute, fetch_all, fetch_one
from app.v1.bot_llm import (
    is_llm_configured,
    llm_config_source,
    load_active_llm_config,
    normalize_llm_config,
    verify_llm_connection,
)

router = APIRouter(prefix="/admin/bot", tags=["v1-bot-admin"])
logger = logging.getLogger(__name__)

Provider = Literal["groq", "grok", "openai", "anthropic", "gemini", "vertex"]
IntentType = Literal["local_static", "local_data"]
BotAudience = Literal["teacher", "platform_admin", "school_admin", "student", "all"]

VALID_AUDIENCES = frozenset({"teacher", "platform_admin", "school_admin", "student", "all"})

_PROVIDER_DEFAULTS: dict[str, str] = {
    "groq": "https://api.groq.com/openai/v1",
    "grok": "https://api.x.ai/v1",
    "openai": "https://api.openai.com/v1",
    "anthropic": "https://api.anthropic.com/v1",
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai",
    "vertex": "projects/SEU_PROJECT/locations/us-central1",
}


def _assert_platform_admin(ctx: AuthContext) -> None:
    if not is_admin_like(ctx.role):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Apenas administradores da plataforma")


def _mask_api_key(value: str | None) -> str | None:
    if not value:
        return None
    trimmed = value.strip()
    if len(trimmed) <= 4:
        return "****"
    return f"{'*' * (len(trimmed) - 4)}{trimmed[-4:]}"


def _serialize_settings(row: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    raw_key = str(out.pop("api_key", "") or "")
    out["api_key_masked"] = _mask_api_key(raw_key)
    out["has_api_key"] = bool(raw_key.strip())
    return out


async def _deactivate_other_settings(db: AsyncSession, keep_id: UUID | None = None) -> None:
    sql = "UPDATE bot_settings SET is_active = false, updated_at = now() WHERE is_active = true"
    params: dict[str, Any] = {}
    if keep_id is not None:
        sql += " AND id <> CAST(:keep_id AS uuid)"
        params["keep_id"] = str(keep_id)
    await execute(db, sql, params)


class BotSettingsCreate(BaseModel):
    provider: Provider
    model_name: str = Field(min_length=1, max_length=120)
    api_key: str = Field(default="", max_length=500)
    base_url: str | None = Field(default=None, max_length=300)
    is_active: bool = False


class BotSettingsPatch(BaseModel):
    provider: Provider | None = None
    model_name: str | None = Field(default=None, min_length=1, max_length=120)
    api_key: str | None = Field(default=None, max_length=500)
    base_url: str | None = Field(default=None, max_length=300)
    is_active: bool | None = None


class BotIntentCreate(BaseModel):
    intent_key: str = Field(min_length=2, max_length=80, pattern=r"^[a-z0-9_]+$")
    title: str = Field(min_length=2, max_length=200)
    patterns: list[str] = Field(min_length=1, max_length=50)
    response_template: str = Field(min_length=1, max_length=8000)
    intent_type: IntentType = "local_static"
    data_handler: str | None = Field(default=None, max_length=80)
    min_score: float = Field(default=85.0, ge=50.0, le=100.0)
    is_active: bool = True
    audiences: list[BotAudience] = Field(default_factory=lambda: ["teacher"], min_length=1, max_length=5)


class BotIntentPatch(BaseModel):
    title: str | None = Field(default=None, min_length=2, max_length=200)
    patterns: list[str] | None = Field(default=None, min_length=1, max_length=50)
    response_template: str | None = Field(default=None, min_length=1, max_length=8000)
    intent_type: IntentType | None = None
    data_handler: str | None = Field(default=None, max_length=80)
    min_score: float | None = Field(default=None, ge=50.0, le=100.0)
    is_active: bool | None = None
    audiences: list[BotAudience] | None = Field(default=None, min_length=1, max_length=5)


def _serialize_intent(row: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    patterns = out.get("patterns")
    if isinstance(patterns, str):
        out["patterns"] = json.loads(patterns)
    audiences = out.get("audiences")
    if isinstance(audiences, str):
        out["audiences"] = json.loads(audiences)
    return out


def _normalize_audiences(values: list[str]) -> list[str]:
    cleaned = [v.strip() for v in values if v.strip()]
    if not cleaned:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Informe ao menos uma audiência")
    invalid = [v for v in cleaned if v not in VALID_AUDIENCES]
    if invalid:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Audiências inválidas: {', '.join(invalid)}",
        )
    return cleaned


@router.get("/stats")
async def bot_admin_stats_v1(
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    _assert_platform_admin(ctx)
    row = await fetch_one(
        db,
        """
        SELECT
          (SELECT COUNT(*)::int FROM bot_conversations) AS conversations,
          (SELECT COUNT(*)::int FROM bot_messages) AS messages,
          (SELECT COUNT(*)::int FROM bot_messages WHERE source IN ('local', 'local_data')) AS local_messages,
          (SELECT COUNT(*)::int FROM bot_messages WHERE source = 'llm') AS llm_messages,
          (SELECT COUNT(*)::int FROM bot_intents WHERE is_active = true) AS active_intents,
          (SELECT COUNT(*)::int FROM bot_settings WHERE is_active = true) AS active_llm_configs
        """,
    )
    llm_config = await load_active_llm_config(db)
    llm_reachable, llm_error = (
        await verify_llm_connection(llm_config) if is_llm_configured(llm_config) else (False, None)
    )
    active_norm = normalize_llm_config(llm_config) if llm_config else None
    return {
        **(row or {}),
        "active_provider": active_norm.get("provider") if active_norm else None,
        "active_model": active_norm.get("model_name") if active_norm else None,
        "config_source": llm_config_source(llm_config),
        "llm_configured": is_llm_configured(llm_config),
        "llm_reachable": llm_reachable,
        "llm_error": llm_error,
    }


@router.get("/settings")
async def list_bot_settings_v1(
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    _assert_platform_admin(ctx)
    rows = await fetch_all(
        db,
        """
        SELECT id, provider, model_name, api_key, base_url, is_active, created_at, updated_at
        FROM bot_settings
        ORDER BY is_active DESC, updated_at DESC
        """,
    )
    return [_serialize_settings(row) for row in rows]


@router.post("/settings", status_code=status.HTTP_201_CREATED)
async def create_bot_settings_v1(
    body: BotSettingsCreate,
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    _assert_platform_admin(ctx)
    if body.is_active:
        await _deactivate_other_settings(db)

    base_url = body.base_url or _PROVIDER_DEFAULTS.get(body.provider)
    row = await fetch_one(
        db,
        """
        INSERT INTO bot_settings (provider, model_name, api_key, base_url, is_active)
        VALUES (:provider, :model_name, :api_key, :base_url, :is_active)
        RETURNING id, provider, model_name, api_key, base_url, is_active, created_at, updated_at
        """,
        {
            "provider": body.provider,
            "model_name": body.model_name,
            "api_key": body.api_key.strip(),
            "base_url": base_url,
            "is_active": body.is_active,
        },
    )
    await db.commit()
    logger.info("bot_admin.settings.create provider=%s by=%s", body.provider, ctx.active_profile_id)
    return _serialize_settings(row or {})


@router.patch("/settings/{settings_id}")
async def patch_bot_settings_v1(
    settings_id: UUID,
    body: BotSettingsPatch,
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    _assert_platform_admin(ctx)
    existing = await fetch_one(
        db,
        "SELECT * FROM bot_settings WHERE id = CAST(:id AS uuid)",
        {"id": str(settings_id)},
    )
    if not existing:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Configuração não encontrada")

    if body.is_active:
        await _deactivate_other_settings(db, keep_id=settings_id)

    sets = ["updated_at = now()"]
    params: dict[str, Any] = {"id": str(settings_id)}

    if body.provider is not None:
        sets.append("provider = :provider")
        params["provider"] = body.provider
    if body.model_name is not None:
        sets.append("model_name = :model_name")
        params["model_name"] = body.model_name
    if body.api_key is not None:
        sets.append("api_key = :api_key")
        params["api_key"] = body.api_key.strip()
    if body.base_url is not None:
        sets.append("base_url = :base_url")
        params["base_url"] = body.base_url.strip() or None
    if body.is_active is not None:
        sets.append("is_active = :is_active")
        params["is_active"] = body.is_active

    if len(sets) == 1:
        return _serialize_settings(existing)

    row = await fetch_one(
        db,
        f"""
        UPDATE bot_settings SET {', '.join(sets)}
        WHERE id = CAST(:id AS uuid)
        RETURNING id, provider, model_name, api_key, base_url, is_active, created_at, updated_at
        """,
        params,
    )
    await db.commit()
    return _serialize_settings(row or {})


@router.post("/settings/{settings_id}/activate")
async def activate_bot_settings_v1(
    settings_id: UUID,
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    _assert_platform_admin(ctx)
    existing = await fetch_one(
        db,
        "SELECT id, api_key FROM bot_settings WHERE id = CAST(:id AS uuid)",
        {"id": str(settings_id)},
    )
    if not existing:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Configuração não encontrada")
    if not str(existing.get("api_key") or "").strip():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Defina api_key antes de ativar")

    await _deactivate_other_settings(db, keep_id=settings_id)
    row = await fetch_one(
        db,
        """
        UPDATE bot_settings SET is_active = true, updated_at = now()
        WHERE id = CAST(:id AS uuid)
        RETURNING id, provider, model_name, api_key, base_url, is_active, created_at, updated_at
        """,
        {"id": str(settings_id)},
    )
    await db.commit()
    return _serialize_settings(row or {})


@router.delete("/settings/{settings_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_bot_settings_v1(
    settings_id: UUID,
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    _assert_platform_admin(ctx)
    existing = await fetch_one(
        db,
        "SELECT id, is_active FROM bot_settings WHERE id = CAST(:id AS uuid)",
        {"id": str(settings_id)},
    )
    if not existing:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Configuração não encontrada")
    await execute(
        db,
        "DELETE FROM bot_settings WHERE id = CAST(:id AS uuid)",
        {"id": str(settings_id)},
    )
    await db.commit()


@router.get("/intents")
async def list_bot_intents_v1(
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    _assert_platform_admin(ctx)
    rows = await fetch_all(
        db,
        """
        SELECT id, intent_key, title, patterns, response_template,
               intent_type, data_handler, min_score, is_active, audiences, created_at
        FROM bot_intents
        ORDER BY intent_key
        """,
    )
    return [_serialize_intent(row) for row in rows]


@router.post("/intents", status_code=status.HTTP_201_CREATED)
async def create_bot_intent_v1(
    body: BotIntentCreate,
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    _assert_platform_admin(ctx)
    patterns = [p.strip() for p in body.patterns if p.strip()]
    if not patterns:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Informe ao menos um padrão")

    existing = await fetch_one(
        db,
        "SELECT id FROM bot_intents WHERE intent_key = :key",
        {"key": body.intent_key},
    )
    if existing:
        raise HTTPException(status.HTTP_409_CONFLICT, "intent_key já existe")

    audiences = _normalize_audiences(list(body.audiences))

    row = await fetch_one(
        db,
        """
        INSERT INTO bot_intents (
          intent_key, title, patterns, response_template,
          intent_type, data_handler, min_score, is_active, audiences
        )
        VALUES (
          :intent_key, :title, CAST(:patterns AS jsonb), :response_template,
          :intent_type, :data_handler, :min_score, :is_active, CAST(:audiences AS text[])
        )
        RETURNING id, intent_key, title, patterns, response_template,
                  intent_type, data_handler, min_score, is_active, audiences, created_at
        """,
        {
            "intent_key": body.intent_key,
            "title": body.title,
            "patterns": json.dumps(patterns, ensure_ascii=False),
            "response_template": body.response_template,
            "intent_type": body.intent_type,
            "data_handler": body.data_handler,
            "min_score": body.min_score,
            "is_active": body.is_active,
            "audiences": audiences,
        },
    )
    await db.commit()
    return _serialize_intent(row or {})


@router.patch("/intents/{intent_id}")
async def patch_bot_intent_v1(
    intent_id: UUID,
    body: BotIntentPatch,
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    _assert_platform_admin(ctx)
    existing = await fetch_one(
        db,
        "SELECT * FROM bot_intents WHERE id = CAST(:id AS uuid)",
        {"id": str(intent_id)},
    )
    if not existing:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Intenção não encontrada")

    sets: list[str] = []
    params: dict[str, Any] = {"id": str(intent_id)}

    if body.title is not None:
        sets.append("title = :title")
        params["title"] = body.title
    if body.patterns is not None:
        patterns = [p.strip() for p in body.patterns if p.strip()]
        if not patterns:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Informe ao menos um padrão")
        sets.append("patterns = CAST(:patterns AS jsonb)")
        params["patterns"] = json.dumps(patterns, ensure_ascii=False)
    if body.response_template is not None:
        sets.append("response_template = :response_template")
        params["response_template"] = body.response_template
    if body.intent_type is not None:
        sets.append("intent_type = :intent_type")
        params["intent_type"] = body.intent_type
    if body.data_handler is not None:
        sets.append("data_handler = :data_handler")
        params["data_handler"] = body.data_handler or None
    if body.min_score is not None:
        sets.append("min_score = :min_score")
        params["min_score"] = body.min_score
    if body.is_active is not None:
        sets.append("is_active = :is_active")
        params["is_active"] = body.is_active
    if body.audiences is not None:
        sets.append("audiences = CAST(:audiences AS text[])")
        params["audiences"] = _normalize_audiences(list(body.audiences))

    if not sets:
        return _serialize_intent(existing)

    row = await fetch_one(
        db,
        f"""
        UPDATE bot_intents SET {', '.join(sets)}
        WHERE id = CAST(:id AS uuid)
        RETURNING id, intent_key, title, patterns, response_template,
                  intent_type, data_handler, min_score, is_active, audiences, created_at
        """,
        params,
    )
    await db.commit()
    return _serialize_intent(row or {})


@router.delete("/intents/{intent_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_bot_intent_v1(
    intent_id: UUID,
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    _assert_platform_admin(ctx)
    existing = await fetch_one(
        db,
        "SELECT id FROM bot_intents WHERE id = CAST(:id AS uuid)",
        {"id": str(intent_id)},
    )
    if not existing:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Intenção não encontrada")
    await execute(
        db,
        "DELETE FROM bot_intents WHERE id = CAST(:id AS uuid)",
        {"id": str(intent_id)},
    )
    await db.commit()
