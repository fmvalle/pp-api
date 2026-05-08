import uuid
from typing import Any

import pytest

from app.core.deps import AuthContext
from app.v1 import catalog_router


class _DummyDb:
    pass


@pytest.mark.asyncio
async def test_filters_academic_years_includes_active_only_clause(monkeypatch):
    captured: dict[str, Any] = {}

    async def fake_fetch_all(_db, sql, params):
        captured["sql"] = sql
        captured["params"] = params
        return [{"id": str(uuid.uuid4()), "year": 2026, "is_active": True}]

    monkeypatch.setattr(catalog_router, "fetch_all", fake_fetch_all)

    ctx = AuthContext(
        session_id=uuid.uuid4(),
        person_id=uuid.uuid4(),
        active_profile_id=uuid.uuid4(),
        role="admin",
        school_id=None,
    )
    await catalog_router.filters_academic_years_v1(
        ctx,
        db=_DummyDb(),  # type: ignore[arg-type]
        active_only=True,
    )
    assert "is_active = true" in captured["sql"].lower()


@pytest.mark.asyncio
async def test_filters_academic_years_omits_filter_when_active_only_false(monkeypatch):
    captured: dict[str, Any] = {}

    async def fake_fetch_all(_db, sql, params):
        captured["sql"] = sql
        return []

    monkeypatch.setattr(catalog_router, "fetch_all", fake_fetch_all)

    ctx = AuthContext(
        session_id=uuid.uuid4(),
        person_id=uuid.uuid4(),
        active_profile_id=uuid.uuid4(),
        role="admin",
        school_id=None,
    )
    await catalog_router.filters_academic_years_v1(
        ctx,
        db=_DummyDb(),  # type: ignore[arg-type]
        active_only=False,
    )
    lowered = captured["sql"].lower()
    assert "is_active = true" not in lowered
