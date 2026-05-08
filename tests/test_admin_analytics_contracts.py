"""Contratos estáveis: desempenho do aluno e engagement admin (lista por dia)."""

import uuid

import pytest
from fastapi import HTTPException

from app.core.deps import AuthContext
from app.v1 import assessments_router, exam_report_router


def _ctx_admin() -> AuthContext:
    return AuthContext(
        session_id=uuid.uuid4(),
        person_id=uuid.uuid4(),
        active_profile_id=uuid.uuid4(),
        role="admin",
        school_id=None,
    )


@pytest.mark.asyncio
async def test_student_performance_v1_contract(monkeypatch):
    sid = uuid.uuid4()
    ay = uuid.uuid4()

    async def fake_resolve(_db, _academic_year_id):
        return ay

    async def fake_rows(_db, _ctx, student_id, effective_ay):
        assert str(student_id) == str(sid)
        assert str(effective_ay) == str(ay)
        return [
            {"status": "graded", "score": 8.0, "type": "homework"},
            {"status": "graded", "score": 10.0, "type": "homework"},
            {"status": "pending", "score": None, "type": "exam"},
        ]

    monkeypatch.setattr(assessments_router, "resolve_academic_year_id", fake_resolve)
    monkeypatch.setattr(assessments_router, "_student_assessment_summary_rows", fake_rows)

    out = await assessments_router.student_performance_v1(
        student_id=sid,
        ctx=_ctx_admin(),
        db=object(),
        academic_year_id=None,
        include_items=False,
    )
    assert out["total_assessments"] == 3
    assert out["completed_assessments"] == 2
    assert out["average_score"] == 9.0
    assert len(out["by_type"]) == 2
    assert out["academic_year_id"] == str(ay)


@pytest.mark.asyncio
async def test_admin_engagement_weekday_returns_list(monkeypatch):
    async def fake_resolve(_db, _academic_year_id):
        return uuid.uuid4()

    async def fake_fetch_all(_db, sql, _params):
        if "EXTRACT(DOW" in sql:
            return [{"dow": 1, "count": 5}, {"dow": 3, "count": 2}]
        return []

    monkeypatch.setattr(exam_report_router, "resolve_academic_year_id", fake_resolve)
    monkeypatch.setattr(exam_report_router, "fetch_all", fake_fetch_all)

    out = await exam_report_router.admin_engagement_v1(
        ctx=_ctx_admin(),
        db=object(),
        academic_year_id=None,
        school_id=None,
        series="weekday",
    )
    assert isinstance(out, list)
    assert len(out) == 7
    labels = [r["day"] for r in out]
    assert labels[0] == "Dom" and labels[1] == "Seg"


@pytest.mark.asyncio
async def test_admin_engagement_forbidden_non_admin(monkeypatch):
    ctx = AuthContext(
        session_id=uuid.uuid4(),
        person_id=uuid.uuid4(),
        active_profile_id=uuid.uuid4(),
        role="teacher",
        school_id=uuid.uuid4(),
    )
    with pytest.raises(HTTPException) as exc:
        await exam_report_router.admin_engagement_v1(
            ctx=ctx,
            db=object(),
            academic_year_id=None,
            school_id=None,
            series="weekday",
        )
    assert exc.value.status_code == 403
