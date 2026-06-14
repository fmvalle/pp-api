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


def _pedagogical_action(variation_pp: float) -> str:
    """Classificação por variação em pontos percentuais (aluno - média de comparação).

    < +5 p.p.  -> intervir
    +5..+10    -> orientar
    > +10      -> desafiar
    """
    if variation_pp > 10.0:
        return "desafiar"
    if variation_pp >= 5.0:
        return "orientar"
    return "intervir"


_ACTION_LABEL = {
    "intervir": "intervenção",
    "orientar": "orientação",
    "desafiar": "desafio",
}


def _build_pedagogical_reading(components: list[dict[str, Any]]) -> dict[str, Any]:
    """Texto determinístico (sem IA) + componentes prioritários para intervenção."""
    intervir = [c for c in components if c["pedagogicalAction"] == "intervir"]
    orientar = [c for c in components if c["pedagogicalAction"] == "orientar"]
    desafiar = [c for c in components if c["pedagogicalAction"] == "desafiar"]
    intervir.sort(key=lambda c: c["variationPercentagePoints"])

    parts: list[str] = []
    if intervir:
        nomes = ", ".join(c["componentName"] for c in intervir)
        parts.append(f"Priorize intervenção em {nomes}")
    if orientar:
        nomes = ", ".join(c["componentName"] for c in orientar)
        parts.append(f"oriente {nomes}")
    if desafiar:
        nomes = ", ".join(c["componentName"] for c in desafiar)
        parts.append(f"desafie {nomes}")
    text = ("; ".join(parts) + ".") if parts else "Sem dados suficientes para leitura pedagógica."
    return {
        "text": text,
        "priorityComponents": [
            {
                "componentId": c["componentId"],
                "componentName": c["componentName"],
                "pedagogicalAction": c["pedagogicalAction"],
                "variationPercentagePoints": c["variationPercentagePoints"],
            }
            for c in intervir
        ],
    }


async def assessment_pedagogical_report_bundle(
    db: AsyncSession,
    *,
    assessment_id: UUID,
    classroom_id: UUID,
    academic_year_id: UUID,
    student_id: UUID | None,
) -> dict[str, Any]:
    """Relatório pedagógico por componente curricular (visão individual ou de turma)."""
    head = await fetch_one(
        db,
        """
        SELECT a.id AS assessment_id, a.title AS assessment_title, a.created_at AS assessment_date,
               a.type AS assessment_type,
               c.id AS classroom_id, c.name AS classroom_name,
               s.name AS school_name, c.school_id
        FROM classrooms c
        JOIN assessments a ON a.id = CAST(:aid AS uuid)
        LEFT JOIN schools s ON s.id = c.school_id
        WHERE c.id = CAST(:cid AS uuid)
          AND c.academic_year_id = CAST(:ay AS uuid)
        """,
        {"aid": str(assessment_id), "cid": str(classroom_id), "ay": str(academic_year_id)},
    )
    if not head:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "Turma não encontrada para a avaliação/ano letivo informados",
        )
    school_id = head.get("school_id")
    # Provas adaptativas só apresentam itens efetivamente respondidos (a base é
    # maior do que o conjunto aplicado a cada aluno/turma).
    is_adaptive = str(head.get("assessment_type") or "").lower() == "adaptive"
    total_questions = await fetch_one(
        db,
        "SELECT COUNT(*)::int AS n FROM questions_assessments WHERE assessment_id = CAST(:aid AS uuid)",
        {"aid": str(assessment_id)},
    )
    total_q = int((total_questions or {}).get("n") or 0)

    student_block: dict[str, Any] | None = None
    if student_id is not None:
        prof = await fetch_one(
            db,
            "SELECT id, full_name, email FROM vw_profiles WHERE id = CAST(:id AS uuid)",
            {"id": str(student_id)},
        )
        if not prof:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Aluno não encontrado")
        student_block = {
            "id": str(student_id),
            "name": prof.get("full_name") or "",
            "registrationCode": prof.get("registration_code") or prof.get("code"),
        }

    # --- Médias agregadas (acurácia média por aluno: turma/escola/sistema) ----
    avg_row = await fetch_one(
        db,
        """
        WITH per_student AS (
            SELECT student_id, classroom_id,
                   SUM(total_questions) AS tq, SUM(correct_answers) AS ca
            FROM vw_assessment_component_results
            WHERE assessment_id = CAST(:aid AS uuid)
            GROUP BY student_id, classroom_id
        ), acc AS (
            SELECT ps.student_id, ps.classroom_id, c.school_id,
                   CASE WHEN ps.tq > 0 THEN 100.0 * ps.ca / ps.tq ELSE 0 END AS accuracy
            FROM per_student ps
            JOIN classrooms c ON c.id = ps.classroom_id
        )
        SELECT
            AVG(accuracy) FILTER (WHERE classroom_id = CAST(:cid AS uuid)) AS classroom_avg,
            AVG(accuracy) FILTER (WHERE school_id = CAST(:sid AS uuid)) AS school_avg,
            AVG(accuracy) AS system_avg
        FROM acc
        """,
        {"aid": str(assessment_id), "cid": str(classroom_id), "sid": str(school_id)},
    )
    classroom_avg = float((avg_row or {}).get("classroom_avg") or 0.0)
    school_avg = float((avg_row or {}).get("school_avg") or 0.0)
    system_avg = float((avg_row or {}).get("system_avg") or 0.0)

    # --- Componentes (acurácia por disciplina) --------------------------------
    comp_rows = await fetch_all(
        db,
        """
        WITH comp AS (
            SELECT student_id, discipline_name, discipline_slug, area_slug,
                   total_questions, correct_answers,
                   CASE WHEN total_questions > 0
                        THEN 100.0 * correct_answers / total_questions ELSE 0 END AS acc
            FROM vw_assessment_component_results
            WHERE assessment_id = CAST(:aid AS uuid)
              AND classroom_id = CAST(:cid AS uuid)
        )
        SELECT discipline_name, discipline_slug, area_slug,
               AVG(acc) AS classroom_acc,
               SUM(total_questions) AS sum_tq,
               SUM(correct_answers) AS sum_ca
        FROM comp
        GROUP BY discipline_name, discipline_slug, area_slug
        ORDER BY discipline_name
        """,
        {"aid": str(assessment_id), "cid": str(classroom_id)},
    )
    student_comp: dict[str, dict[str, Any]] = {}
    if student_id is not None:
        srows = await fetch_all(
            db,
            """
            SELECT discipline_name, discipline_slug, area_slug,
                   total_questions, correct_answers,
                   CASE WHEN total_questions > 0
                        THEN 100.0 * correct_answers / total_questions ELSE 0 END AS acc
            FROM vw_assessment_component_results
            WHERE assessment_id = CAST(:aid AS uuid)
              AND classroom_id = CAST(:cid AS uuid)
              AND student_id = CAST(:sid AS uuid)
            """,
            {"aid": str(assessment_id), "cid": str(classroom_id), "sid": str(student_id)},
        )
        for r in srows:
            student_comp[str(r.get("discipline_name") or "")] = r

    areas = await fetch_all(db, "SELECT slug, name FROM curricular_areas", {})
    area_name_by_slug = {str(a["slug"]): a.get("name") for a in areas}

    def _area_name(slug: Any, fallback: str) -> str:
        if slug and str(slug) in area_name_by_slug:
            return str(area_name_by_slug[str(slug)])
        return fallback

    component_performance: list[dict[str, Any]] = []
    student_total_q = 0
    student_correct = 0
    for r in comp_rows:
        name = str(r.get("discipline_name") or "Sem componente")
        comparison_avg = float(r.get("classroom_acc") or 0.0)
        if student_id is not None:
            sc = student_comp.get(name)
            tq = int((sc or {}).get("total_questions") or 0)
            ca = int((sc or {}).get("correct_answers") or 0)
            student_accuracy = (100.0 * ca / tq) if tq else 0.0
        else:
            tq = int(r.get("sum_tq") or 0)
            ca = int(r.get("sum_ca") or 0)
            student_accuracy = comparison_avg
        student_total_q += tq
        student_correct += ca
        variation = round(student_accuracy - comparison_avg, 1)
        component_performance.append(
            {
                "componentId": str(r.get("discipline_slug") or name),
                "componentName": name,
                "areaName": _area_name(r.get("area_slug"), name),
                "totalQuestions": tq,
                "correctAnswers": ca,
                "studentAccuracy": round(student_accuracy, 1),
                "comparisonAverage": round(comparison_avg, 1),
                "variationPercentagePoints": variation,
                "pedagogicalAction": _pedagogical_action(variation),
            }
        )

    if student_id is not None:
        summary_total = student_total_q or total_q
        summary_correct = student_correct
        summary_accuracy = round((100.0 * summary_correct / summary_total), 1) if summary_total else 0.0
    else:
        summary_total = total_q
        summary_correct = 0
        summary_accuracy = round(classroom_avg, 1)

    summary = {
        "totalQuestions": summary_total,
        "correctAnswers": summary_correct,
        "accuracyPercentage": summary_accuracy,
        "classroomAverage": round(classroom_avg, 1),
        "schoolAverage": round(school_avg, 1),
        "systemAverage": round(system_avg, 1),
    }

    pedagogical_reading = _build_pedagogical_reading(component_performance)

    # --- Questão a questão -----------------------------------------------------
    q_rows = await fetch_all(
        db,
        """
        SELECT qa.question_id, qa.order_index,
               qi.question_type, qi.discipline_name, qi.discipline_slug, qi.area_slug,
               qi.description_html, qi.description_raw,
               (SELECT label FROM question_alternative
                 WHERE question_id = qi.id AND is_correct = true
                 ORDER BY order_index NULLS LAST LIMIT 1) AS correct_answer
        FROM questions_assessments qa
        JOIN question_item qi ON qi.id = qa.question_id
        WHERE qa.assessment_id = CAST(:aid AS uuid)
        ORDER BY qa.order_index
        """,
        {"aid": str(assessment_id)},
    )
    skill_rows = await fetch_all(
        db,
        """
        SELECT qit.question_id, qt.tag_type, qt.external_id AS code, qt.label AS description
        FROM question_item_tag qit
        JOIN question_tag qt ON qt.id = qit.tag_id
        WHERE qt.tag_type IN ('skill', 'topic')
        """,
        {},
    )
    skill_by_q: dict[str, dict[str, Any]] = {}
    for sr in skill_rows:
        qid = str(sr.get("question_id") or "")
        if not qid:
            continue
        # 'skill' tem prioridade sobre 'topic'
        if qid not in skill_by_q or sr.get("tag_type") == "skill":
            skill_by_q[qid] = {"code": sr.get("code"), "description": sr.get("description")}

    stat_rows = await fetch_all(
        db,
        """
        SELECT question_id,
               SUM(responses) FILTER (WHERE school_id = CAST(:sid AS uuid)) AS school_resp,
               SUM(correct)   FILTER (WHERE school_id = CAST(:sid AS uuid)) AS school_corr,
               SUM(responses) AS sys_resp,
               SUM(correct) AS sys_corr
        FROM vw_question_item_stats
        WHERE assessment_id = CAST(:aid AS uuid)
        GROUP BY question_id
        """,
        {"aid": str(assessment_id), "sid": str(school_id)},
    )
    stat_by_q: dict[str, dict[str, Any]] = {str(r.get("question_id")): r for r in stat_rows}

    # Alternativas de cada questão (enunciado, gabarito, texto).
    alt_rows = await fetch_all(
        db,
        """
        SELECT qa.question_id, alt.label, alt.text, alt.raw_text,
               alt.is_correct, alt.order_index
        FROM questions_assessments qa
        JOIN question_alternative alt ON alt.question_id = qa.question_id
        WHERE qa.assessment_id = CAST(:aid AS uuid)
        ORDER BY qa.order_index, alt.order_index NULLS LAST
        """,
        {"aid": str(assessment_id)},
    )
    alts_by_q: dict[str, list[dict[str, Any]]] = {}
    for ar in alt_rows:
        qid = str(ar.get("question_id") or "")
        if not qid:
            continue
        alts_by_q.setdefault(qid, []).append(ar)

    # Contagem de seleção por alternativa, no escopo da turma.
    sel_rows = await fetch_all(
        db,
        """
        SELECT qsr.question_id, alt.label, COUNT(*)::int AS cnt
        FROM question_student_responsed qsr
        JOIN question_alternative alt ON alt.id = qsr.response_id
        JOIN assessment_schedules ass ON ass.id = qsr.schedule_id
        WHERE qsr.assessment_id = CAST(:aid AS uuid)
          AND ass.classroom_id = CAST(:cid AS uuid)
        GROUP BY qsr.question_id, alt.label
        """,
        {"aid": str(assessment_id), "cid": str(classroom_id)},
    )
    sel_by_q: dict[str, dict[str, int]] = {}
    sel_total_by_q: dict[str, int] = {}
    for sr in sel_rows:
        qid = str(sr.get("question_id") or "")
        if not qid:
            continue
        label = sr.get("label") or ""
        cnt = int(sr.get("cnt") or 0)
        sel_by_q.setdefault(qid, {})[label] = cnt
        sel_total_by_q[qid] = sel_total_by_q.get(qid, 0) + cnt

    answer_by_q: dict[str, dict[str, Any]] = {}
    if student_id is not None:
        ans_rows = await fetch_all(
            db,
            """
            SELECT question_id, label, is_correct, correct_alternative
            FROM vw_student_answers_report
            WHERE student_id = CAST(:sid AS uuid)
              AND assessment_id = CAST(:aid AS uuid)
              AND classroom_id = CAST(:cid AS uuid)
            """,
            {"sid": str(student_id), "aid": str(assessment_id), "cid": str(classroom_id)},
        )
        answer_by_q = {str(r.get("question_id")): r for r in ans_rows}

    def _pct(corr: Any, resp: Any) -> float | None:
        c = int(corr or 0)
        n = int(resp or 0)
        return round(100.0 * c / n, 1) if n else None

    groups: dict[str, dict[str, Any]] = {}
    for q in q_rows:
        qid = str(q.get("question_id") or "")
        # Adaptativa: oculta itens sem nenhuma resposta no escopo (aluno/turma).
        if is_adaptive:
            has_response = (
                qid in answer_by_q
                if student_id is not None
                else sel_total_by_q.get(qid, 0) > 0
            )
            if not has_response:
                continue
        comp_name = str(q.get("discipline_name") or "Sem componente")
        area_name = _area_name(q.get("area_slug"), comp_name)
        skill = skill_by_q.get(qid) or {}
        stat = stat_by_q.get(qid) or {}
        ans = answer_by_q.get(qid)
        student_answer = ans.get("label") if ans else None
        is_correct = bool(ans.get("is_correct")) if ans else None
        if is_correct is None:
            status_label = "—"
        else:
            status_label = "Acertou" if is_correct else "Errou"
        sel_counts = sel_by_q.get(qid, {})
        sel_total = sel_total_by_q.get(qid, 0)
        alternatives = []
        for alt in alts_by_q.get(qid, []):
            label = alt.get("label") or ""
            cnt = int(sel_counts.get(label, 0))
            alternatives.append(
                {
                    "label": label,
                    "text": alt.get("text") or alt.get("raw_text") or "",
                    "isCorrect": bool(alt.get("is_correct")),
                    "orderIndex": int(alt.get("order_index") or 0),
                    "selectedCount": cnt,
                    "selectedPercentage": round(100.0 * cnt / sel_total, 1) if sel_total else None,
                }
            )
        question = {
            "questionNumber": int(q.get("order_index") or 0),
            "questionType": q.get("question_type"),
            "componentName": comp_name,
            "skillCode": skill.get("code"),
            "skillDescription": skill.get("description"),
            "correctAnswer": q.get("correct_answer"),
            "studentAnswer": student_answer,
            "isCorrect": is_correct,
            "statusLabel": status_label,
            "schoolAccuracyPercentage": _pct(stat.get("school_corr"), stat.get("school_resp")),
            "systemAccuracyPercentage": _pct(stat.get("sys_corr"), stat.get("sys_resp")),
            "description": q.get("description_html") or q.get("description_raw"),
            "totalResponses": sel_total,
            "alternatives": alternatives,
        }
        g = groups.setdefault(
            comp_name,
            {"areaName": area_name, "componentName": comp_name, "questions": []},
        )
        g["questions"].append(question)

    question_groups: list[dict[str, Any]] = []
    for g in groups.values():
        qs = g["questions"]
        qs.sort(key=lambda q: q["questionNumber"])
        correct_in_group = sum(1 for q in qs if q["isCorrect"] is True)
        answered = sum(1 for q in qs if q["isCorrect"] is not None)
        g["totalQuestions"] = len(qs)
        g["accuracyPercentage"] = round(100.0 * correct_in_group / answered, 1) if answered else None
        question_groups.append(g)
    # Ordena os componentes pela ordem das questões (menor número de questão do
    # grupo), de modo que a sequência siga a numeração das questões.
    question_groups.sort(
        key=lambda g: g["questions"][0]["questionNumber"] if g["questions"] else 0
    )

    logger.info(
        "[v1/reports/pedagogical] assessment_id=%s classroom_id=%s student_id=%s components=%s questions=%s",
        assessment_id,
        classroom_id,
        student_id,
        len(component_performance),
        len(q_rows),
    )

    return {
        "assessment": {
            "id": str(assessment_id),
            "title": head.get("assessment_title"),
            "date": str(head.get("assessment_date")) if head.get("assessment_date") else None,
            "totalQuestions": total_q,
        },
        "classroom": {
            "id": str(classroom_id),
            "name": head.get("classroom_name"),
            "school": head.get("school_name"),
        },
        "student": student_block,
        "summary": summary,
        "componentPerformance": component_performance,
        "pedagogicalReading": pedagogical_reading,
        "questionGroups": question_groups,
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
