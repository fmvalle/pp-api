"""Envelope do portal do professor: dashboard com métricas + agenda com academic_year_id."""

import uuid

import pytest

from app.core.deps import AuthContext
from app.v1 import exam_report_router


def _ctx_teacher() -> AuthContext:
    return AuthContext(
        session_id=uuid.uuid4(),
        person_id=uuid.uuid4(),
        active_profile_id=uuid.uuid4(),
        role="teacher",
        school_id=uuid.uuid4(),
    )


@pytest.mark.asyncio
async def test_teacher_dashboard_summary_includes_aggregate_keys(monkeypatch):
    ay = uuid.uuid4()

    async def fake_resolve(_db, _academic_year_id):
        return ay

    async def fake_scope(_db, _ctx):
        return {"is_admin_like": True}

    fetch_one_calls: list[str] = []

    async def fake_fetch_one(_db, sql, _params):
        fetch_one_calls.append(sql)
        if "AS total FROM" in sql and "COUNT(*)" in sql:
            return {"total": 1}
        if "total_assessments" in sql:
            return {
                "total_assessments": 4,
                "active_assessments": 2,
                "completed_assessments": 3,
                "total_students": 12,
                "total_classrooms": 3,
            }
        return {}

    async def fake_fetch_all(_db, sql, _params):
        return [
            {
                "assessment_id": str(uuid.uuid4()),
                "classroom_id": str(uuid.uuid4()),
                "pending": 1,
                "completed": 0,
                "schedule_id": str(uuid.uuid4()),
            }
        ]

    monkeypatch.setattr(exam_report_router, "resolve_academic_year_id", fake_resolve)
    monkeypatch.setattr(exam_report_router, "get_effective_classroom_scope", fake_scope)
    monkeypatch.setattr(exam_report_router, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(exam_report_router, "fetch_all", fake_fetch_all)

    out = await exam_report_router.teacher_dashboard_v1(
        ctx=_ctx_teacher(),
        db=object(),
        academic_year_id=None,
        classroom_id=None,
        pg=exam_report_router.PageArgs(1, 50),
    )
    assert out["academic_year_id"] == str(ay)
    assert out["page"] == 1
    assert out["total"] == 1
    assert len(out["items"]) == 1
    assert out["total_students"] == 12
    assert out["total_classrooms"] == 3
    assert out["active_assessments"] == 2
    assert out["completed_assessments"] == 3
    assert out["total_assessments"] == 4


@pytest.mark.asyncio
async def test_agenda_returns_academic_year_envelope(monkeypatch):
    ay = uuid.uuid4()

    async def fake_resolve(_db, _academic_year_id):
        return ay

    async def fake_scope(_db, _ctx):
        return {
            "is_admin_like": False,
            "effective_classroom_ids": [uuid.uuid4()],
        }

    async def fake_fetch_one(_db, sql, _params):
        return {"total": 0}

    async def fake_fetch_all(_db, sql, _params):
        return []

    monkeypatch.setattr(exam_report_router, "resolve_academic_year_id", fake_resolve)
    monkeypatch.setattr(exam_report_router, "get_effective_classroom_scope", fake_scope)
    monkeypatch.setattr(exam_report_router, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(exam_report_router, "fetch_all", fake_fetch_all)

    out = await exam_report_router.agenda_v1(
        ctx=_ctx_teacher(),
        db=object(),
        academic_year_id=None,
        start_date=None,
        end_date=None,
        classroom_id=None,
        pg=exam_report_router.PageArgs(1, 20),
    )
    assert out["academic_year_id"] == str(ay)
    assert "items" in out
