"""Prova (Etapa 6), relatórios e agenda (Etapa 7)."""

import logging
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import AuthContext, get_auth_context
from app.db.session import get_db
from app.v1._academic_year import resolve_academic_year_id
from app.v1._paging import PageArgs, paged_response, paged_response_with_academic_year, pagination_params
from app.v1._scope import (
    get_descendant_school_ids,
    get_effective_classroom_scope,
    get_effective_school_scope,
    is_admin_like,
    is_teacher_like,
    resolve_admin_dashboard_school_ids,
)
from app.v1._sql import execute, fetch_all, fetch_one
from app.v1.directory_router import _student_scope_read
from app.v1.macro_report import (
    macro_components_report,
    macro_components_report_for_classrooms,
    macro_report_context,
    macro_students_report,
    macro_students_report_for_classrooms,
    resolve_macro_scope,
    resolve_macro_scope_school,
)
from app.v1.report_bundle import (
    assert_actor_can_read_classroom,
    assert_can_access_assessment_report_student,
    assert_can_read_schedule_report,
    assessment_pedagogical_report_bundle,
    assessment_schedule_report_bundle,
    classroom_assessment_report_envelope,
    load_classroom_row_by_id,
    student_assessment_worksheet_bundle,
)

router = APIRouter(tags=["v1-exam-reports"])
logger = logging.getLogger(__name__)


async def _resolve_exam_classroom_id(
    db: AsyncSession,
    *,
    classroom_id: UUID | None,
    schedule_id: UUID | None,
    assessment_id: UUID,
    student_id: UUID,
) -> UUID:
    """classroom_id obrigatório em assessment_results; deriva de schedule ou matrícula."""
    if classroom_id is not None:
        return classroom_id
    if schedule_id is not None:
        row = await fetch_one(
            db,
            """
            SELECT classroom_id FROM assessment_schedules
            WHERE id = CAST(:sid AS uuid)
            LIMIT 1
            """,
            {"sid": str(schedule_id)},
        )
        if row and row.get("classroom_id"):
            return UUID(str(row["classroom_id"]))
    row = await fetch_one(
        db,
        """
        SELECT ass.classroom_id
        FROM assessment_schedules ass
        INNER JOIN classroom_students cs
          ON cs.classroom_id = ass.classroom_id
         AND cs.student_id = CAST(:stu AS uuid)
        WHERE ass.assessment_id = CAST(:aid AS uuid)
        ORDER BY ass.start_time DESC NULLS LAST
        LIMIT 1
        """,
        {"stu": str(student_id), "aid": str(assessment_id)},
    )
    if row and row.get("classroom_id"):
        return UUID(str(row["classroom_id"]))
    raise HTTPException(
        status.HTTP_400_BAD_REQUEST,
        "Informe classroom_id ou schedule_id para gravar o resultado da avaliação.",
    )


async def _fetch_alternatives_json(db: AsyncSession, question_id: UUID) -> list[dict[str, Any]]:
    rows = await fetch_all(
        db,
        """
        SELECT id, label, text, raw_text, order_index
        FROM question_alternative
        WHERE question_id = CAST(:q AS uuid)
        ORDER BY order_index NULLS LAST, id
        """,
        {"q": str(question_id)},
    )
    out: list[dict[str, Any]] = []
    for a in rows:
        oid = a.get("order_index")
        out.append(
            {
                "id": str(a["id"]),
                "label": (a.get("label") or "") if a.get("label") is not None else "",
                "text": (a.get("text") or a.get("raw_text") or "") or "",
                "order_index": int(oid) if oid is not None else 0,
            }
        )
    return out


async def _build_app_question(
    db: AsyncSession,
    assessment_id: UUID,
    question_id: UUID,
    order_index: int | None,
) -> dict[str, Any] | None:
    meta = await fetch_one(
        db,
        """
        SELECT qi.id AS question_id, qi.description_html, qa.order_index
        FROM questions_assessments qa
        INNER JOIN question_item qi ON qi.id = qa.question_id
        WHERE qa.assessment_id = CAST(:aid AS uuid) AND qa.question_id = CAST(:qid AS uuid)
        LIMIT 1
        """,
        {"aid": str(assessment_id), "qid": str(question_id)},
    )
    if not meta:
        return None
    oi = order_index if order_index is not None else meta.get("order_index")
    oi_int = int(oi) if oi is not None else 0
    alts = await _fetch_alternatives_json(db, question_id)
    return {
        "id": str(meta["question_id"]),
        "description_html": (meta.get("description_html") or "") or "",
        "order": oi_int,
        "alternatives": alts,
    }


async def _upsert_question_student_response(
    db: AsyncSession,
    *,
    student_id: UUID,
    assessment_id: UUID,
    question_id: UUID,
    response_id: UUID,
    schedule_id: UUID | None,
) -> int:
    """Persiste resposta; devolve order_index da questão na prova."""
    o_row = await fetch_one(
        db,
        """
        SELECT order_index FROM questions_assessments
        WHERE assessment_id = CAST(:aid AS uuid) AND question_id = CAST(:qid AS uuid)
        LIMIT 1
        """,
        {"aid": str(assessment_id), "qid": str(question_id)},
    )
    if not o_row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Questão não pertence à avaliação")
    oidx = o_row.get("order_index")
    oidx_val = int(oidx) if oidx is not None else 0

    existing = await fetch_one(
        db,
        """
        SELECT id FROM question_student_responsed
        WHERE student_id = CAST(:sid AS uuid)
          AND assessment_id = CAST(:aid AS uuid)
          AND question_id = CAST(:qid AS uuid)
        ORDER BY updated_at DESC NULLS LAST
        LIMIT 1
        """,
        {"sid": str(student_id), "aid": str(assessment_id), "qid": str(question_id)},
    )
    params_u: dict[str, Any] = {
        "sid": str(student_id),
        "aid": str(assessment_id),
        "qid": str(question_id),
        "rid": str(response_id),
        "oidx": oidx_val,
    }
    if existing:
        pk = existing["id"]
        if schedule_id:
            await execute(
                db,
                """
                UPDATE question_student_responsed
                SET response_id = CAST(:rid AS uuid),
                    order_index = :oidx,
                    schedule_id = CAST(:sch AS uuid),
                    updated_at = now()
                WHERE id = :pk
                """,
                {**params_u, "sch": str(schedule_id), "pk": pk},
            )
        else:
            await execute(
                db,
                """
                UPDATE question_student_responsed
                SET response_id = CAST(:rid AS uuid),
                    order_index = :oidx,
                    updated_at = now()
                WHERE id = :pk
                """,
                {**params_u, "pk": pk},
            )
    else:
        if schedule_id:
            await fetch_one(
                db,
                """
                INSERT INTO question_student_responsed
                    (student_id, assessment_id, question_id, response_id, order_index, schedule_id)
                VALUES (
                    CAST(:sid AS uuid), CAST(:aid AS uuid), CAST(:qid AS uuid),
                    CAST(:rid AS uuid), :oidx, CAST(:sch AS uuid)
                )
                RETURNING id
                """,
                {**params_u, "sch": str(schedule_id)},
            )
        else:
            await fetch_one(
                db,
                """
                INSERT INTO question_student_responsed
                    (student_id, assessment_id, question_id, response_id, order_index)
                VALUES (
                    CAST(:sid AS uuid), CAST(:aid AS uuid), CAST(:qid AS uuid),
                    CAST(:rid AS uuid), :oidx
                )
                RETURNING id
                """,
                params_u,
            )
    return oidx_val


def _exam_progress_block(
    *,
    questions: list[dict[str, Any]],
    answered_ids: set[str],
) -> dict[str, Any]:
    answered_orders = [
        int(q["order_index"])
        for q in questions
        if str(q["question_id"]) in answered_ids and q.get("order_index") is not None
    ]
    last_answered_order = max(answered_orders) if answered_orders else None
    remaining_count = max(0, len(questions) - len(answered_ids))
    return {
        "total_questions": len(questions),
        "answered_count": len(answered_ids),
        "remaining_count": remaining_count,
        "last_answered_order": last_answered_order,
    }


async def _load_questions_and_answered(
    db: AsyncSession, assessment_id: UUID, student_id: UUID
) -> tuple[list[dict[str, Any]], set[str]]:
    questions = await fetch_all(
        db,
        """
        SELECT question_id, order_index, question
        FROM vw_questions_assessments
        WHERE assessment_id = CAST(:aid AS uuid)
        ORDER BY order_index
        """,
        {"aid": str(assessment_id)},
    )
    answered_rows = await fetch_all(
        db,
        """
        SELECT DISTINCT question_id
        FROM question_student_responsed
        WHERE assessment_id = CAST(:aid AS uuid)
          AND student_id = CAST(:sid AS uuid)
          AND response_id IS NOT NULL
        """,
        {"aid": str(assessment_id), "sid": str(student_id)},
    )
    answered_ids = {str(r["question_id"]) for r in answered_rows}
    return questions, answered_ids


class NextQuestionBody(BaseModel):
    assessment_id: UUID
    student_id: UUID | None = None
    classroom_id: UUID | None = None
    schedule_id: UUID | None = None
    question_id: UUID | None = None
    response_id: UUID | None = None
    assessment_result_id: UUID | None = Field(
        default=None,
        description="ID legado; ignorado pela engine v1 (linear).",
    )
    assessment_type: str | None = Field(
        default=None,
        description="Tipo da prova; ignorado pela engine v1 (sem IRT adaptativo nesta rodada).",
    )


@router.post("/exam/next-question")
async def exam_next_question_v1(
    body: NextQuestionBody,
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Próxima questão (ordem fixa) + persistência de resposta; contrato alinhado ao app (ex-edge).

    Engine **linear**: próxima não respondida em `order_index`. Não replica IRT/parada adaptativa
    da edge legada (ver documentação).
    """
    student_id = body.student_id or ctx.active_profile_id
    if str(student_id) != str(ctx.active_profile_id) and not is_admin_like(ctx.role):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Escopo")
    cscope = await get_effective_classroom_scope(db, ctx)
    if body.classroom_id and not cscope["is_admin_like"]:
        if str(body.classroom_id) not in {str(x) for x in (cscope["effective_classroom_ids"] or [])}:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Classroom fora do escopo")

    if body.assessment_result_id:
        logger.debug("exam next-question: assessment_result_id ignored in v1 linear engine")

    questions, answered_ids = await _load_questions_and_answered(db, body.assessment_id, student_id)
    if not questions:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Assessment sem questões")

    # --- persistir resposta (paridade status=updated da edge) ---
    if body.question_id is not None and body.response_id is not None:
        await _upsert_question_student_response(
            db,
            student_id=student_id,
            assessment_id=body.assessment_id,
            question_id=body.question_id,
            response_id=body.response_id,
            schedule_id=body.schedule_id,
        )
        await db.commit()
        questions, answered_ids = await _load_questions_and_answered(db, body.assessment_id, student_id)
        prog = _exam_progress_block(questions=questions, answered_ids=answered_ids)
        base = {
            "assessment_id": str(body.assessment_id),
            "student_id": str(student_id),
            "classroom_id": str(body.classroom_id) if body.classroom_id else None,
            "schedule_id": str(body.schedule_id) if body.schedule_id else None,
            **prog,
            "finished": False,
            "completed": False,
            "status": "updated",
            "message": None,
            "question": None,
            "order_index": None,
            "next_question": None,
        }
        return base

    # --- questão específica (revisão / gabarito): só question_id, sem response_id ---
    if body.question_id is not None and body.response_id is None:
        row = next(
            (q for q in questions if str(q["question_id"]) == str(body.question_id)),
            None,
        )
        if not row:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Questão não encontrada nesta avaliação")
        prog = _exam_progress_block(questions=questions, answered_ids=answered_ids)
        q_raw = row["question_id"]
        q_uuid = q_raw if isinstance(q_raw, UUID) else UUID(str(q_raw))
        oidx_raw = row.get("order_index")
        oidx = int(oidx_raw) if oidx_raw is not None else None
        app_q = await _build_app_question(db, body.assessment_id, q_uuid, oidx)
        if not app_q:
            raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Falha ao montar payload da questão")
        return {
            "assessment_id": str(body.assessment_id),
            "student_id": str(student_id),
            "classroom_id": str(body.classroom_id) if body.classroom_id else None,
            "schedule_id": str(body.schedule_id) if body.schedule_id else None,
            **prog,
            "finished": False,
            "completed": False,
            "status": None,
            "message": None,
            "question": app_q,
            "order_index": oidx,
            "next_question": row,
        }

    next_q = next((q for q in questions if str(q["question_id"]) not in answered_ids), None)
    prog = _exam_progress_block(questions=questions, answered_ids=answered_ids)
    finished = next_q is None

    base_out: dict[str, Any] = {
        "assessment_id": str(body.assessment_id),
        "student_id": str(student_id),
        "classroom_id": str(body.classroom_id) if body.classroom_id else None,
        "schedule_id": str(body.schedule_id) if body.schedule_id else None,
        **prog,
        "finished": finished,
        "completed": finished,
        "status": None,
        "message": None,
        "next_question": next_q,
    }

    if finished:
        base_out["question"] = None
        base_out["order_index"] = None
        return base_out

    q_raw = next_q["question_id"]
    q_uuid = q_raw if isinstance(q_raw, UUID) else UUID(str(q_raw))
    oidx_raw = next_q.get("order_index")
    oidx = int(oidx_raw) if oidx_raw is not None else None
    app_q = await _build_app_question(db, body.assessment_id, q_uuid, oidx)
    if not app_q:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Falha ao montar payload da questão")
    base_out["question"] = app_q
    base_out["order_index"] = oidx
    return base_out


class AssessmentResultBody(BaseModel):
    assessment_id: UUID
    student_id: UUID
    classroom_id: UUID | None = None
    schedule_id: UUID | None = None
    started_at: str | None = None
    submitted_at: str | None = None
    score: float | None = None
    feedback: str | None = None
    status: str | None = "pending"


@router.post("/exam/assessment-result")
async def exam_assessment_result_v1(
    body: AssessmentResultBody,
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Substituto mínimo de api_upsert_assessment_result (ON CONFLICT assessment_id, student_id)."""
    if str(body.student_id) != str(ctx.active_profile_id) and not is_admin_like(ctx.role):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Somente o aluno autenticado ou admin")
    cid = await _resolve_exam_classroom_id(
        db,
        classroom_id=body.classroom_id,
        schedule_id=body.schedule_id,
        assessment_id=body.assessment_id,
        student_id=body.student_id,
    )
    sch_sql = "CAST(:sc AS uuid)" if body.schedule_id else "NULL"
    sa_sql = "CAST(:sa AS timestamptz)" if body.started_at else "NULL"
    su_sql = "CAST(:su AS timestamptz)" if body.submitted_at else "NULL"
    params: dict[str, Any] = {
        "a": str(body.assessment_id),
        "st": str(body.student_id),
        "c": str(cid),
        "score": body.score,
        "fb": body.feedback,
        "status": body.status or "pending",
    }
    if body.schedule_id:
        params["sc"] = str(body.schedule_id)
    if body.started_at:
        params["sa"] = body.started_at
    if body.submitted_at:
        params["su"] = body.submitted_at
    row = await fetch_one(
        db,
        f"""
        INSERT INTO assessment_results (
            assessment_id, student_id, classroom_id, schedule_id,
            started_at, submitted_at, score, feedback, status
        )
        VALUES (
            CAST(:a AS uuid), CAST(:st AS uuid), CAST(:c AS uuid),
            {sch_sql}, {sa_sql}, {su_sql},
            :score, :fb, :status
        )
        ON CONFLICT ON CONSTRAINT assessment_results_assessment_id_student_id_key
        DO UPDATE SET
            classroom_id = EXCLUDED.classroom_id,
            schedule_id = COALESCE(EXCLUDED.schedule_id, assessment_results.schedule_id),
            started_at = COALESCE(EXCLUDED.started_at, assessment_results.started_at),
            submitted_at = COALESCE(EXCLUDED.submitted_at, assessment_results.submitted_at),
            score = COALESCE(EXCLUDED.score, assessment_results.score),
            feedback = COALESCE(EXCLUDED.feedback, assessment_results.feedback),
            status = COALESCE(EXCLUDED.status, assessment_results.status),
            updated_at = now()
        RETURNING *
        """,
        params,
    )
    await db.commit()
    return row


class GradeResultBody(BaseModel):
    student_id: UUID
    assessment_id: UUID
    status: str | None = Field(
        default=None,
        description="pending | in_progress | submitted | graded. "
        "Se omitido, é derivado da quantidade de questões respondidas.",
    )


_ALLOWED_RESULT_STATUS = {"pending", "in_progress", "submitted", "graded"}


@router.post("/reports/assessment-results/grade")
async def grade_assessment_result_v1(
    body: GradeResultBody,
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Gera/atualiza o resultado de um aluno em uma avaliação.

    Calcula o score (percentual de acertos) a partir das respostas e faz upsert
    em assessment_results via a function `fn_grade_assessment_result`.
    """
    if not (is_admin_like(ctx.role) or is_teacher_like(ctx.role)):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "Apenas professor ou gestor"
        )
    if body.status is not None and body.status not in _ALLOWED_RESULT_STATUS:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "status deve ser um de: " + ", ".join(sorted(_ALLOWED_RESULT_STATUS)),
        )
    row = await fetch_one(
        db,
        """
        SELECT * FROM public.fn_grade_assessment_result(
            CAST(:sid AS uuid), CAST(:aid AS uuid), :st
        )
        """,
        {
            "sid": str(body.student_id),
            "aid": str(body.assessment_id),
            "st": body.status,
        },
    )
    await db.commit()
    return row


@router.post("/reports/assessments/{assessment_id}/grade-all")
async def grade_assessment_all_v1(
    assessment_id: UUID,
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
    status_param: str | None = Query(
        None,
        alias="status",
        description="pending | in_progress | submitted | graded (opcional).",
    ),
):
    """Gera/atualiza os resultados de todos os alunos vinculados ao caderno."""
    if not (is_admin_like(ctx.role) or is_teacher_like(ctx.role)):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "Apenas professor ou gestor"
        )
    if status_param is not None and status_param not in _ALLOWED_RESULT_STATUS:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "status deve ser um de: " + ", ".join(sorted(_ALLOWED_RESULT_STATUS)),
        )
    rows = await fetch_all(
        db,
        """
        SELECT * FROM public.fn_grade_assessment_all(
            CAST(:aid AS uuid), :st
        )
        """,
        {"aid": str(assessment_id), "st": status_param},
    )
    await db.commit()
    return {"graded": len(rows), "items": rows}


@router.get("/exam/evaluation-in-progress")
async def exam_eval_in_progress_v1(
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
    assessment_id: UUID | None = Query(None, description="Filtrar por avaliação (opcional)."),
):
    if assessment_id is None:
        return await fetch_all(
            db,
            "SELECT * FROM vw_evaluation_in_progress WHERE student_id = CAST(:sid AS uuid)",
            {"sid": str(ctx.active_profile_id)},
        )
    return await fetch_all(
        db,
        """
        SELECT e.*
        FROM vw_evaluation_in_progress e
        WHERE e.student_id = CAST(:sid AS uuid)
          AND e.assessment_id = CAST(:aid AS uuid)
        """,
        {"sid": str(ctx.active_profile_id), "aid": str(assessment_id)},
    )


@router.get("/exam/progress")
async def exam_progress_v1(
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
    assessment_id: UUID = Query(..., description="Avaliação."),
):
    """Respostas já gravadas com alternativas (ids) para bootstrap / gabarito no app."""
    sid = ctx.active_profile_id
    rows = await fetch_all(
        db,
        """
        SELECT qsr.question_id,
               qsr.response_id,
               qa.order_index,
               qi.description_html,
               sel.label AS label
        FROM (
            SELECT DISTINCT ON (question_id) question_id, response_id, updated_at
            FROM question_student_responsed
            WHERE student_id = CAST(:sid AS uuid)
              AND assessment_id = CAST(:aid AS uuid)
              AND response_id IS NOT NULL
            ORDER BY question_id, updated_at DESC NULLS LAST
        ) qsr
        INNER JOIN questions_assessments qa
          ON qa.assessment_id = CAST(:aid AS uuid) AND qa.question_id = qsr.question_id
        INNER JOIN question_item qi ON qi.id = qsr.question_id
        LEFT JOIN question_alternative sel ON sel.id = qsr.response_id
        ORDER BY qa.order_index NULLS LAST
        """,
        {"sid": str(sid), "aid": str(assessment_id)},
    )
    items: list[dict[str, Any]] = []
    for r in rows:
        qid = r["question_id"]
        if qid is None:
            continue
        q_uuid = UUID(str(qid))
        oi_raw = r.get("order_index")
        oi = int(oi_raw) if oi_raw is not None else 0
        alts = await _fetch_alternatives_json(db, q_uuid)
        items.append(
            {
                "question_id": str(qid),
                "response_id": str(r["response_id"]) if r.get("response_id") else None,
                "order_index": oi,
                "label": r.get("label"),
                "question": {
                    "id": str(qid),
                    "description_html": (r.get("description_html") or "") or "",
                    "order": oi,
                    "alternatives": alts,
                },
            }
        )
    return {"assessment_id": str(assessment_id), "student_id": str(sid), "items": items}


@router.get("/exam/questions")
async def exam_questions_v1(
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
    assessment_id: UUID,
):
    return await fetch_all(
        db,
        "SELECT * FROM vw_questions_assessments WHERE assessment_id = CAST(:a AS uuid) ORDER BY order_index",
        {"a": str(assessment_id)},
    )


@router.get("/exam/questions/{question_id}/alternatives")
async def exam_question_alternatives_v1(
    question_id: UUID,
    _: Annotated[AuthContext, Depends(get_auth_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    return await fetch_all(
        db,
        "SELECT * FROM question_alternative WHERE question_id = CAST(:q AS uuid) ORDER BY order_index NULLS LAST",
        {"q": str(question_id)},
    )


@router.get("/reports/assessments/{assessment_id}/students/{student_id}/worksheet")
async def report_student_worksheet_v1(
    assessment_id: UUID,
    student_id: UUID,
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
    academic_year_id: UUID | None = Query(None, description="Ano letivo. Se omitido, usa is_primary=true."),
    classroom_id: UUID | None = Query(None, description="Turma do resultado (opcional, desambiguação)."),
    schedule_id: UUID | None = Query(None, description="Agendamento (opcional)."),
):
    """Relatório individual completo para a UI (sem PostgREST)."""
    await assert_can_access_assessment_report_student(
        db, ctx, student_id=student_id, assessment_id=assessment_id
    )
    effective_ay = await resolve_academic_year_id(db, academic_year_id)
    return await student_assessment_worksheet_bundle(
        db,
        student_id=student_id,
        assessment_id=assessment_id,
        academic_year_id=effective_ay,
        classroom_id=classroom_id,
        schedule_id=schedule_id,
    )


@router.get("/reports/assessments/{assessment_id}/students/{student_id}")
async def report_assessment_student_v1(
    assessment_id: UUID,
    student_id: UUID,
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
    academic_year_id: UUID | None = Query(None, description="Ano letivo. Se omitido, usa is_primary=true."),
    classroom_id: UUID | None = Query(None, description="Filtrar por turma (opcional)."),
    pg: Annotated[PageArgs, Depends(pagination_params)] = PageArgs(1, 50),
):
    effective_ay = await resolve_academic_year_id(db, academic_year_id)
    await assert_can_access_assessment_report_student(
        db, ctx, student_id=student_id, assessment_id=assessment_id
    )
    sql = """
    SELECT ar.*
    FROM assessment_report ar
    JOIN classrooms c ON c.id = ar.classroom_id
    WHERE ar.assessment_id = CAST(:a AS uuid)
      AND ar.student_id = CAST(:s AS uuid)
      AND c.academic_year_id = CAST(:ay AS uuid)
    """
    params: dict[str, Any] = {"a": str(assessment_id), "s": str(student_id), "ay": str(effective_ay)}
    if classroom_id:
        sql += " AND ar.classroom_id = CAST(:cid AS uuid)"
        params["cid"] = str(classroom_id)
    count_row = await fetch_one(db, f"SELECT COUNT(*)::int AS total FROM ({sql}) q", params)
    items = await fetch_all(
        db,
        f"{sql} ORDER BY classroom_id LIMIT {pg.per_page} OFFSET {pg.offset}",
        params,
    )
    logger.info(
        "[v1/reports/assessment-student] assessment_id=%s student_id=%s academic_year_id=%s classroom_id=%s total=%s",
        assessment_id,
        student_id,
        effective_ay,
        classroom_id,
        (count_row or {}).get("total", 0),
    )
    return paged_response(page=pg.page, per_page=pg.per_page, total=(count_row or {}).get("total", 0), items=items)


@router.get("/reports/assessment-schedules/{schedule_id}")
async def report_assessment_schedule_v1(
    schedule_id: UUID,
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
    academic_year_id: UUID | None = Query(None, description="Ano letivo. Se omitido, usa is_primary=true."),
):
    """Relatório por agendamento (substitui PostgREST em `TeacherService.getAssessmentReport`)."""
    effective_ay = await resolve_academic_year_id(db, academic_year_id)
    await assert_can_read_schedule_report(
        db, ctx, schedule_id=schedule_id, academic_year_id=effective_ay
    )
    return await assessment_schedule_report_bundle(
        db, schedule_id=schedule_id, academic_year_id=effective_ay
    )


@router.get("/reports/classrooms/{classroom_id}")
async def report_classroom_v1(
    classroom_id: UUID,
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
    academic_year_id: UUID | None = Query(None, description="Ano letivo. Se omitido, usa is_primary=true."),
    assessment_id: UUID | None = Query(None, description="Filtrar por avaliação (opcional)."),
    pg: Annotated[PageArgs, Depends(pagination_params)] = PageArgs(1, 50),
):
    effective_ay = await resolve_academic_year_id(db, academic_year_id)
    c_row = await load_classroom_row_by_id(db, classroom_id)
    if not c_row:
        logger.warning(
            "[v1/reports/classrooms] classroom_id=%s not found (no row in classrooms)",
            classroom_id,
        )
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Classroom id not found")
    if str(c_row.get("academic_year_id")) != str(effective_ay):
        logger.warning(
            "[v1/reports/classrooms] academic year mismatch classroom_id=%s classroom_ay=%s requested_ay=%s",
            classroom_id,
            c_row.get("academic_year_id"),
            effective_ay,
        )
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Turma não pertence ao ano letivo solicitado "
            f"(classroom_academic_year_id={c_row.get('academic_year_id')} "
            f"requested_academic_year_id={effective_ay} classroom_id={classroom_id})",
        )
    await assert_actor_can_read_classroom(db, ctx, classroom_id)
    logger.info(
        "[v1/reports/classrooms] classroom_id=%s assessment_id=%s academic_year_id=%s role=%r",
        classroom_id,
        assessment_id,
        effective_ay,
        ctx.role,
    )
    if assessment_id:
        return await classroom_assessment_report_envelope(
            db,
            classroom_id=classroom_id,
            assessment_id=assessment_id,
            academic_year_id=effective_ay,
        )
    sql = "SELECT * FROM vw_assessment_summary WHERE classroom_id = CAST(:cid AS uuid)"
    params: dict[str, Any] = {"cid": str(classroom_id)}
    count_row = await fetch_one(db, f"SELECT COUNT(*)::int AS total FROM ({sql}) q", params)
    items = await fetch_all(
        db,
        f"{sql} ORDER BY assessment_id LIMIT {pg.per_page} OFFSET {pg.offset}",
        params,
    )
    return paged_response(page=pg.page, per_page=pg.per_page, total=(count_row or {}).get("total", 0), items=items)


@router.get("/reports/assessments/{assessment_id}/pedagogical")
async def report_assessment_pedagogical_v1(
    assessment_id: UUID,
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
    classroom_id: UUID = Query(..., description="Turma do relatório (obrigatório)."),
    student_id: UUID | None = Query(None, description="Aluno (omitido = consolidado da turma)."),
    academic_year_id: UUID | None = Query(None, description="Ano letivo. Se omitido, usa o da turma."),
):
    """Relatório pedagógico por componente curricular (variação intervir/orientar/desafiar)."""
    c_row = await load_classroom_row_by_id(db, classroom_id)
    if not c_row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Turma não encontrada")
    classroom_ay = UUID(str(c_row["academic_year_id"]))
    if academic_year_id is not None and str(academic_year_id) != str(classroom_ay):
        logger.warning(
            "[v1/reports/pedagogical] academic_year_id=%s difere da turma %s — usando %s",
            academic_year_id,
            classroom_id,
            classroom_ay,
        )
    await assert_actor_can_read_classroom(db, ctx, classroom_id)
    if student_id is not None:
        await assert_can_access_assessment_report_student(
            db, ctx, student_id=student_id, assessment_id=assessment_id
        )
    return await assessment_pedagogical_report_bundle(
        db,
        assessment_id=assessment_id,
        classroom_id=classroom_id,
        academic_year_id=classroom_ay,
        student_id=student_id,
    )


@router.get("/students/{student_id}/detail")
async def student_detail_v1(
    student_id: UUID,
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
    academic_year_id: UUID | None = Query(None, description="Ano letivo. Se omitido, usa is_primary=true."),
):
    """Perfil + resumos de avaliação no ano letivo. Escopo alinhado a GET /v1/students/{id} (escola/pessoa)."""
    prof = await _student_scope_read(db, ctx, student_id)
    if "student" not in str(prof.get("role") or "").lower():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Student not found")
    effective_ay = await resolve_academic_year_id(db, academic_year_id)
    sums = await fetch_all(
        db,
        """
        SELECT vs.*
        FROM vw_student_assessment_sumarize vs
        JOIN classrooms c ON c.id = vs.classroom_id
        WHERE vs.student_id = CAST(:id AS uuid)
          AND c.academic_year_id = CAST(:ay AS uuid)
        """,
        {"id": str(student_id), "ay": str(effective_ay)},
    )
    return {"profile": prof, "assessment_summaries": sums, "academic_year_id": str(effective_ay)}


@router.get("/teacher/classrooms")
async def teacher_classrooms_v1(
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
    academic_year_id: UUID | None = Query(None, description="Ano letivo. Se omitido, usa is_primary=true."),
):
    effective_ay = await resolve_academic_year_id(db, academic_year_id)
    cscope = await get_effective_classroom_scope(db, ctx)
    sql = """
    SELECT mc.*
    FROM my_classrooms mc
    JOIN classrooms c ON c.id = mc.classroom_id
    WHERE mc.teacher_id = CAST(:tid AS uuid)
      AND c.academic_year_id = CAST(:ay AS uuid)
    """
    params: dict[str, Any] = {"tid": str(ctx.active_profile_id), "ay": str(effective_ay)}
    # `my_classrooms` já é derivada de `vw_teacher_classroom_options` (ver schema.sql). Para
    # professor, `teacher_id` + ano letivo bastam; `ANY(effective_classroom_ids)` era redundante
    # e podia zerar a lista se `get_effective_classroom_scope` não devolvesse os mesmos ids.
    if not cscope["is_admin_like"] and not is_teacher_like(ctx.role):
        sql += " AND mc.classroom_id = ANY(CAST(:cids AS uuid[]))"
        params["cids"] = [str(x) for x in (cscope["effective_classroom_ids"] or [])]
    logger.info(
        "[v1/teacher/classrooms] active_profile_id=%s role=%r resolved_academic_year_id=%s "
        "is_admin_like=%s is_teacher_like=%s sql=%r params=%s",
        ctx.active_profile_id,
        ctx.role,
        str(effective_ay),
        cscope.get("is_admin_like"),
        is_teacher_like(ctx.role),
        sql.strip(),
        {k: (v if k != "cids" else f"<{len(v)} ids>") for k, v in params.items()},
    )
    rows = await fetch_all(db, sql, params)
    logger.info("[v1/teacher/classrooms] row_count=%s", len(rows))
    return rows


async def _assert_teacher_classroom_scope(
    db: AsyncSession,
    ctx: AuthContext,
    cscope: dict[str, Any],
    classroom_id: UUID | None,
) -> None:
    """Garante que a turma filtrada pertence ao escopo do professor/gestor."""
    if not classroom_id or cscope["is_admin_like"]:
        return
    if is_teacher_like(ctx.role):
        ok = await fetch_one(
            db,
            """
            SELECT 1 AS ok FROM my_classrooms mc
            WHERE mc.teacher_id = CAST(:pid AS uuid)
              AND mc.classroom_id = CAST(:cid AS uuid)
            LIMIT 1
            """,
            {"pid": str(ctx.active_profile_id), "cid": str(classroom_id)},
        )
        if not ok:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Turma fora do vínculo do professor")
    elif str(classroom_id) not in {str(x) for x in (cscope["effective_classroom_ids"] or [])}:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Classroom fora do escopo")


@router.get("/teacher/assessments")
async def teacher_macro_assessments_v1(
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
    academic_year_id: UUID | None = Query(None, description="Ano letivo. Se omitido, usa is_primary=true."),
    classroom_id: UUID | None = Query(None, description="Filtrar por turma selecionada."),
):
    """Listagem de **macro avaliações** do professor/gestor.

    Fonte principal: ``vw_macro_assessment_summary`` (uma linha por macro avaliação
    × turma, consolidando pendentes/concluídos/não entregues dos cadernos).

    Para compatibilidade durante a transição, também inclui cadernos avulsos
    (`assessments` sem ``macro_assessment_id``) a partir de ``vw_assessment_summary``,
    para que avaliações ainda não vinculadas a uma macro não desapareçam da lista.
    Cada item expõe ``id`` (macro avaliação **ou** assessment legado) — a rota de
    relatório resolve ambos.
    """
    effective_ay = await resolve_academic_year_id(db, academic_year_id)
    cscope = await get_effective_classroom_scope(db, ctx)
    await _assert_teacher_classroom_scope(db, ctx, cscope, classroom_id)

    params: dict[str, Any] = {"ay": str(effective_ay)}

    # Filtro de ano letivo: aplicado só quando a listagem é ampla (sem turma)
    # E não é o caso de um professor (cujo escopo já vem de `my_classrooms`).
    # - turma informada → a própria turma determina o ano;
    # - professor sem turma → escopo por `my_classrooms` (vê todas as suas turmas),
    #   sem amarrar ao ano selecionado no app (que pode estar defasado em cache);
    # - gestor sem turma → restringe ao ano letivo selecionado.
    scope_by_teacher = (not cscope["is_admin_like"]) and is_teacher_like(ctx.role)
    apply_ay = (classroom_id is None) and (not scope_by_teacher)
    ay_filter = " AND c.academic_year_id = CAST(:ay AS uuid)" if apply_ay else ""

    def _scope_sql(alias: str) -> str:
        sql = ""
        if classroom_id:
            sql += f" AND {alias}.classroom_id = CAST(:cid AS uuid)"
            params["cid"] = str(classroom_id)
        if not cscope["is_admin_like"]:
            if is_teacher_like(ctx.role):
                sql += f"""
                  AND EXISTS (
                    SELECT 1 FROM my_classrooms mc
                    WHERE mc.teacher_id = CAST(:tid AS uuid)
                      AND mc.classroom_id = {alias}.classroom_id
                  )
                """
                params["tid"] = str(ctx.active_profile_id)
            else:
                sql += f" AND {alias}.classroom_id = ANY(CAST(:cids AS uuid[]))"
                params["cids"] = [str(x) for x in (cscope["effective_classroom_ids"] or [])]
        return sql

    # Macro avaliações (consolidadas).
    macro_sql = f"""
    SELECT v.macro_assessment_id, v.classroom_id, v.school_id,
           MIN(c.name) AS classroom_name,
           MIN(v.title) AS title,
           MIN(v.description) AS description,
           MIN(v.type) AS type,
           MIN(v.year) AS year,
           bool_or(v.is_active) AS is_active,
           COALESCE(SUM(v.pending), 0)::int AS pending,
           COALESCE(SUM(v.completed), 0)::int AS completed,
           COALESCE(SUM(v.did_not_deliver), 0)::int AS did_not_deliver
    FROM vw_macro_assessment_summary v
    JOIN classrooms c ON c.id = v.classroom_id
    WHERE v.macro_assessment_id IS NOT NULL
      {ay_filter}
      {_scope_sql("v")}
    GROUP BY v.macro_assessment_id, v.classroom_id, v.school_id
    """
    macro_rows = await fetch_all(db, macro_sql, params)

    # Cadernos avulsos (sem macro) — compatibilidade.
    legacy_sql = f"""
    SELECT vas.assessment_id, vas.classroom_id, vas.school_id,
           c.name AS classroom_name,
           vas.title, vas.description, vas.type, vas.year, vas.is_active,
           COALESCE(vas.pending, 0)::int AS pending,
           COALESCE(vas.completed, 0)::int AS completed,
           COALESCE(vas.did_not_deliver, 0)::int AS did_not_deliver
    FROM vw_assessment_summary vas
    JOIN classrooms c ON c.id = vas.classroom_id
    JOIN assessments a ON a.id = vas.assessment_id
    WHERE a.macro_assessment_id IS NULL
      {ay_filter}
      {_scope_sql("vas")}
    """
    legacy_rows = await fetch_all(db, legacy_sql, params)

    items: list[dict[str, Any]] = []
    for r in macro_rows:
        items.append(
            {
                "id": str(r.get("macro_assessment_id")),
                "macroAssessmentId": str(r.get("macro_assessment_id")),
                "isMacro": True,
                "title": r.get("title") or "",
                "description": r.get("description"),
                "type": r.get("type"),
                "year": r.get("year"),
                "isActive": bool(r.get("is_active")),
                "schoolId": str(r.get("school_id")) if r.get("school_id") else None,
                "classroomId": str(r.get("classroom_id")) if r.get("classroom_id") else None,
                "classroomName": r.get("classroom_name"),
                "pending": int(r.get("pending") or 0),
                "completed": int(r.get("completed") or 0),
                "didNotDeliver": int(r.get("did_not_deliver") or 0),
            }
        )
    for r in legacy_rows:
        items.append(
            {
                "id": str(r.get("assessment_id")),
                "macroAssessmentId": None,
                "isMacro": False,
                "title": r.get("title") or "",
                "description": r.get("description"),
                "type": r.get("type"),
                "year": r.get("year"),
                "isActive": bool(r.get("is_active")),
                "schoolId": str(r.get("school_id")) if r.get("school_id") else None,
                "classroomId": str(r.get("classroom_id")) if r.get("classroom_id") else None,
                "classroomName": r.get("classroom_name"),
                "pending": int(r.get("pending") or 0),
                "completed": int(r.get("completed") or 0),
                "didNotDeliver": int(r.get("did_not_deliver") or 0),
            }
        )
    items.sort(key=lambda x: ((x.get("classroomName") or "").lower(), (x["title"] or "").lower()))
    return {"items": items, "academic_year_id": str(effective_ay)}


@router.get("/teacher/macro-assessments/{macro_id}/report-context")
async def macro_report_context_v1(
    macro_id: UUID,
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
    classroom_id: UUID = Query(..., description="Turma do relatório (obrigatório)."),
):
    """Contexto da macro avaliação (macro + turma + cadernos).

    Aceita também um `assessment_id` legado em ``macro_id`` (resolve a macro).
    """
    await assert_actor_can_read_classroom(db, ctx, classroom_id)
    scope = await resolve_macro_scope(db, route_id=macro_id, classroom_id=classroom_id)
    return await macro_report_context(db, scope=scope, classroom_id=classroom_id)


@router.get("/teacher/macro-assessments/{macro_id}/assessments")
async def macro_assessments_list_v1(
    macro_id: UUID,
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
    classroom_id: UUID = Query(..., description="Turma do relatório (obrigatório)."),
):
    """Cadernos (`assessments`) de uma macro avaliação agendados para a turma."""
    await assert_actor_can_read_classroom(db, ctx, classroom_id)
    scope = await resolve_macro_scope(db, route_id=macro_id, classroom_id=classroom_id)
    return {"macroAssessmentId": scope.get("macro_id"), "items": scope["assessments"]}


@router.get("/teacher/macro-assessments/{macro_id}/report/components")
async def macro_report_components_v1(
    macro_id: UUID,
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
    classroom_id: UUID = Query(..., description="Turma do relatório (obrigatório)."),
):
    """Componentes consolidados de todos os cadernos da macro avaliação (turma)."""
    await assert_actor_can_read_classroom(db, ctx, classroom_id)
    scope = await resolve_macro_scope(db, route_id=macro_id, classroom_id=classroom_id)
    return await macro_components_report(
        db, assessment_ids=scope["assessment_ids"], classroom_id=classroom_id
    )


@router.get("/teacher/macro-assessments/{macro_id}/report/students")
async def macro_report_students_v1(
    macro_id: UUID,
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
    classroom_id: UUID = Query(..., description="Turma do relatório (obrigatório)."),
):
    """Desempenho por aluno consolidando a macro avaliação (linha por aluno×caderno)."""
    await assert_actor_can_read_classroom(db, ctx, classroom_id)
    scope = await resolve_macro_scope(db, route_id=macro_id, classroom_id=classroom_id)
    return await macro_students_report(
        db, assessment_ids=scope["assessment_ids"], classroom_id=classroom_id
    )


# ---------------------------------------------------------------------------
# Visão do gestor escolar: macro avaliações consolidadas por ESCOLA.
# ---------------------------------------------------------------------------


async def _resolve_school_ids(
    db: AsyncSession, ctx: AuthContext, school_id: UUID
) -> list[UUID]:
    """Valida o escopo do gestor e devolve a subárvore de escolas permitida.

    Reutiliza :func:`resolve_admin_dashboard_school_ids` (403 se não for staff
    admin; intersecta com o escopo do `school_admin`).
    """
    ids = await resolve_admin_dashboard_school_ids(db, ctx, school_id)
    if ids is None:
        # admin global sem recorte explícito: restringe à subárvore pedida.
        ids = await get_descendant_school_ids(db, school_id)
    if not ids:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Escola fora do seu escopo")
    return ids


@router.get("/school-admin/schools/{school_id}/macro-assessments")
async def school_admin_macro_assessments_v1(
    school_id: UUID,
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
    academic_year_id: UUID | None = Query(None, description="Ano letivo. Se omitido, usa is_primary=true."),
):
    """Lista **macro avaliações** consolidadas por escola (1 linha por macro).

    Soma pendentes/concluídos/não entregues de todas as turmas da escola e conta
    quantas turmas têm cadernos da macro. Inclui cadernos avulsos (sem macro).
    """
    effective_ay = await resolve_academic_year_id(db, academic_year_id)
    school_ids = await _resolve_school_ids(db, ctx, school_id)
    params: dict[str, Any] = {"ay": str(effective_ay), "sids": [str(s) for s in school_ids]}

    macro_sql = """
    SELECT v.macro_assessment_id,
           MIN(c.school_id::text) AS school_id,
           MIN(v.title) AS title,
           MIN(v.description) AS description,
           MIN(v.type) AS type,
           MIN(v.year) AS year,
           bool_or(v.is_active) AS is_active,
           COUNT(DISTINCT v.classroom_id) AS classrooms,
           COALESCE(SUM(v.pending), 0)::int AS pending,
           COALESCE(SUM(v.completed), 0)::int AS completed,
           COALESCE(SUM(v.did_not_deliver), 0)::int AS did_not_deliver
    FROM vw_macro_assessment_summary v
    JOIN classrooms c ON c.id = v.classroom_id
    WHERE v.macro_assessment_id IS NOT NULL
      AND c.school_id = ANY(CAST(:sids AS uuid[]))
      AND c.academic_year_id = CAST(:ay AS uuid)
    GROUP BY v.macro_assessment_id
    """
    macro_rows = await fetch_all(db, macro_sql, params)

    legacy_sql = """
    SELECT vas.assessment_id,
           MIN(c.school_id::text) AS school_id,
           MIN(vas.title) AS title,
           MIN(vas.description) AS description,
           MIN(vas.type) AS type,
           MIN(vas.year) AS year,
           bool_or(vas.is_active) AS is_active,
           COUNT(DISTINCT vas.classroom_id) AS classrooms,
           COALESCE(SUM(vas.pending), 0)::int AS pending,
           COALESCE(SUM(vas.completed), 0)::int AS completed,
           COALESCE(SUM(vas.did_not_deliver), 0)::int AS did_not_deliver
    FROM vw_assessment_summary vas
    JOIN classrooms c ON c.id = vas.classroom_id
    JOIN assessments a ON a.id = vas.assessment_id
    WHERE a.macro_assessment_id IS NULL
      AND c.school_id = ANY(CAST(:sids AS uuid[]))
      AND c.academic_year_id = CAST(:ay AS uuid)
    GROUP BY vas.assessment_id
    """
    legacy_rows = await fetch_all(db, legacy_sql, params)

    items: list[dict[str, Any]] = []
    for r in macro_rows:
        items.append(
            {
                "id": str(r.get("macro_assessment_id")),
                "macroAssessmentId": str(r.get("macro_assessment_id")),
                "isMacro": True,
                "title": r.get("title") or "",
                "description": r.get("description"),
                "type": r.get("type"),
                "year": r.get("year"),
                "isActive": bool(r.get("is_active")),
                "schoolId": r.get("school_id"),
                "classrooms": int(r.get("classrooms") or 0),
                "pending": int(r.get("pending") or 0),
                "completed": int(r.get("completed") or 0),
                "didNotDeliver": int(r.get("did_not_deliver") or 0),
            }
        )
    for r in legacy_rows:
        items.append(
            {
                "id": str(r.get("assessment_id")),
                "macroAssessmentId": None,
                "isMacro": False,
                "title": r.get("title") or "",
                "description": r.get("description"),
                "type": r.get("type"),
                "year": r.get("year"),
                "isActive": bool(r.get("is_active")),
                "schoolId": r.get("school_id"),
                "classrooms": int(r.get("classrooms") or 0),
                "pending": int(r.get("pending") or 0),
                "completed": int(r.get("completed") or 0),
                "didNotDeliver": int(r.get("did_not_deliver") or 0),
            }
        )
    items.sort(key=lambda x: (x["title"] or "").lower())
    return {"items": items, "academic_year_id": str(effective_ay)}


@router.get("/school-admin/macro-assessments/{macro_id}/assessments")
async def school_admin_macro_cadernos_v1(
    macro_id: UUID,
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
    school_id: UUID = Query(..., description="Escola do gestor (obrigatório)."),
):
    """Lista TODOS os cadernos (`assessments`) de uma macro avaliação (para agendar).

    Não exige agendamento prévio; aceita também um `assessment_id` legado.
    """
    await _resolve_school_ids(db, ctx, school_id)
    macro_row = await fetch_one(
        db,
        "SELECT id, title FROM macro_assessments WHERE id = CAST(:id AS uuid)",
        {"id": str(macro_id)},
    )
    if macro_row:
        rows = await fetch_all(
            db,
            """
            SELECT id, title, description, type
            FROM assessments
            WHERE macro_assessment_id = CAST(:mid AS uuid)
            ORDER BY created_at, title
            """,
            {"mid": str(macro_id)},
        )
    else:
        rows = await fetch_all(
            db,
            "SELECT id, title, description, type FROM assessments WHERE id = CAST(:id AS uuid)",
            {"id": str(macro_id)},
        )
        if not rows:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Macro avaliação não encontrada")
    items = [
        {
            "id": str(r["id"]),
            "title": r.get("title") or "",
            "description": r.get("description"),
            "type": r.get("type"),
        }
        for r in rows
    ]
    return {"macroAssessmentId": str(macro_id) if macro_row else None, "items": items}


@router.get("/school-admin/macro-assessments/{macro_id}/report/context")
async def school_admin_macro_report_context_v1(
    macro_id: UUID,
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
    school_id: UUID = Query(..., description="Escola do gestor (obrigatório)."),
):
    """Contexto consolidado por escola: macro + cadernos + turmas (para o seletor)."""
    school_ids = await _resolve_school_ids(db, ctx, school_id)
    scope = await resolve_macro_scope_school(db, route_id=macro_id, school_ids=school_ids)
    school_row = await fetch_one(
        db,
        "SELECT id, name FROM schools WHERE id = CAST(:id AS uuid)",
        {"id": str(school_id)},
    )
    return {
        "macroAssessment": {"id": scope.get("macro_id"), "title": scope.get("macro_title") or ""},
        "school": {
            "id": str(school_id),
            "name": (school_row or {}).get("name") or "",
        },
        "assessments": scope["assessments"],
        "classrooms": scope["classrooms"],
    }


@router.get("/school-admin/macro-assessments/{macro_id}/report/components")
async def school_admin_macro_report_components_v1(
    macro_id: UUID,
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
    school_id: UUID = Query(..., description="Escola do gestor (obrigatório)."),
):
    """Componentes consolidados de todos os cadernos da macro em toda a escola."""
    school_ids = await _resolve_school_ids(db, ctx, school_id)
    scope = await resolve_macro_scope_school(db, route_id=macro_id, school_ids=school_ids)
    return await macro_components_report_for_classrooms(
        db, assessment_ids=scope["assessment_ids"], classroom_ids=scope["classroom_ids"]
    )


@router.get("/school-admin/macro-assessments/{macro_id}/report/students")
async def school_admin_macro_report_students_v1(
    macro_id: UUID,
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
    school_id: UUID = Query(..., description="Escola do gestor (obrigatório)."),
):
    """Desempenho por aluno consolidando a macro em toda a escola (linha por aluno×caderno×turma)."""
    school_ids = await _resolve_school_ids(db, ctx, school_id)
    scope = await resolve_macro_scope_school(db, route_id=macro_id, school_ids=school_ids)
    return await macro_students_report_for_classrooms(
        db, assessment_ids=scope["assessment_ids"], classroom_ids=scope["classroom_ids"]
    )


@router.get("/teacher/dashboard/summary")
async def teacher_dashboard_v1(
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
    academic_year_id: UUID | None = Query(None, description="Ano letivo. Se omitido, usa is_primary=true."),
    classroom_id: UUID | None = Query(None, description="Filtrar dashboard por turma selecionada."),
    pg: Annotated[PageArgs, Depends(pagination_params)] = PageArgs(1, 50),
):
    """Resumo paginado (`items`) + métricas agregadas no topo para cards do app (`total_students`, …)."""
    effective_ay = await resolve_academic_year_id(db, academic_year_id)
    cscope = await get_effective_classroom_scope(db, ctx)
    if classroom_id and not cscope["is_admin_like"]:
        if is_teacher_like(ctx.role):
            ok = await fetch_one(
                db,
                """
                SELECT 1 AS ok FROM my_classrooms mc
                WHERE mc.teacher_id = CAST(:pid AS uuid)
                  AND mc.classroom_id = CAST(:cid AS uuid)
                LIMIT 1
                """,
                {"pid": str(ctx.active_profile_id), "cid": str(classroom_id)},
            )
            if not ok:
                raise HTTPException(
                    status.HTTP_403_FORBIDDEN,
                    "Turma fora do vínculo do professor",
                )
        elif str(classroom_id) not in {str(x) for x in (cscope["effective_classroom_ids"] or [])}:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Classroom fora do escopo")

    # schedule_id: um agendamento por (assessment_id, classroom_id) — mesma regra que o app usava via PostgREST.
    base_sql = """
    SELECT vas.*,
        (
            SELECT sch.id
            FROM assessment_schedules sch
            WHERE sch.assessment_id = vas.assessment_id
              AND sch.classroom_id = vas.classroom_id
            ORDER BY sch.start_time NULLS LAST, sch.id
            LIMIT 1
        ) AS schedule_id
    FROM vw_assessment_summary vas
    JOIN classrooms c ON c.id = vas.classroom_id
    WHERE c.academic_year_id = CAST(:ay AS uuid)
    """
    params: dict[str, Any] = {"ay": str(effective_ay)}

    if classroom_id:
        base_sql += " AND vas.classroom_id = CAST(:cid AS uuid)"
        params["cid"] = str(classroom_id)

    if cscope["is_admin_like"]:
        count_row = await fetch_one(db, f"SELECT COUNT(*)::int AS total FROM ({base_sql}) q", params)
        items = await fetch_all(
            db,
            f"{base_sql} ORDER BY vas.classroom_id LIMIT {pg.per_page} OFFSET {pg.offset}",
            params,
        )
        mrow = await fetch_one(
            db,
            f"""
            SELECT
              COUNT(*)::int AS total_assessments,
              COUNT(*) FILTER (WHERE vas.pending > 0)::int AS active_assessments,
              COUNT(*) FILTER (WHERE vas.completed > 0)::int AS completed_assessments,
              (SELECT COUNT(DISTINCT cs.student_id)::int
                 FROM classroom_students cs
                 JOIN classrooms c2 ON c2.id = cs.classroom_id
                WHERE c2.academic_year_id = CAST(:ay AS uuid)
                  {("AND cs.classroom_id = CAST(:cid AS uuid)" if classroom_id else "")}
              ) AS total_students,
              (SELECT COUNT(*)::int FROM classrooms c3
                WHERE c3.academic_year_id = CAST(:ay AS uuid)
                  {("AND c3.id = CAST(:cid AS uuid)" if classroom_id else "")}
              ) AS total_classrooms
            FROM vw_assessment_summary vas
            JOIN classrooms c ON c.id = vas.classroom_id
            WHERE c.academic_year_id = CAST(:ay AS uuid)
            {("AND vas.classroom_id = CAST(:cid AS uuid)" if classroom_id else "")}
            """,
            params,
        )
        out = paged_response_with_academic_year(
            academic_year_id=effective_ay,
            page=pg.page,
            per_page=pg.per_page,
            total=(count_row or {}).get("total", 0),
            items=items,
        )
        out.update(
            {
                "total_classrooms": (mrow or {}).get("total_classrooms") or 0,
                "total_students": (mrow or {}).get("total_students") or 0,
                "active_assessments": (mrow or {}).get("active_assessments") or 0,
                "completed_assessments": (mrow or {}).get("completed_assessments") or 0,
                "total_assessments": (mrow or {}).get("total_assessments") or 0,
            }
        )
        return out

    if is_teacher_like(ctx.role):
        base_sql += """
          AND EXISTS (
            SELECT 1 FROM my_classrooms mc
            WHERE mc.teacher_id = CAST(:tid_sc AS uuid)
              AND mc.classroom_id = vas.classroom_id
          )
        """
        params["tid_sc"] = str(ctx.active_profile_id)
    else:
        base_sql += " AND vas.classroom_id = ANY(CAST(:cids AS uuid[]))"
        params["cids"] = [str(x) for x in (cscope["effective_classroom_ids"] or [])]

    count_row = await fetch_one(db, f"SELECT COUNT(*)::int AS total FROM ({base_sql}) q", params)
    items = await fetch_all(
        db,
        f"{base_sql} ORDER BY vas.classroom_id LIMIT {pg.per_page} OFFSET {pg.offset}",
        params,
    )

    agg_from = """
        FROM vw_assessment_summary vas
        JOIN classrooms c ON c.id = vas.classroom_id
        WHERE c.academic_year_id = CAST(:ay AS uuid)
    """
    if classroom_id:
        agg_from += " AND vas.classroom_id = CAST(:cid AS uuid)"
    if is_teacher_like(ctx.role):
        agg_from += """
          AND EXISTS (
            SELECT 1 FROM my_classrooms mc
            WHERE mc.teacher_id = CAST(:tid_sc AS uuid)
              AND mc.classroom_id = vas.classroom_id
          )
        """
    else:
        agg_from += " AND vas.classroom_id = ANY(CAST(:cids AS uuid[]))"

    mrow = await fetch_one(
        db,
        f"""
        SELECT
          COUNT(*)::int AS total_assessments,
          COUNT(*) FILTER (WHERE vas.pending > 0)::int AS active_assessments,
          COUNT(*) FILTER (WHERE vas.completed > 0)::int AS completed_assessments
        {agg_from}
        """,
        params,
    )

    if is_teacher_like(ctx.role):
        tc_sql = """
            SELECT COUNT(*)::int AS n FROM my_classrooms mc
            JOIN classrooms c ON c.id = mc.classroom_id
            WHERE mc.teacher_id = CAST(:tid AS uuid)
              AND c.academic_year_id = CAST(:ay AS uuid)
        """
        ts_sql = """
            SELECT COUNT(DISTINCT cs.student_id)::int AS n
            FROM classroom_students cs
            JOIN my_classrooms mc ON mc.classroom_id = cs.classroom_id
            JOIN classrooms c ON c.id = cs.classroom_id
            WHERE mc.teacher_id = CAST(:tid AS uuid)
              AND c.academic_year_id = CAST(:ay AS uuid)
        """
        tc_params = {"tid": str(ctx.active_profile_id), "ay": str(effective_ay)}
        if classroom_id:
            tc_sql += " AND mc.classroom_id = CAST(:cid AS uuid)"
            ts_sql += " AND cs.classroom_id = CAST(:cid AS uuid)"
            tc_params["cid"] = str(classroom_id)
        tc_row = await fetch_one(db, tc_sql, tc_params)
        ts_row = await fetch_one(db, ts_sql, tc_params)
        total_classrooms = (tc_row or {}).get("n") or 0
        total_students = (ts_row or {}).get("n") or 0
    else:
        tc_row = await fetch_one(
            db,
            f"""
            SELECT COUNT(DISTINCT vas.classroom_id)::int AS n
            {agg_from}
            """,
            params,
        )
        ts_row = await fetch_one(
            db,
            f"""
            SELECT COUNT(DISTINCT cs.student_id)::int AS n
            FROM classroom_students cs
            WHERE cs.classroom_id = ANY(CAST(:cids2 AS uuid[]))
              AND EXISTS (
                SELECT 1 FROM classrooms c
                WHERE c.id = cs.classroom_id
                  AND c.academic_year_id = CAST(:ay AS uuid)
              )
            """,
            {
                "cids2": [str(x) for x in (cscope["effective_classroom_ids"] or [])],
                "ay": str(effective_ay),
            },
        )
        total_classrooms = (tc_row or {}).get("n") or 0
        total_students = (ts_row or {}).get("n") or 0

    out = paged_response_with_academic_year(
        academic_year_id=effective_ay,
        page=pg.page,
        per_page=pg.per_page,
        total=(count_row or {}).get("total", 0),
        items=items,
    )
    out.update(
        {
            "total_classrooms": total_classrooms,
            "total_students": total_students,
            "active_assessments": (mrow or {}).get("active_assessments") or 0,
            "completed_assessments": (mrow or {}).get("completed_assessments") or 0,
            "total_assessments": (mrow or {}).get("total_assessments") or 0,
        }
    )
    return out


@router.get("/teacher/attendance")
async def teacher_attendance_v1(
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
    academic_year_id: UUID | None = Query(None, description="Ano letivo. Se omitido, usa is_primary=true."),
    classroom_id: UUID | None = Query(None, description="Filtrar presenças por turma."),
    pg: Annotated[PageArgs, Depends(pagination_params)] = PageArgs(1, 50),
):
    effective_ay = await resolve_academic_year_id(db, academic_year_id)
    sql = """
        SELECT aal.* FROM assessment_attendance_list aal
        JOIN assessment_schedules ass ON ass.id = aal.assessment_schedules_id
        JOIN classrooms c ON c.id = ass.classroom_id
        JOIN classroom_teachers ct ON ct.classroom_id = ass.classroom_id
        WHERE ct.teacher_id = CAST(:tid AS uuid)
          AND c.academic_year_id = CAST(:ay AS uuid)
          AND aal.status IS DISTINCT FROM 'out'::attendance_list
        """
    params: dict[str, Any] = {"tid": str(ctx.active_profile_id), "ay": str(effective_ay)}
    if classroom_id is not None:
        ok = await fetch_one(
            db,
            """
            SELECT 1 AS ok FROM my_classrooms mc
            WHERE mc.teacher_id = CAST(:tid2 AS uuid)
              AND mc.classroom_id = CAST(:cid AS uuid)
            LIMIT 1
            """,
            {"tid2": str(ctx.active_profile_id), "cid": str(classroom_id)},
        )
        if not ok:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Turma fora do vínculo do professor")
        sql += " AND ass.classroom_id = CAST(:cid AS uuid)"
        params["cid"] = str(classroom_id)
    count_row = await fetch_one(db, f"SELECT COUNT(*)::int AS total FROM ({sql}) q", params)
    items = await fetch_all(
        db,
        f"{sql} ORDER BY aal.id DESC LIMIT {pg.per_page} OFFSET {pg.offset}",
        params,
    )
    return paged_response_with_academic_year(
        academic_year_id=effective_ay,
        page=pg.page,
        per_page=pg.per_page,
        total=(count_row or {}).get("total", 0),
        items=items,
    )


def _admin_classroom_scope_sql(school_ids: list[UUID] | None) -> tuple[str, dict[str, Any]]:
    """Fragmento `WHERE` adicional para turmas no ano (opcionalmente recorte por árvore de escola)."""
    if not school_ids:
        return "", {}
    sid = [str(x) for x in school_ids]
    return " AND c.school_id = ANY(CAST(:_school_ids AS uuid[]))", {"_school_ids": sid}


@router.get("/admin/dashboard/kpi")
async def admin_kpi_v1(
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
    academic_year_id: UUID | None = Query(None, description="Ano letivo. Se omitido, usa is_primary=true."),
    school_id: UUID | None = Query(
        None,
        description="Raiz da escola: KPIs limitados à subárvore (raiz + descendentes).",
    ),
):
    ay = await resolve_academic_year_id(db, academic_year_id)
    school_ids: list[UUID] | None = await resolve_admin_dashboard_school_ids(
        db, ctx, school_id
    )
    extra_sql, extra_params = _admin_classroom_scope_sql(school_ids)
    params: dict[str, Any] = {"ay": str(ay), **extra_params}

    schools_sql = "SELECT COUNT(*)::int AS n FROM schools"
    if school_ids:
        schools_sql = "SELECT COUNT(*)::int AS n FROM schools WHERE id = ANY(CAST(:_school_ids AS uuid[]))"

    row = await fetch_one(
        db,
        f"""
        SELECT
          CAST(:ay AS uuid) AS academic_year_id,
          ({schools_sql}) AS total_schools,
          (SELECT COUNT(*)::int FROM classrooms c
            WHERE c.academic_year_id = CAST(:ay AS uuid) {extra_sql}) AS total_classrooms,
          (
            SELECT COUNT(DISTINCT cs.student_id)::int
            FROM classroom_students cs
            JOIN classrooms c ON c.id = cs.classroom_id
            WHERE c.academic_year_id = CAST(:ay AS uuid) {extra_sql}
          ) AS total_students,
          (
            SELECT COUNT(DISTINCT ct.teacher_id)::int
            FROM classroom_teachers ct
            JOIN classrooms c ON c.id = ct.classroom_id
            WHERE c.academic_year_id = CAST(:ay AS uuid) {extra_sql}
          ) AS total_teachers,
          (
            SELECT COUNT(DISTINCT s.assessment_id)::int
            FROM assessment_schedules s
            JOIN classrooms c ON c.id = s.classroom_id
            WHERE c.academic_year_id = CAST(:ay AS uuid) {extra_sql}
          ) AS total_assessments,
          (
            SELECT COUNT(DISTINCT s.assessment_id)::int
            FROM assessment_schedules s
            JOIN classrooms c ON c.id = s.classroom_id
            WHERE c.academic_year_id = CAST(:ay AS uuid) {extra_sql}
              AND s.end_time >= (now() AT TIME ZONE 'utc') - interval '180 days'
          ) AS active_assessments,
          (
            SELECT COUNT(*)::int
            FROM assessment_results ar
            JOIN classrooms c ON c.id = ar.classroom_id
            WHERE c.academic_year_id = CAST(:ay AS uuid) {extra_sql}
              AND lower(COALESCE(ar.status::text, '')) IN ('graded', 'submitted')
          ) AS completed_assessments,
          (
            SELECT COUNT(*)::int
            FROM assessment_schedules s
            JOIN classrooms c ON c.id = s.classroom_id
            WHERE c.academic_year_id = CAST(:ay AS uuid) {extra_sql}
          ) AS schedules_count,
          (
            SELECT COUNT(DISTINCT ar.student_id)::int
            FROM assessment_results ar
            JOIN classrooms c ON c.id = ar.classroom_id
            WHERE c.academic_year_id = CAST(:ay AS uuid) {extra_sql}
          ) AS students_with_results_count,
          (
            SELECT COALESCE(ROUND(AVG(ar.score)::numeric, 2), 0)::float
            FROM assessment_results ar
            JOIN classrooms c ON c.id = ar.classroom_id
            WHERE c.academic_year_id = CAST(:ay AS uuid) {extra_sql}
              AND ar.score IS NOT NULL
          ) AS avg_score
        """,
        params,
    )
    out = dict(row or {})
    # Aliases legados / extras para clientes que ainda leem chaves antigas
    out["classrooms_count"] = out.get("total_classrooms")
    out["assessments_count"] = out.get("total_assessments")
    # Heurística: agendamentos no ano menos linhas de resultado concluídas (proxy de “pendente”).
    sch = int(out.get("schedules_count") or 0)
    comp = int(out.get("completed_assessments") or 0)
    out["pending_assessments"] = max(0, sch - comp)
    return out


_DOW_PT = ("Dom", "Seg", "Ter", "Qua", "Qui", "Sex", "Sáb")


@router.get("/admin/dashboard/engagement")
async def admin_engagement_v1(
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
    academic_year_id: UUID | None = Query(None, description="Ano letivo. Se omitido, usa is_primary=true."),
    school_id: UUID | None = Query(None, description="Recorte por árvore de escola (admin)."),
    series: str = Query(
        "weekday",
        description="`weekday` (padrão): contagens por dia da semana (submissões). "
        "`classrooms`: agregado por turma.",
    ),
):
    ay = await resolve_academic_year_id(db, academic_year_id)
    school_ids: list[UUID] | None = await resolve_admin_dashboard_school_ids(
        db, ctx, school_id
    )
    extra_sql, extra_params = _admin_classroom_scope_sql(school_ids)
    params: dict[str, Any] = {"ay": str(ay), **extra_params}

    if (series or "").lower().strip() == "classrooms":
        return await fetch_all(
            db,
            f"""
            SELECT
              c.id AS classroom_id,
              c.name AS classroom_name,
              COUNT(DISTINCT ar.student_id)::int AS students_engaged,
              COUNT(ar.id)::int AS total_results,
              COALESCE(ROUND(AVG(ar.score)::numeric, 2), 0)::float AS avg_score
            FROM classrooms c
            LEFT JOIN assessment_results ar ON ar.classroom_id = c.id
            WHERE c.academic_year_id = CAST(:ay AS uuid) {extra_sql}
            GROUP BY c.id, c.name
            ORDER BY students_engaged DESC, total_results DESC
            LIMIT 500
            """,
            params,
        )

    rows = await fetch_all(
        db,
        f"""
        SELECT
          (EXTRACT(DOW FROM ar.submitted_at AT TIME ZONE 'UTC'))::int AS dow,
          COUNT(*)::int AS count
        FROM assessment_results ar
        JOIN classrooms c ON c.id = ar.classroom_id
        WHERE c.academic_year_id = CAST(:ay AS uuid) {extra_sql}
          AND ar.submitted_at IS NOT NULL
        GROUP BY 1
        ORDER BY 1
        """,
        params,
    )
    by_dow = {int(r["dow"]): int(r["count"]) for r in rows}
    out: list[dict[str, Any]] = []
    for i, label in enumerate(_DOW_PT):
        out.append({"day": label, "weekday": label, "count": by_dow.get(i, 0)})
    return out


@router.get("/agenda")
async def agenda_v1(
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
    academic_year_id: UUID | None = Query(None, description="Ano letivo. Se omitido, usa is_primary=true."),
    start_date: str | None = Query(
        None,
        description="YYYY-MM-DD — início do intervalo (interseção com start_date/end_date do evento).",
    ),
    end_date: str | None = Query(
        None,
        description="YYYY-MM-DD — fim do intervalo (inclusive).",
    ),
    classroom_id: UUID | None = Query(None, description="Filtrar eventos desta turma."),
    pg: Annotated[PageArgs, Depends(pagination_params)] = PageArgs(1, 50),
):
    effective_ay = await resolve_academic_year_id(db, academic_year_id)
    cscope = await get_effective_classroom_scope(db, ctx)
    if cscope["is_admin_like"]:
        base_sql = """
        SELECT va.*
        FROM vw_agenda va
        LEFT JOIN classrooms c ON c.id = va.classroom_id
        WHERE va.classroom_id IS NULL OR c.academic_year_id = CAST(:ay AS uuid)
        """
        params: dict[str, Any] = {"ay": str(effective_ay)}
    else:
        base_sql = """
        SELECT va.*
        FROM vw_agenda va
        LEFT JOIN classrooms c ON c.id = va.classroom_id
        WHERE person_id = CAST(:pid AS uuid)
           OR (
             classroom_id = ANY(CAST(:cids AS uuid[]))
             AND (va.classroom_id IS NULL OR c.academic_year_id = CAST(:ay AS uuid))
           )
        """
        params = {
            "pid": str(ctx.active_profile_id),
            "cids": [str(x) for x in (cscope["effective_classroom_ids"] or [])],
            "ay": str(effective_ay),
        }
    if start_date and end_date:
        base_sql += """
          AND va.start_date <= :ag_end
          AND COALESCE(va.end_date, va.start_date) >= :ag_start
        """
        params["ag_start"] = start_date
        params["ag_end"] = end_date
    if classroom_id is not None:
        if not cscope["is_admin_like"] and str(classroom_id) not in {
            str(x) for x in (cscope["effective_classroom_ids"] or [])
        }:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Turma fora do escopo")
        base_sql += " AND va.classroom_id = CAST(:ag_cid AS uuid)"
        params["ag_cid"] = str(classroom_id)
    count_row = await fetch_one(db, f"SELECT COUNT(*)::int AS total FROM ({base_sql}) q", params)
    items = await fetch_all(
        db,
        f"{base_sql} ORDER BY start_date DESC NULLS LAST LIMIT {pg.per_page} OFFSET {pg.offset}",
        params,
    )
    return paged_response_with_academic_year(
        academic_year_id=effective_ay,
        page=pg.page,
        per_page=pg.per_page,
        total=(count_row or {}).get("total", 0),
        items=items,
    )
