"""Controle de acesso ao Avaliador por escola."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import AuthContext
from app.v1._scope import is_admin_like
from app.v1._sql import fetch_one


async def resolve_school_id_for_bot(
    db: AsyncSession,
    ctx: AuthContext,
    *,
    classroom_id: UUID | None = None,
    page_context: dict[str, Any] | None = None,
) -> UUID | None:
    if page_context:
        raw = page_context.get("school_id")
        if raw:
            return UUID(str(raw))
    if classroom_id:
        row = await fetch_one(
            db,
            "SELECT school_id FROM classrooms WHERE id = CAST(:id AS uuid)",
            {"id": str(classroom_id)},
        )
        if row and row.get("school_id"):
            return row["school_id"]
    return ctx.school_id


async def is_bot_enabled_for_school(db: AsyncSession, school_id: UUID | None) -> bool:
    if not school_id:
        return False
    row = await fetch_one(
        db,
        "SELECT bot_enabled FROM schools WHERE id = CAST(:id AS uuid)",
        {"id": str(school_id)},
    )
    return bool(row and row.get("bot_enabled"))


async def assert_bot_enabled(
    db: AsyncSession,
    ctx: AuthContext,
    *,
    classroom_id: UUID | None = None,
    page_context: dict[str, Any] | None = None,
) -> None:
    """Admins de plataforma ignoram o flag; professores dependem da escola."""
    if is_admin_like(ctx.role):
        return
    school_id = await resolve_school_id_for_bot(
        db,
        ctx,
        classroom_id=classroom_id,
        page_context=page_context,
    )
    if not await is_bot_enabled_for_school(db, school_id):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "O Avaliador não está habilitado para esta escola. "
            "Solicite ao administrador da plataforma.",
        )


async def bot_enabled_for_context(
    db: AsyncSession,
    ctx: AuthContext,
    *,
    classroom_id: UUID | None = None,
) -> bool:
    if is_admin_like(ctx.role):
        return True
    school_id = await resolve_school_id_for_bot(db, ctx, classroom_id=classroom_id)
    return await is_bot_enabled_for_school(db, school_id)
