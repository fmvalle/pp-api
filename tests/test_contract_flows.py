import uuid

import pytest
from fastapi import HTTPException

from app.core.deps import AuthContext
from app.v1 import exam_report_router


def _ctx(role: str = "teacher") -> AuthContext:
    return AuthContext(
        session_id=uuid.uuid4(),
        person_id=uuid.uuid4(),
        active_profile_id=uuid.uuid4(),
        role=role,
        school_id=uuid.uuid4(),
    )


@pytest.mark.asyncio
async def test_exam_next_question_success(monkeypatch):
    async def fake_scope(_db, _ctx):
        return {"is_admin_like": False, "effective_classroom_ids": []}

    q1 = str(uuid.uuid4())
    q2 = str(uuid.uuid4())
    qrows = [
        {"question_id": q1, "order_index": 1, "question": {"id": q1}},
        {"question_id": q2, "order_index": 2, "question": {"id": q2}},
    ]

    async def fake_load(_db, _aid, _sid):
        return qrows, {q1}

    async def fake_build(_db, _aid, qid, oidx):
        return {"id": str(qid), "description_html": "", "order": int(oidx or 0), "alternatives": []}

    monkeypatch.setattr(exam_report_router, "_load_questions_and_answered", fake_load)
    monkeypatch.setattr(exam_report_router, "_build_app_question", fake_build)
    monkeypatch.setattr(exam_report_router, "get_effective_classroom_scope", fake_scope)
    body = exam_report_router.NextQuestionBody(assessment_id=uuid.uuid4())
    res = await exam_report_router.exam_next_question_v1(body=body, ctx=_ctx("student"), db=object())
    assert res["completed"] is False
    assert res["finished"] is False
    assert res["next_question"]["question_id"] == q2
    assert res["remaining_count"] == 1
    assert res["last_answered_order"] == 1
    assert res["question"]["id"] == q2
    assert res["order_index"] == 2


@pytest.mark.asyncio
async def test_exam_next_question_save_returns_updated(monkeypatch):
    async def fake_scope(_db, _ctx):
        return {"is_admin_like": False, "effective_classroom_ids": []}

    q1 = str(uuid.uuid4())
    q2 = str(uuid.uuid4())
    qrows = [
        {"question_id": q1, "order_index": 1, "question": {"id": q1}},
        {"question_id": q2, "order_index": 2, "question": {"id": q2}},
    ]
    loads = [
        (qrows, set()),
        (qrows, {q1}),
    ]

    async def fake_load(_db, _aid, _sid):
        return loads.pop(0)

    upsert_calls: list[tuple] = []

    async def fake_upsert(db, **kwargs):
        upsert_calls.append((db, kwargs))
        return 1

    class DummyDb:
        async def commit(self):
            pass

    monkeypatch.setattr(exam_report_router, "_load_questions_and_answered", fake_load)
    monkeypatch.setattr(exam_report_router, "_upsert_question_student_response", fake_upsert)
    monkeypatch.setattr(exam_report_router, "get_effective_classroom_scope", fake_scope)
    aid = uuid.uuid4()
    body = exam_report_router.NextQuestionBody(
        assessment_id=aid,
        question_id=uuid.uuid4(),
        response_id=uuid.uuid4(),
    )
    res = await exam_report_router.exam_next_question_v1(body=body, ctx=_ctx("student"), db=DummyDb())
    assert res["status"] == "updated"
    assert res["question"] is None
    assert res["answered_count"] == 1
    assert len(upsert_calls) == 1


@pytest.mark.asyncio
async def test_exam_next_question_forbidden_for_other_student(monkeypatch):
    async def fake_fetch_all(_db, _sql, _params):
        return []

    monkeypatch.setattr(exam_report_router, "fetch_all", fake_fetch_all)
    ctx = _ctx("student")
    body = exam_report_router.NextQuestionBody(assessment_id=uuid.uuid4(), student_id=uuid.uuid4())
    with pytest.raises(HTTPException) as exc:
        await exam_report_router.exam_next_question_v1(body=body, ctx=ctx, db=object())
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_exam_next_question_forbidden_for_classroom_out_of_scope(monkeypatch):
    async def fake_fetch_all(_db, _sql, _params):
        return []

    async def fake_scope(_db, _ctx):
        return {"is_admin_like": False, "effective_classroom_ids": ["in-scope"]}

    monkeypatch.setattr(exam_report_router, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(exam_report_router, "get_effective_classroom_scope", fake_scope)
    ctx = _ctx("teacher")
    body = exam_report_router.NextQuestionBody(
        assessment_id=uuid.uuid4(),
        student_id=ctx.active_profile_id,
        classroom_id=uuid.uuid4(),
    )
    with pytest.raises(HTTPException) as exc:
        await exam_report_router.exam_next_question_v1(body=body, ctx=ctx, db=object())
    assert exc.value.status_code == 403
