"""Camada de macro avaliação (agrupamento de cadernos/`assessments`).

A experiência do professor/gestor passa a ser orientada por `macro_assessments`,
mantendo os agendamentos e resultados por `assessments` (caderno). Este módulo
resolve o contexto de macro avaliação e consolida componentes e desempenho por
aluno de todos os cadernos vinculados, preservando compatibilidade com links
antigos que ainda enviam um `assessment_id`.
"""

import logging
from typing import Any
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.v1._sql import fetch_all, fetch_one
from app.v1.report_bundle import _build_pedagogical_reading, _pedagogical_action

logger = logging.getLogger(__name__)


async def resolve_macro_scope(
    db: AsyncSession,
    *,
    route_id: UUID,
    classroom_id: UUID,
) -> dict[str, Any]:
    """Resolve o `route_id` da rota de relatório para o contexto de macro avaliação.

    Compatibilidade:
    - `route_id` é um `macro_assessment_id` → usa-se diretamente.
    - `route_id` é um `assessment_id` com `macro_assessment_id` → resolve a macro.
    - `route_id` é um `assessment_id` sem macro → comportamento antigo (caderno único).

    Os cadernos retornados são apenas os agendados para a turma (`classroom_id`).
    Retorna dict: ``{macro_id, macro_title, is_macro, assessments[], assessment_ids[]}``.
    """
    macro_row = await fetch_one(
        db,
        "SELECT id, title FROM macro_assessments WHERE id = CAST(:id AS uuid)",
        {"id": str(route_id)},
    )

    macro_id: str | None = None
    macro_title: str = ""
    is_macro = False

    if macro_row:
        macro_id = str(macro_row.get("id"))
        macro_title = macro_row.get("title") or ""
        is_macro = True
    else:
        assess_row = await fetch_one(
            db,
            """
            SELECT id, title, description, type, macro_assessment_id
            FROM assessments
            WHERE id = CAST(:id AS uuid)
            """,
            {"id": str(route_id)},
        )
        if not assess_row:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND,
                "Avaliação/macro avaliação não encontrada",
            )
        macro_fk = assess_row.get("macro_assessment_id")
        if macro_fk:
            macro_id = str(macro_fk)
            is_macro = True
            mt = await fetch_one(
                db,
                "SELECT title FROM macro_assessments WHERE id = CAST(:id AS uuid)",
                {"id": macro_id},
            )
            macro_title = (mt or {}).get("title") or assess_row.get("title") or ""
        else:
            # Caderno avulso (sem macro): contexto antigo de assessment único.
            macro_id = None
            macro_title = assess_row.get("title") or ""

    # Cadernos vinculados e agendados para a turma.
    if is_macro and macro_id is not None:
        assessments = await fetch_all(
            db,
            """
            SELECT a.id, a.title, a.description, a.type
            FROM assessments a
            WHERE a.macro_assessment_id = CAST(:mid AS uuid)
              AND EXISTS (
                SELECT 1 FROM assessment_schedules s
                WHERE s.assessment_id = a.id
                  AND s.classroom_id = CAST(:cid AS uuid)
              )
            ORDER BY a.created_at, a.title
            """,
            {"mid": macro_id, "cid": str(classroom_id)},
        )
    else:
        # Sem macro: apenas o próprio assessment recebido.
        assessments = await fetch_all(
            db,
            """
            SELECT a.id, a.title, a.description, a.type
            FROM assessments a
            WHERE a.id = CAST(:id AS uuid)
            """,
            {"id": str(route_id)},
        )

    assessment_ids = [str(a["id"]) for a in assessments]
    if not assessment_ids:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "Nenhum caderno desta macro avaliação está agendado para a turma",
        )

    return {
        "macro_id": macro_id,
        "macro_title": macro_title,
        "is_macro": is_macro,
        "assessments": [
            {
                "id": str(a["id"]),
                "title": a.get("title") or "",
                "description": a.get("description"),
                "type": a.get("type"),
            }
            for a in assessments
        ],
        "assessment_ids": assessment_ids,
    }


async def _classroom_head(db: AsyncSession, classroom_id: UUID) -> dict[str, Any]:
    row = await fetch_one(
        db,
        """
        SELECT c.id, c.name, c.school_id, s.name AS school_name
        FROM classrooms c
        LEFT JOIN schools s ON s.id = c.school_id
        WHERE c.id = CAST(:cid AS uuid)
        """,
        {"cid": str(classroom_id)},
    )
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Turma não encontrada")
    return row


async def macro_report_context(
    db: AsyncSession,
    *,
    scope: dict[str, Any],
    classroom_id: UUID,
) -> dict[str, Any]:
    """Contexto da macro avaliação: macro + turma + cadernos."""
    head = await _classroom_head(db, classroom_id)
    return {
        "macroAssessment": {
            "id": scope.get("macro_id"),
            "title": scope.get("macro_title") or "",
        },
        "classroom": {
            "id": str(classroom_id),
            "name": head.get("name") or "",
            "school": head.get("school_name"),
        },
        "assessments": scope["assessments"],
    }


async def macro_components_report(
    db: AsyncSession,
    *,
    assessment_ids: list[str],
    classroom_id: UUID,
) -> dict[str, Any]:
    """Componentes consolidados de todos os cadernos da macro avaliação (turma).

    Consolidação ponderada por número de itens (não média simples) para totais por
    componente; médias turma/escola/sistema usam acurácia por aluno.
    """
    head = await _classroom_head(db, classroom_id)
    school_id = head.get("school_id")

    # Médias por aluno consolidando todos os cadernos.
    avg_row = await fetch_one(
        db,
        """
        WITH per_student AS (
            SELECT student_id, classroom_id,
                   SUM(total_questions) AS tq, SUM(correct_answers) AS ca
            FROM vw_assessment_component_results
            WHERE assessment_id = ANY(CAST(:aids AS uuid[]))
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
        {"aids": assessment_ids, "cid": str(classroom_id), "sid": str(school_id)},
    )
    classroom_avg = float((avg_row or {}).get("classroom_avg") or 0.0)
    school_avg = float((avg_row or {}).get("school_avg") or 0.0)
    system_avg = float((avg_row or {}).get("system_avg") or 0.0)

    comp_rows = await fetch_all(
        db,
        """
        SELECT discipline_name, discipline_slug, area_slug,
               SUM(total_questions) AS sum_tq,
               SUM(correct_answers) AS sum_ca
        FROM vw_assessment_component_results
        WHERE assessment_id = ANY(CAST(:aids AS uuid[]))
          AND classroom_id = CAST(:cid AS uuid)
        GROUP BY discipline_name, discipline_slug, area_slug
        ORDER BY discipline_name
        """,
        {"aids": assessment_ids, "cid": str(classroom_id)},
    )

    areas = await fetch_all(db, "SELECT slug, name FROM curricular_areas", {})
    area_name_by_slug = {str(a["slug"]): a.get("name") for a in areas}

    def _area_name(slug: Any, fallback: str) -> str:
        if slug and str(slug) in area_name_by_slug:
            return str(area_name_by_slug[str(slug)])
        return fallback

    component_performance: list[dict[str, Any]] = []
    for r in comp_rows:
        name = str(r.get("discipline_name") or "Sem componente")
        tq = int(r.get("sum_tq") or 0)
        ca = int(r.get("sum_ca") or 0)
        accuracy = round((100.0 * ca / tq), 1) if tq else 0.0
        # Visão de turma: acurácia consolidada vs. ela mesma (variação 0),
        # mantendo a semântica do relatório por turma já existente.
        variation = 0.0
        component_performance.append(
            {
                "componentId": str(r.get("discipline_slug") or name),
                "componentName": name,
                "areaName": _area_name(r.get("area_slug"), name),
                "totalQuestions": tq,
                "correctAnswers": ca,
                "studentAccuracy": accuracy,
                "comparisonAverage": accuracy,
                "variationPercentagePoints": variation,
                "pedagogicalAction": _pedagogical_action(variation),
            }
        )

    # Total de itens da base (todos os cadernos).
    total_row = await fetch_one(
        db,
        """
        SELECT COUNT(*)::int AS n
        FROM questions_assessments
        WHERE assessment_id = ANY(CAST(:aids AS uuid[]))
        """,
        {"aids": assessment_ids},
    )
    total_q = int((total_row or {}).get("n") or 0)

    summary = {
        "totalQuestions": total_q,
        "correctAnswers": 0,
        "accuracyPercentage": round(classroom_avg, 1),
        "classroomAverage": round(classroom_avg, 1),
        "schoolAverage": round(school_avg, 1),
        "systemAverage": round(system_avg, 1),
    }

    return {
        "summary": summary,
        "componentPerformance": component_performance,
        "pedagogicalReading": _build_pedagogical_reading(component_performance),
    }


async def macro_students_report(
    db: AsyncSession,
    *,
    assessment_ids: list[str],
    classroom_id: UUID,
) -> dict[str, Any]:
    """Desempenho por aluno consolidando a macro avaliação.

    Uma linha por (aluno, caderno), com `assessmentTitle`, status, acertos, total
    de itens, percentual e data de envio.
    """
    rows = await fetch_all(
        db,
        """
        SELECT ar.student_id,
               COALESCE(p.full_name, '') AS full_name,
               ar.assessment_id,
               a.title AS assessment_title,
               ar.status,
               ar.score,
               ar.submitted_at
        FROM assessment_results ar
        JOIN assessments a ON a.id = ar.assessment_id
        LEFT JOIN vw_profiles p ON p.id = ar.student_id
        WHERE ar.classroom_id = CAST(:cid AS uuid)
          AND ar.assessment_id = ANY(CAST(:aids AS uuid[]))
        ORDER BY p.full_name NULLS LAST, a.title
        """,
        {"aids": assessment_ids, "cid": str(classroom_id)},
    )

    # Acertos/itens respondidos por (aluno, caderno).
    correct_rows = await fetch_all(
        db,
        """
        SELECT student_id, assessment_id,
               SUM(total_questions) AS tq, SUM(correct_answers) AS ca
        FROM vw_assessment_component_results
        WHERE assessment_id = ANY(CAST(:aids AS uuid[]))
          AND classroom_id = CAST(:cid AS uuid)
        GROUP BY student_id, assessment_id
        """,
        {"aids": assessment_ids, "cid": str(classroom_id)},
    )
    correct_by: dict[tuple[str, str], dict[str, Any]] = {}
    for r in correct_rows:
        key = (str(r.get("student_id")), str(r.get("assessment_id")))
        correct_by[key] = r

    # Total de itens da base por caderno (para mostrar total mesmo sem resposta).
    base_rows = await fetch_all(
        db,
        """
        SELECT assessment_id, COUNT(*)::int AS n
        FROM questions_assessments
        WHERE assessment_id = ANY(CAST(:aids AS uuid[]))
        GROUP BY assessment_id
        """,
        {"aids": assessment_ids},
    )
    base_by = {str(r.get("assessment_id")): int(r.get("n") or 0) for r in base_rows}

    items: list[dict[str, Any]] = []
    for r in rows:
        sid = str(r.get("student_id"))
        aid = str(r.get("assessment_id"))
        agg = correct_by.get((sid, aid)) or {}
        correct = int(agg.get("ca") or 0)
        answered = int(agg.get("tq") or 0)
        total = base_by.get(aid) or answered
        accuracy = round((100.0 * correct / total), 1) if total else None
        items.append(
            {
                "studentId": sid,
                "studentName": r.get("full_name") or "",
                "assessmentId": aid,
                "assessmentTitle": r.get("assessment_title") or "",
                "status": r.get("status"),
                "score": float(r["score"]) if r.get("score") is not None else None,
                "correctAnswers": correct,
                "totalQuestions": total,
                "accuracyPercentage": accuracy,
                "submittedAt": str(r["submitted_at"]) if r.get("submitted_at") else None,
            }
        )

    return {"items": items}
