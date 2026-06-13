"""Macro avaliações: resolução de contexto, listagem e consolidações."""

import uuid

import pytest
from fastapi import HTTPException

from app.core.deps import AuthContext
from app.v1 import exam_report_router, macro_report


def _ctx(role: str = "teacher") -> AuthContext:
    return AuthContext(
        session_id=uuid.uuid4(),
        person_id=uuid.uuid4(),
        active_profile_id=uuid.uuid4(),
        role=role,
        school_id=None,
    )


@pytest.mark.asyncio
async def test_resolve_macro_scope_macro_id(monkeypatch):
    macro_id = uuid.uuid4()
    cid = uuid.uuid4()

    async def fake_fetch_one(_db, sql, params=None):
        if "FROM macro_assessments" in sql:
            return {"id": params["id"], "title": "Avaliação Bimestral"}
        return None

    async def fake_fetch_all(_db, sql, params=None):
        if "FROM assessments a" in sql:
            return [
                {"id": "a1", "title": "Caderno 1", "description": None, "type": "exam"},
                {"id": "a2", "title": "Caderno 2", "description": None, "type": "exam"},
            ]
        return []

    monkeypatch.setattr(macro_report, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(macro_report, "fetch_all", fake_fetch_all)

    scope = await macro_report.resolve_macro_scope(
        object(), route_id=macro_id, classroom_id=cid  # type: ignore[arg-type]
    )
    assert scope["is_macro"] is True
    assert scope["macro_id"] == str(macro_id)
    assert scope["assessment_ids"] == ["a1", "a2"]
    assert scope["macro_title"] == "Avaliação Bimestral"


@pytest.mark.asyncio
async def test_resolve_macro_scope_legacy_assessment_without_macro(monkeypatch):
    aid = uuid.uuid4()
    cid = uuid.uuid4()

    async def fake_fetch_one(_db, sql, params=None):
        if "FROM macro_assessments" in sql:
            return None  # não é macro
        if "FROM assessments" in sql:
            return {
                "id": params["id"],
                "title": "Prova Avulsa",
                "description": None,
                "type": "exam",
                "macro_assessment_id": None,
            }
        return None

    async def fake_fetch_all(_db, sql, params=None):
        # caderno único (sem macro) → retorna o próprio assessment
        return [{"id": str(aid), "title": "Prova Avulsa", "description": None, "type": "exam"}]

    monkeypatch.setattr(macro_report, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(macro_report, "fetch_all", fake_fetch_all)

    scope = await macro_report.resolve_macro_scope(
        object(), route_id=aid, classroom_id=cid  # type: ignore[arg-type]
    )
    assert scope["is_macro"] is False
    assert scope["macro_id"] is None
    assert scope["assessment_ids"] == [str(aid)]


@pytest.mark.asyncio
async def test_resolve_macro_scope_not_found_404(monkeypatch):
    async def fake_fetch_one(_db, _sql, _params=None):
        return None

    monkeypatch.setattr(macro_report, "fetch_one", fake_fetch_one)

    with pytest.raises(HTTPException) as ei:
        await macro_report.resolve_macro_scope(
            object(), route_id=uuid.uuid4(), classroom_id=uuid.uuid4()  # type: ignore[arg-type]
        )
    assert ei.value.status_code == 404


@pytest.mark.asyncio
async def test_resolve_macro_scope_no_scheduled_caderno_404(monkeypatch):
    macro_id = uuid.uuid4()

    async def fake_fetch_one(_db, sql, params=None):
        if "FROM macro_assessments" in sql:
            return {"id": params["id"], "title": "Macro Vazia"}
        return None

    async def fake_fetch_all(_db, _sql, _params=None):
        return []  # nenhum caderno agendado para a turma

    monkeypatch.setattr(macro_report, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(macro_report, "fetch_all", fake_fetch_all)

    with pytest.raises(HTTPException) as ei:
        await macro_report.resolve_macro_scope(
            object(), route_id=macro_id, classroom_id=uuid.uuid4()  # type: ignore[arg-type]
        )
    assert ei.value.status_code == 404


@pytest.mark.asyncio
async def test_teacher_macro_assessments_listing_shapes_items(monkeypatch):
    ay = uuid.uuid4()
    macro_id = uuid.uuid4()
    cid = uuid.uuid4()

    async def fake_resolve(_db, _q):
        return ay

    async def fake_scope(_db, _ctx):
        return {"is_admin_like": False, "effective_classroom_ids": []}

    async def fake_fetch_all(_db, sql, _params=None):
        if "vw_macro_assessment_summary" in sql:
            return [
                {
                    "macro_assessment_id": str(macro_id),
                    "classroom_id": str(cid),
                    "school_id": str(uuid.uuid4()),
                    "title": "Avaliação Bimestral",
                    "description": "desc",
                    "type": "exam",
                    "year": 2026,
                    "is_active": True,
                    "pending": 10,
                    "completed": 20,
                    "did_not_deliver": 2,
                }
            ]
        return []  # sem cadernos avulsos

    monkeypatch.setattr(exam_report_router, "resolve_academic_year_id", fake_resolve)
    monkeypatch.setattr(exam_report_router, "get_effective_classroom_scope", fake_scope)
    monkeypatch.setattr(exam_report_router, "fetch_all", fake_fetch_all)

    out = await exam_report_router.teacher_macro_assessments_v1(
        ctx=_ctx("teacher"),
        db=object(),  # type: ignore[arg-type]
        academic_year_id=None,
        classroom_id=None,
    )
    assert len(out["items"]) == 1
    item = out["items"][0]
    assert item["id"] == str(macro_id)
    assert item["macroAssessmentId"] == str(macro_id)
    assert item["isMacro"] is True
    assert item["pending"] == 10
    assert item["completed"] == 20
    assert item["didNotDeliver"] == 2
    assert item["isActive"] is True


@pytest.mark.asyncio
async def test_teacher_macro_assessments_includes_legacy_standalone(monkeypatch):
    ay = uuid.uuid4()
    aid = uuid.uuid4()
    cid = uuid.uuid4()

    async def fake_resolve(_db, _q):
        return ay

    async def fake_scope(_db, _ctx):
        return {"is_admin_like": False, "effective_classroom_ids": []}

    async def fake_fetch_all(_db, sql, _params=None):
        if "vw_macro_assessment_summary" in sql:
            return []  # nenhuma macro vinculada ainda
        # caderno avulso (sem macro)
        return [
            {
                "assessment_id": str(aid),
                "classroom_id": str(cid),
                "school_id": str(uuid.uuid4()),
                "title": "Prova Avulsa",
                "description": None,
                "type": "exam",
                "year": 2026,
                "is_active": True,
                "pending": 3,
                "completed": 5,
                "did_not_deliver": 1,
            }
        ]

    monkeypatch.setattr(exam_report_router, "resolve_academic_year_id", fake_resolve)
    monkeypatch.setattr(exam_report_router, "get_effective_classroom_scope", fake_scope)
    monkeypatch.setattr(exam_report_router, "fetch_all", fake_fetch_all)

    out = await exam_report_router.teacher_macro_assessments_v1(
        ctx=_ctx("teacher"),
        db=object(),  # type: ignore[arg-type]
        academic_year_id=None,
        classroom_id=None,
    )
    assert len(out["items"]) == 1
    item = out["items"][0]
    assert item["id"] == str(aid)
    assert item["isMacro"] is False
    assert item["macroAssessmentId"] is None


@pytest.mark.asyncio
async def test_macro_students_report_includes_assessment_title(monkeypatch):
    cid = uuid.uuid4()
    sid = str(uuid.uuid4())

    async def fake_fetch_all(_db, sql, _params=None):
        if "FROM assessment_results" in sql:
            return [
                {
                    "student_id": sid,
                    "full_name": "Aluno Teste",
                    "assessment_id": "a1",
                    "assessment_title": "Caderno 1",
                    "status": "submitted",
                    "score": 8.0,
                    "submitted_at": None,
                }
            ]
        if "vw_assessment_component_results" in sql:
            return [{"student_id": sid, "assessment_id": "a1", "tq": 10, "ca": 8}]
        if "FROM questions_assessments" in sql:
            return [{"assessment_id": "a1", "n": 12}]
        return []

    monkeypatch.setattr(macro_report, "fetch_all", fake_fetch_all)

    out = await macro_report.macro_students_report(
        object(), assessment_ids=["a1"], classroom_id=cid  # type: ignore[arg-type]
    )
    assert len(out["items"]) == 1
    row = out["items"][0]
    assert row["assessmentTitle"] == "Caderno 1"
    assert row["correctAnswers"] == 8
    assert row["totalQuestions"] == 12  # base de itens
    assert row["accuracyPercentage"] == pytest.approx(66.7, abs=0.1)
