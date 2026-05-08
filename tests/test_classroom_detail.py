"""Contrato GET turma + listagens com academic_year_id no envelope."""

import uuid

import pytest

from app.core.deps import AuthContext
from app.v1 import catalog_router


def _ctx_admin() -> AuthContext:
    return AuthContext(
        session_id=uuid.uuid4(),
        person_id=uuid.uuid4(),
        active_profile_id=uuid.uuid4(),
        role="admin",
        school_id=None,
    )


@pytest.mark.asyncio
async def test_get_classroom_returns_envelope(monkeypatch):
    cid = uuid.uuid4()
    ay = uuid.uuid4()

    async def fake_resolve(_db, _academic_year_id=None):
        return ay

    async def fake_fetch_scoped(_db, _ctx, _classroom_id, _academic_year_id=None):
        return {
            "id": cid,
            "school_id": uuid.uuid4(),
            "academic_year_id": ay,
            "grade_id": uuid.uuid4(),
            "code": "A1",
            "name": "Turma A",
            "shift": "M",
        }

    async def fake_fetch_one(_db, sql, params):
        if "vw_classroom_list" in sql:
            return {
                "classroom_id": cid,
                "students": 10,
                "teachers": 2,
                "grade": "1º ano",
                "segment": "EF",
                "academic_year": 2026,
            }
        return None

    monkeypatch.setattr(catalog_router, "resolve_academic_year_id", fake_resolve)
    monkeypatch.setattr(catalog_router, "_fetch_classroom_scoped", fake_fetch_scoped)
    monkeypatch.setattr(catalog_router, "fetch_one", fake_fetch_one)

    out = await catalog_router.get_classroom_v1(
        classroom_id=cid,
        ctx=_ctx_admin(),
        db=object(),  # type: ignore[arg-type]
        academic_year_id=None,
    )
    assert out["academic_year_id"] == str(ay)
    assert out["classroom"]["code"] == "A1"
    assert out["catalog"]["students"] == 10


@pytest.mark.asyncio
async def test_patch_classroom_uses_fetch_by_id_scoped(monkeypatch):
    """PATCH turma não deve depender do ano letivo primário (mutação por id)."""
    cid = uuid.uuid4()
    ay = uuid.uuid4()

    calls: list[str] = []

    async def fake_by_id(_db, _ctx, _classroom_id):
        calls.append("by_id")
        return {
            "id": cid,
            "school_id": uuid.uuid4(),
            "academic_year_id": ay,
            "grade_id": uuid.uuid4(),
            "code": "A1",
            "name": "Turma A",
            "shift": "M",
        }

    monkeypatch.setattr(catalog_router, "_fetch_classroom_by_id_scoped", fake_by_id)

    async def no_scoped_year(*_a, **_k):
        raise AssertionError("_fetch_classroom_scoped não deve ser usado no PATCH")

    monkeypatch.setattr(catalog_router, "_fetch_classroom_scoped", no_scoped_year)

    async def fake_fetch_one(db, sql, params):
        if "UPDATE classrooms" in sql:
            return {
                "id": cid,
                "school_id": uuid.uuid4(),
                "academic_year_id": ay,
                "grade_id": uuid.uuid4(),
                "code": "A1",
                "name": "Patched",
                "shift": "M",
            }
        return None

    monkeypatch.setattr(catalog_router, "fetch_one", fake_fetch_one)

    class _DummyDb:
        async def commit(self) -> None:
            return None

    body = catalog_router.ClassroomPatch(name="Patched")
    out = await catalog_router.patch_classroom_v1(
        classroom_id=cid,
        body=body,
        ctx=_ctx_admin(),
        db=_DummyDb(),  # type: ignore[arg-type]
    )
    assert calls == ["by_id"]
    assert out["name"] == "Patched"


@pytest.mark.asyncio
async def test_paged_response_with_academic_year_import():
    from app.v1._paging import paged_response_with_academic_year

    ay = uuid.uuid4()
    out = paged_response_with_academic_year(
        academic_year_id=ay,
        page=1,
        per_page=50,
        total=3,
        items=[{"a": 1}],
    )
    assert out["academic_year_id"] == str(ay)
    assert out["total"] == 3
    assert len(out["items"]) == 1
