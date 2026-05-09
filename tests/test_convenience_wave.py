import uuid

import pytest

from app.core.deps import AuthContext
from app.v1 import assessments_router


def _ctx_admin() -> AuthContext:
    return AuthContext(
        session_id=uuid.uuid4(),
        person_id=uuid.uuid4(),
        active_profile_id=uuid.uuid4(),
        role="admin",
        school_id=None,
    )


@pytest.mark.asyncio
async def test_schedule_roster_returns_expected_keys(monkeypatch):
    cid = uuid.uuid4()
    sid = uuid.uuid4()

    async def fake_get_schedule(_schedule_id, _ctx, _db, academic_year_id=None):
        return {"id": sid, "classroom_id": cid, "assessment_id": uuid.uuid4()}

    async def fake_fetch_one(_db, sql, params):
        if "FROM classrooms" in sql:
            return {"id": cid, "name": "Turma A", "school_id": uuid.uuid4()}
        return None

    calls = {"n": 0}

    async def fake_fetch_all(_db, sql, _params):
        calls["n"] += 1
        if "classroom_teachers" in sql:
            return [{"teacher_id": uuid.uuid4()}]
        if "vw_classroom_students" in sql or "FROM classroom_students cs" in sql:
            return [{"student_id": uuid.uuid4()}]
        if "assessment_attendance_list" in sql:
            return []
        return []

    monkeypatch.setattr(assessments_router, "get_schedule_v1", fake_get_schedule)
    monkeypatch.setattr(assessments_router, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(assessments_router, "fetch_all", fake_fetch_all)

    out = await assessments_router.get_schedule_roster_v1(
        schedule_id=sid,
        ctx=_ctx_admin(),
        db=object(),  # type: ignore[arg-type]
    )
    assert set(out.keys()) == {"schedule", "classroom", "teachers", "students", "attendance"}
    assert out["schedule"]["classroom_id"] == cid
    assert calls["n"] == 3


@pytest.mark.asyncio
async def test_post_assessment_school_idempotent(monkeypatch):
    aid = uuid.uuid4()
    sch_id = uuid.uuid4()

    async def fake_get_assessment(*_a, **_k):
        return {"id": aid, "school_id": sch_id}

    existing = {"id": uuid.uuid4(), "assessment_id": aid, "school_id": sch_id}

    async def fake_fetch_one(_db, sql, params):
        if "FROM assessment_school" in sql and "LIMIT 1" in sql:
            return existing
        return None

    monkeypatch.setattr(assessments_router, "get_assessment_v1", fake_get_assessment)
    monkeypatch.setattr(assessments_router, "fetch_one", fake_fetch_one)

    body = assessments_router.AssessmentSchoolBody(school_id=sch_id)
    out = await assessments_router.post_assessment_school_v1(
        assessment_id=aid,
        body=body,
        ctx=_ctx_admin(),
        db=object(),  # type: ignore[arg-type]
    )
    assert out == existing
