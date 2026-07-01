import uuid
from typing import Any

import pytest
from fastapi import HTTPException

from app.core.deps import AuthContext
from app.v1 import catalog_router


class _DummyDb:
    pass


def _admin_ctx() -> AuthContext:
    return AuthContext(
        session_id=uuid.uuid4(),
        person_id=uuid.uuid4(),
        active_profile_id=uuid.uuid4(),
        role="platform_admin",
        school_id=None,
    )


def _teacher_ctx() -> AuthContext:
    return AuthContext(
        session_id=uuid.uuid4(),
        person_id=uuid.uuid4(),
        active_profile_id=uuid.uuid4(),
        role="teacher",
        school_id=uuid.uuid4(),
    )


@pytest.mark.asyncio
async def test_create_academic_year_requires_admin(monkeypatch):
    async def fake_commit(_self):
        return None

    monkeypatch.setattr(_DummyDb, "commit", fake_commit, raising=False)

    with pytest.raises(HTTPException) as exc:
        await catalog_router.create_academic_year_v1(
            catalog_router.AcademicYearCreate(year=2026, is_primary=True),
            _teacher_ctx(),
            _DummyDb(),  # type: ignore[arg-type]
        )
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_create_academic_year_clears_other_primaries(monkeypatch):
    calls: list[tuple[str, dict[str, Any]]] = []

    async def fake_execute(_db, sql, params):
        calls.append((sql, params))

    async def fake_fetch_one(_db, sql, params):
        if "INSERT INTO academic_years" in sql:
            return {"id": str(uuid.uuid4()), "year": params["year"], "is_primary": True}
        return None

    async def fake_commit(_self):
        return None

    monkeypatch.setattr(catalog_router, "execute", fake_execute)
    monkeypatch.setattr(catalog_router, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(_DummyDb, "commit", fake_commit, raising=False)

    row = await catalog_router.create_academic_year_v1(
        catalog_router.AcademicYearCreate(year=2027, is_primary=True),
        _admin_ctx(),
        _DummyDb(),  # type: ignore[arg-type]
    )
    assert row["year"] == 2027
    assert any("is_primary = false" in sql for sql, _ in calls)


@pytest.mark.asyncio
async def test_delete_academic_year_blocks_when_classrooms_exist(monkeypatch):
    ay_id = uuid.uuid4()

    async def fake_fetch_one(_db, sql, params):
        if "FROM academic_years" in sql and "classrooms" not in sql:
            return {"id": str(ay_id)}
        if "FROM classrooms" in sql:
            return {"x": 1}
        return None

    monkeypatch.setattr(catalog_router, "fetch_one", fake_fetch_one)

    with pytest.raises(HTTPException) as exc:
        await catalog_router.delete_academic_year_v1(
            ay_id,
            _admin_ctx(),
            _DummyDb(),  # type: ignore[arg-type]
        )
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_create_grade_validates_segment(monkeypatch):
    seg_id = uuid.uuid4()

    async def fake_fetch_one(_db, sql, params):
        if "FROM segments" in sql:
            return None
        return None

    monkeypatch.setattr(catalog_router, "fetch_one", fake_fetch_one)

    with pytest.raises(HTTPException) as exc:
        await catalog_router.create_grade_v1(
            catalog_router.GradeCreate(segment_id=seg_id, name="6º ano"),
            _admin_ctx(),
            _DummyDb(),  # type: ignore[arg-type]
        )
    assert exc.value.status_code == 404
