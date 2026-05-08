"""Agregações e checagens de escopo para relatórios de avaliação (API v1)."""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import AuthContext
from app.v1._scope import get_effective_classroom_scope, get_effective_school_scope, is_admin_like, is_teacher_like
from app.v1._sql import fetch_all, fetch_one

logger = logging.getLogger(__name__)


async def assert_can_access_assessment_report_student(
    db: AsyncSession,
    ctx: AuthContext,
    *,
    student_id: UUID,
    assessment_id: UUID,
) -> None:
    """Aluno (self), admin, professor com vínculo turma+resultado, ou staff da escola do aluno."""
    if is_admin_like(ctx.role):
        return
    if str(student_id) == str(ctx.active_profile_id):
        return
    if is_teacher_like(ctx.role):
        row = await fetch_one(
            db,
            """
            SELECT 1
            FROM classroom_students cs
            INNER JOIN classroom_teachers ct
              ON ct.classroom_id = cs.classroom_id
             AND ct.teacher_id = CAST(:tid AS uuid)
            INNER JOIN assessment_results ar
              ON ar.student_id = cs.student_id
             AND ar.classroom_id = cs.classroom_id
            WHERE cs.student_id = CAST(:sid AS uuid)
              AND ar.assessment_id = CAST(:aid AS uuid)
            LIMIT 1
            """,
            {"tid": str(ctx.active_profile_id), "sid": str(student_id), "aid": str(assessment_id)},
        )
        if row:
            return
    prow = await fetch_one(
        db,
        "SELECT school_id, role::text AS role FROM vw_profiles WHERE id = CAST(:id AS uuid)",
        {"id": str(student_id)},
    )
    if not prow:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Student not found")
    sscope = await get_effective_school_scope(db, ctx)
    pschool = prow.get("school_id")
    if pschool and str(pschool) in {str(x) for x in (sscope["effective_school_ids"] or [])}:
        return
    raise HTTPException(status.HTTP_403_FORBIDDEN, "Fora do escopo")


async def load_classroom_row_by_id(db: AsyncSession, classroom_id: UUID) -> dict[str, Any] | None:
    return await fetch_one(
        db,
        """
        SELECT id, school_id, academic_year_id
        FROM classrooms
        WHERE id = CAST(:id AS uuid)
        """,
        {"id": str(classroom_id)},
    )


async def assert_actor_can_read_classroom(
    db: AsyncSession,
    ctx: AuthContext,
    classroom_id: UUID,
) -> None:
    if is_admin_like(ctx.role):
        return
    if is_teacher_like(ctx.role):
        row = await fetch_one(
            db,
            """
            SELECT 1 FROM my_classrooms mc
            WHERE mc.classroom_id = CAST(:cid AS uuid)
              AND mc.teacher_id = CAST(:tid AS uuid)
            LIMIT 1
            """,
            {"cid": str(classroom_id), "tid": str(ctx.active_profile_id)},
        )
        if row:
            return
    cscope = await get_effective_classroom_scope(db, ctx)
    cids = cscope.get("effective_classroom_ids") or []
    if str(classroom_id) in {str(x) for x in cids}:
        return
    raise HTTPException(status.HTTP_403_FORBIDDEN, "Fora do escopo")


async def classroom_assessment_report_envelope(
    db: AsyncSession,
    *,
    classroom_id: UUID,
    assessment_id: UUID,
    academic_year_id: UUID,
) -> dict[str, Any]:
    """Payload esperado pelo app (`AssessmentReportData` via JSON)."""
    student_rows = await fetch_all(
        db,
        """
        SELECT ar.student_id,
               COALESCE(p.full_name, '') AS full_name,
               p.email AS email,
               ar.score,
               ar.status
        FROM assessment_results ar
        LEFT JOIN vw_profiles p ON p.id = ar.student_id
        WHERE ar.classroom_id = CAST(:cid AS uuid)
          AND ar.assessment_id = CAST(:aid AS uuid)
        ORDER BY p.full_name NULLS LAST, ar.student_id
        """,
        {"cid": str(classroom_id), "aid": str(assessment_id)},
    )
    total_students = len(student_rows)
    completed = 0
    total_score = 0.0
    scored = 0
    for r in student_rows:
        st = (r.get("status") or "").lower()
        if st in ("submitted", "graded"):
            completed += 1
        sc = r.get("score")
        if sc is not None:
            total_score += float(sc)
            scored += 1
    average_score = (total_score / scored) if scored else 0.0

    summaries = await fetch_all(
        db,
        """
        SELECT v.question_id,
               MIN(v.order_index)::int AS order_index,
               SUM(v.response_count)::bigint AS response_count,
               SUM(v.correct_count)::bigint AS correct_count
        FROM vw_questions_report_summary v
        WHERE v.assessment_id = CAST(:aid AS uuid)
          AND v.classroom_id = CAST(:cid AS uuid)
        GROUP BY v.question_id
        """,
        {"aid": str(assessment_id), "cid": str(classroom_id)},
    )
    keys = await fetch_all(
        db,
        """
        SELECT question_id, order_index, question
        FROM vw_questions_assessments_answer_key
        WHERE assessment_id = CAST(:aid AS uuid)
        """,
        {"aid": str(assessment_id)},
    )
    key_by_q: dict[str, dict[str, Any]] = {}
    for k in keys:
        qid = str(k.get("question_id") or "")
        if qid:
            key_by_q[qid] = k

    question_summaries: list[dict[str, Any]] = []
    for s in summaries:
        qid = str(s.get("question_id") or "")
        if not qid:
            continue
        detail = key_by_q.get(qid) or {}
        qjson = detail.get("question")
        if not isinstance(qjson, dict):
            qjson = {}
        question_summaries.append(
            {
                "question_id": qid,
                "order_index": int(s.get("order_index") or 0),
                "correct_answers": int(s.get("correct_count") or 0),
                "total_answers": int(s.get("response_count") or 0),
                "question": qjson,
            }
        )
    question_summaries.sort(key=lambda x: x.get("order_index") or 0)

    return {
        "classroom_id": str(classroom_id),
        "assessment_id": str(assessment_id),
        "academic_year_id": str(academic_year_id),
        "total_students": total_students,
        "completed_students": completed,
        "average_score": round(average_score, 2),
        "question_summaries": question_summaries,
        "student_rows": student_rows,
    }


async def student_assessment_worksheet_bundle(
    db: AsyncSession,
    *,
    student_id: UUID,
    assessment_id: UUID,
    academic_year_id: UUID,
    classroom_id: UUID | None,
    schedule_id: UUID | None,
) -> dict[str, Any]:
    """Dados para a página de relatório individual (substitui PostgREST composto)."""
    params: dict[str, Any] = {
        "sid": str(student_id),
        "aid": str(assessment_id),
        "ay": str(academic_year_id),
    }
    ar_sql = """
        SELECT ar.*,
               a.title AS assessment_title,
               a.type AS assessment_type,
               c.id AS classroom_uuid,
               c.name AS classroom_name
        FROM assessment_results ar
        JOIN classrooms c ON c.id = ar.classroom_id
        JOIN assessments a ON a.id = ar.assessment_id
        WHERE ar.student_id = CAST(:sid AS uuid)
          AND ar.assessment_id = CAST(:aid AS uuid)
          AND c.academic_year_id = CAST(:ay AS uuid)
    """
    if classroom_id:
        ar_sql += " AND ar.classroom_id = CAST(:cid AS uuid)"
        params["cid"] = str(classroom_id)
    if schedule_id:
        ar_sql += " AND ar.schedule_id = CAST(:sch AS uuid)"
        params["sch"] = str(schedule_id)
    ar_sql += " LIMIT 1"
    ar_row = await fetch_one(db, ar_sql, params)
    if not ar_row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Assessment result not found for this year/context")

    prof = await fetch_one(db, "SELECT * FROM vw_profiles WHERE id = CAST(:id AS uuid)", {"id": str(student_id)})
    if not prof:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Student not found")

    cid = ar_row.get("classroom_id") or ar_row.get("classroom_uuid")
    answers_params: dict[str, Any] = {
        "sid": str(student_id),
        "aid": str(assessment_id),
        "cid": str(cid),
    }
    answers = await fetch_all(
        db,
        """
        SELECT *
        FROM vw_student_answers_report
        WHERE student_id = CAST(:sid AS uuid)
          AND assessment_id = CAST(:aid AS uuid)
          AND classroom_id = CAST(:cid AS uuid)
        ORDER BY question_id
        """,
        answers_params,
    )

    q_orders = await fetch_all(
        db,
        """
        SELECT question_id, order_index
        FROM questions_assessments
        WHERE assessment_id = CAST(:aid AS uuid)
        """,
        {"aid": str(assessment_id)},
    )
    order_map = {str(r["question_id"]): int(r["order_index"] or 0) for r in q_orders if r.get("question_id")}

    keys = await fetch_all(
        db,
        """
        SELECT question_id, question
        FROM vw_questions_assessments_answer_key
        WHERE assessment_id = CAST(:aid AS uuid)
        """,
        {"aid": str(assessment_id)},
    )
    details: dict[str, dict[str, Any]] = {}
    for k in keys:
        qid = str(k.get("question_id") or "")
        if not qid:
            continue
        qj = k.get("question")
        details[qid] = qj if isinstance(qj, dict) else {}

    schedule_start = None
    sch_id = ar_row.get("schedule_id")
    if sch_id:
        srow = await fetch_one(
            db,
            "SELECT start_time FROM assessment_schedules WHERE id = CAST(:id AS uuid)",
            {"id": str(sch_id)},
        )
        if srow and srow.get("start_time") is not None:
            schedule_start = str(srow["start_time"])

    assessment = {
        "id": str(assessment_id),
        "title": ar_row.get("assessment_title"),
        "type": ar_row.get("assessment_type"),
    }
    classroom = {
        "id": str(cid) if cid else "",
        "name": ar_row.get("classroom_name") or "",
    }

    logger.info(
        "[v1/reports/worksheet] student_id=%s assessment_id=%s classroom_id=%s academic_year_id=%s answers=%s",
        student_id,
        assessment_id,
        cid,
        academic_year_id,
        len(answers),
    )

    drop = {"assessment_title", "assessment_type", "classroom_uuid", "classroom_name"}
    assessment_result = {k: v for k, v in ar_row.items() if k not in drop}
    assessment_result["assessments"] = assessment
    assessment_result["classrooms"] = classroom
    return {
        "student_profile": prof,
        "assessment_result": assessment_result,
        "assessment": assessment,
        "classroom": classroom,
        "student_answers": answers,
        "question_order": order_map,
        "question_details": details,
        "schedule_start_time": schedule_start,
    }


async def assert_can_read_schedule_report(
    db: AsyncSession,
    ctx: AuthContext,
    *,
    schedule_id: UUID,
    academic_year_id: UUID,
) -> None:
    row = await fetch_one(
        db,
        """
        SELECT ass.classroom_id
        FROM assessment_schedules ass
        JOIN classrooms c ON c.id = ass.classroom_id
        WHERE ass.id = CAST(:sid AS uuid)
          AND c.academic_year_id = CAST(:ay AS uuid)
        """,
        {"sid": str(schedule_id), "ay": str(academic_year_id)},
    )
    if not row or not row.get("classroom_id"):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Schedule not found for this academic year")
    await assert_actor_can_read_classroom(db, ctx, UUID(str(row["classroom_id"])))


async def assessment_schedule_report_bundle(
    db: AsyncSession,
    *,
    schedule_id: UUID,
    academic_year_id: UUID,
) -> dict[str, Any]:
    """Substitui `TeacherService.getAssessmentReport` (schedule + resultados + estatísticas)."""
    sched = await fetch_one(
        db,
        """
        SELECT ass.*,
               a.title AS assessment_title,
               a.type AS assessment_type,
               a.description AS assessment_description,
               c.name AS classroom_name,
               c.code AS classroom_code,
               s.name AS school_name
        FROM assessment_schedules ass
        JOIN assessments a ON a.id = ass.assessment_id
        JOIN classrooms c ON c.id = ass.classroom_id
        LEFT JOIN schools s ON s.id = ass.school_id
        WHERE ass.id = CAST(:sid AS uuid)
          AND c.academic_year_id = CAST(:ay AS uuid)
        """,
        {"sid": str(schedule_id), "ay": str(academic_year_id)},
    )
    if not sched:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Schedule not found for this academic year")

    results = await fetch_all(
        db,
        """
        SELECT ar.*,
               p.full_name AS student_full_name,
               p.email AS student_email
        FROM assessment_results ar
        LEFT JOIN vw_profiles p ON p.id = ar.student_id
        WHERE ar.schedule_id = CAST(:sid AS uuid)
        ORDER BY p.full_name NULLS LAST, ar.submitted_at DESC NULLS LAST
        """,
        {"sid": str(schedule_id)},
    )
    total = len(results)
    completed = sum(1 for r in results if str(r.get("status") or "").lower() in ("submitted", "graded"))
    total_score = 0.0
    scored = 0
    for r in results:
        sc = r.get("score")
        if sc is not None:
            total_score += float(sc)
            scored += 1
    avg = (total_score / scored) if scored else 0.0

    schedule_payload = {
        **{k: v for k, v in sched.items()},
        "assessments": {
            "id": str(sched.get("assessment_id")),
            "title": sched.get("assessment_title"),
            "type": sched.get("assessment_type"),
            "description": sched.get("assessment_description"),
        },
        "classrooms": {
            "id": str(sched.get("classroom_id")),
            "name": sched.get("classroom_name"),
            "code": sched.get("classroom_code"),
            "schools": {"name": sched.get("school_name")},
        },
    }
    out_results = []
    for r in results:
        row = dict(r)
        row["profiles"] = {
            "id": row.get("student_id"),
            "full_name": row.pop("student_full_name", None),
            "email": row.pop("student_email", None),
        }
        out_results.append(row)

    return {
        "schedule": schedule_payload,
        "results": out_results,
        "statistics": {
            "total_students": total,
            "completed_count": completed,
            "average_score": round(avg, 2),
        },
    }
