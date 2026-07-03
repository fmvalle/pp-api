"""Helpers mínimos para SQL assíncrono (repositório leve por domínio)."""

from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def fetch_all(db: AsyncSession, sql: str, params: dict[str, Any] | None = None) -> list[dict]:
    res = await db.execute(text(sql), params or {})
    return [dict(r) for r in res.mappings().all()]


async def fetch_one(db: AsyncSession, sql: str, params: dict[str, Any] | None = None) -> dict | None:
    res = await db.execute(text(sql), params or {})
    row = res.mappings().first()
    return dict(row) if row else None


async def execute(db: AsyncSession, sql: str, params: dict[str, Any] | None = None) -> None:
    await db.execute(text(sql), params or {})


async def execute_many(
    db: AsyncSession, sql: str, params_list: list[dict[str, Any]]
) -> None:
    if not params_list:
        return
    await db.execute(text(sql), params_list)
