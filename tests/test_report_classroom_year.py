"""Cenários de ano letivo / escopo em GET /v1/reports/classrooms/{id}."""

import uuid

import pytest
from fastapi import HTTPException

from app.core.deps import AuthContext
from app.v1 import exam_report_router


class _DummyDb:
    pass


@pytest.mark.asyncio
async def test_report_classroom_academic_year_mismatch_returns_400(monkeypatch):
    cid = uuid.uuid4()
    ay_req = uuid.uuid4()
    ay_class = uuid.uuid4()

    async def fake_resolve(_db, _q):
        return ay_req

    async def fake_load(_db, _classroom_id):
        return {"id": str(cid), "school_id": str(uuid.uuid4()), "academic_year_id": str(ay_class)}

    async def fake_assert(*_a, **_k):
        return None

    monkeypatch.setattr(exam_report_router, "resolve_academic_year_id", fake_resolve)
    monkeypatch.setattr(exam_report_router, "load_classroom_row_by_id", fake_load)
    monkeypatch.setattr(exam_report_router, "assert_actor_can_read_classroom", fake_assert)

    ctx = AuthContext(
        session_id=uuid.uuid4(),
        person_id=uuid.uuid4(),
        active_profile_id=uuid.uuid4(),
        role="teacher",
        school_id=None,
    )
    with pytest.raises(HTTPException) as ei:
        await exam_report_router.report_classroom_v1(
            classroom_id=cid,
            ctx=ctx,
            db=_DummyDb(),  # type: ignore[arg-type]
            academic_year_id=None,
            assessment_id=None,
        )
    assert ei.value.status_code == 400


@pytest.mark.asyncio
async def test_report_classroom_unknown_id_returns_404(monkeypatch):
    cid = uuid.uuid4()
    ay = uuid.uuid4()

    async def fake_resolve(_db, _q):
        return ay

    async def fake_load(_db, _classroom_id):
        return None

    monkeypatch.setattr(exam_report_router, "resolve_academic_year_id", fake_resolve)
    monkeypatch.setattr(exam_report_router, "load_classroom_row_by_id", fake_load)

    ctx = AuthContext(
        session_id=uuid.uuid4(),
        person_id=uuid.uuid4(),
        active_profile_id=uuid.uuid4(),
        role="admin",
        school_id=None,
    )
    with pytest.raises(HTTPException) as ei:
        await exam_report_router.report_classroom_v1(
            classroom_id=cid,
            ctx=ctx,
            db=_DummyDb(),  # type: ignore[arg-type]
            academic_year_id=None,
            assessment_id=None,
        )
    assert ei.value.status_code == 404
