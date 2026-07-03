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
from app.v1.proficiency_report import (
    average_proficiency_by_area,
    proficiencies_by_student_assessment,
)
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


async def resolve_macro_scope_school(
    db: AsyncSession,
    *,
    route_id: UUID,
    school_ids: list[UUID],
) -> dict[str, Any]:
    """Resolve o contexto de macro avaliação no escopo de uma ESCOLA (subárvore).

    Diferente de :func:`resolve_macro_scope` (que recorta por uma turma), aqui os
    cadernos e turmas consideradas são todos os agendados em QUALQUER turma das
    escolas em ``school_ids``. Retorna também a lista de turmas para o seletor.
    """
    macro_row = await fetch_one(
        db,
        "SELECT id, title FROM macro_assessments WHERE id = CAST(:id AS uuid)",
        {"id": str(route_id)},
    )

    macro_id: str | None = None
    macro_title = ""
    is_macro = False

    if macro_row:
        macro_id = str(macro_row.get("id"))
        macro_title = macro_row.get("title") or ""
        is_macro = True
    else:
        assess_row = await fetch_one(
            db,
            "SELECT id, title, macro_assessment_id FROM assessments WHERE id = CAST(:id AS uuid)",
            {"id": str(route_id)},
        )
        if not assess_row:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND, "Avaliação/macro avaliação não encontrada"
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
            macro_id = None
            macro_title = assess_row.get("title") or ""

    sids = [str(s) for s in school_ids]

    # Cadernos da macro (ou o caderno avulso) agendados em alguma turma da escola.
    if is_macro and macro_id is not None:
        assessments = await fetch_all(
            db,
            """
            SELECT DISTINCT a.id, a.title, a.description, a.type, a.created_at
            FROM assessments a
            JOIN assessment_schedules s ON s.assessment_id = a.id
            JOIN classrooms c ON c.id = s.classroom_id
            WHERE a.macro_assessment_id = CAST(:mid AS uuid)
              AND c.school_id = ANY(CAST(:sids AS uuid[]))
            ORDER BY a.created_at, a.title
            """,
            {"mid": macro_id, "sids": sids},
        )
    else:
        assessments = await fetch_all(
            db,
            """
            SELECT DISTINCT a.id, a.title, a.description, a.type, a.created_at
            FROM assessments a
            JOIN assessment_schedules s ON s.assessment_id = a.id
            JOIN classrooms c ON c.id = s.classroom_id
            WHERE a.id = CAST(:id AS uuid)
              AND c.school_id = ANY(CAST(:sids AS uuid[]))
            """,
            {"id": str(route_id), "sids": sids},
        )

    assessment_ids = [str(a["id"]) for a in assessments]
    if not assessment_ids:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "Nenhum caderno desta macro avaliação está agendado nesta escola",
        )

    classroom_rows = await fetch_all(
        db,
        """
        SELECT DISTINCT c.id, c.name
        FROM classrooms c
        JOIN assessment_schedules s ON s.classroom_id = c.id
        WHERE s.assessment_id = ANY(CAST(:aids AS uuid[]))
          AND c.school_id = ANY(CAST(:sids AS uuid[]))
        ORDER BY c.name
        """,
        {"aids": assessment_ids, "sids": sids},
    )
    classrooms = [{"id": str(r["id"]), "name": r.get("name") or ""} for r in classroom_rows]
    classroom_ids = [c["id"] for c in classrooms]

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
        "classrooms": classrooms,
        "classroom_ids": classroom_ids,
    }


async def _component_accuracy_averages(
    db: AsyncSession,
    *,
    assessment_ids: list[str],
    scope_classroom_ids: list[str],
    reference_classroom_ids: list[str] | None = None,
) -> tuple[dict[tuple[str, str, str], float], dict[tuple[str, str, str], float]]:
    """Média de acurácia por componente no escopo e na referência (média por aluno)."""
    ref_filter = bool(reference_classroom_ids)
    rows = await fetch_all(
        db,
        """
        WITH per_student AS (
            SELECT COALESCE(discipline_name, 'Sem componente') AS discipline_name,
                   discipline_slug,
                   area_slug,
                   classroom_id,
                   student_id,
                   CASE WHEN SUM(total_questions) > 0
                        THEN 100.0 * SUM(correct_answers) / SUM(total_questions)
                        ELSE 0 END AS acc
            FROM vw_assessment_component_results
            WHERE assessment_id = ANY(CAST(:aids AS uuid[]))
            GROUP BY discipline_name, discipline_slug, area_slug,
                     classroom_id, student_id
        )
        SELECT discipline_name, discipline_slug, area_slug,
               AVG(acc) FILTER (
                 WHERE classroom_id = ANY(CAST(:scope_cids AS uuid[]))
               ) AS scope_acc,
               AVG(acc) FILTER (
                 WHERE CAST(:use_ref AS boolean) = false
                    OR classroom_id = ANY(CAST(:ref_cids AS uuid[]))
               ) AS ref_acc
        FROM per_student
        GROUP BY discipline_name, discipline_slug, area_slug
        """,
        {
            "aids": assessment_ids,
            "scope_cids": scope_classroom_ids,
            "use_ref": ref_filter,
            "ref_cids": reference_classroom_ids or scope_classroom_ids,
        },
    )

    def _key(dn: Any, ds: Any, ar: Any) -> tuple[str, str, str]:
        return (str(ds or ""), str(dn or ""), str(ar or ""))

    scope_by: dict[tuple[str, str, str], float] = {}
    ref_by: dict[tuple[str, str, str], float] = {}
    for r in rows:
        key = _key(r.get("discipline_name"), r.get("discipline_slug"), r.get("area_slug"))
        if r.get("scope_acc") is not None:
            scope_by[key] = float(r["scope_acc"])
        if r.get("ref_acc") is not None:
            ref_by[key] = float(r["ref_acc"])
    return scope_by, ref_by


def _area_proficiencies_from_averages(
    prof_avg_by_area: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for slug in sorted(prof_avg_by_area.keys()):
        prof = prof_avg_by_area[slug]
        if prof.get("proficiency") is None:
            continue
        items.append(
            {
                "areaSlug": slug,
                "areaName": prof.get("areaName") or slug,
                "proficiency": round(float(prof["proficiency"]), 1),
            }
        )
    return items


async def _component_performance(
    db: AsyncSession,
    *,
    assessment_ids: list[str],
    classroom_ids: list[str],
    reference_classroom_ids: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Lista de componentes a partir da BASE de questões dos cadernos.

    A lista de componentes (e o número de questões por componente) deriva das
    questões vinculadas aos cadernos (`questions_assessments` → `question_item`),
    de modo que os componentes aparecem mesmo sem respostas dos alunos. Acertos e
    acurácia vêm das respostas (`vw_assessment_component_results`) quando existem;
    caso contrário ficam zerados.
    """
    base_rows = await fetch_all(
        db,
        """
        SELECT COALESCE(qi.discipline_name, 'Sem componente') AS discipline_name,
               qi.discipline_slug, qi.area_slug,
               COUNT(*)::int AS base_questions
        FROM questions_assessments qa
        JOIN question_item qi ON qi.id = qa.question_id
        WHERE qa.assessment_id = ANY(CAST(:aids AS uuid[]))
        GROUP BY qi.discipline_name, qi.discipline_slug, qi.area_slug
        ORDER BY discipline_name
        """,
        {"aids": assessment_ids},
    )

    resp_rows = await fetch_all(
        db,
        """
        SELECT COALESCE(discipline_name, 'Sem componente') AS discipline_name,
               discipline_slug, area_slug,
               SUM(total_questions) AS tq,
               SUM(correct_answers) AS ca
        FROM vw_assessment_component_results
        WHERE assessment_id = ANY(CAST(:aids AS uuid[]))
          AND classroom_id = ANY(CAST(:cids AS uuid[]))
        GROUP BY discipline_name, discipline_slug, area_slug
        """,
        {"aids": assessment_ids, "cids": classroom_ids},
    )

    def _key(dn: Any, ds: Any, ar: Any) -> tuple[str, str, str]:
        return (str(ds or ""), str(dn or ""), str(ar or ""))

    resp_by: dict[tuple[str, str, str], dict[str, Any]] = {}
    for r in resp_rows:
        resp_by[_key(r.get("discipline_name"), r.get("discipline_slug"), r.get("area_slug"))] = r

    areas = await fetch_all(db, "SELECT slug, name FROM curricular_areas", {})
    area_name_by_slug = {str(a["slug"]): a.get("name") for a in areas}

    def _area_name(slug: Any, fallback: str) -> str:
        if slug and str(slug) in area_name_by_slug:
            return str(area_name_by_slug[str(slug)])
        return fallback

    # Quantidade de questões do componente em UM caderno. Como a base soma todos
    # os cadernos e a distribuição é igual entre eles, divide-se pelo nº de cadernos.
    num_cadernos = max(1, len(assessment_ids))

    prof_avg_by_area = await average_proficiency_by_area(
        db, assessment_ids=assessment_ids, classroom_ids=classroom_ids
    )

    scope_acc_by, ref_acc_by = await _component_accuracy_averages(
        db,
        assessment_ids=assessment_ids,
        scope_classroom_ids=classroom_ids,
        reference_classroom_ids=reference_classroom_ids,
    )

    out: list[dict[str, Any]] = []
    for b in base_rows:
        name = str(b.get("discipline_name") or "Sem componente")
        area_slug = str(b.get("area_slug") or "")
        base_q = int(b.get("base_questions") or 0)
        per_caderno = round(base_q / num_cadernos)
        comp_key = _key(b.get("discipline_name"), b.get("discipline_slug"), b.get("area_slug"))
        rr = resp_by.get(comp_key) or {}
        tq_resp = int(rr.get("tq") or 0)
        ca_resp = int(rr.get("ca") or 0)
        aggregate_accuracy = round(100.0 * ca_resp / tq_resp, 1) if tq_resp > 0 else 0.0
        student_accuracy = round(scope_acc_by.get(comp_key, aggregate_accuracy), 1)
        comparison_avg = round(ref_acc_by.get(comp_key, student_accuracy), 1)
        variation = round(student_accuracy - comparison_avg, 1)
        prof = prof_avg_by_area.get(area_slug) if area_slug else None
        item: dict[str, Any] = {
            "componentId": str(b.get("discipline_slug") or name),
            "componentName": name,
            "areaName": _area_name(b.get("area_slug"), name),
            "areaSlug": area_slug or None,
            "totalQuestions": per_caderno,
            "correctAnswers": ca_resp,
            "studentAccuracy": student_accuracy,
            "comparisonAverage": comparison_avg,
            "variationPercentagePoints": variation,
            "pedagogicalAction": _pedagogical_action(variation),
        }
        if prof and prof.get("proficiency") is not None:
            item["proficiency"] = round(float(prof["proficiency"]), 1)
        out.append(item)
    return out


async def _students_for_classrooms(
    db: AsyncSession,
    *,
    assessment_ids: list[str],
    classroom_ids: list[str],
) -> list[dict[str, Any]]:
    """Lista de desempenho por (aluno × caderno × turma) a partir da PRESENÇA.

    A listagem parte de ``assessment_attendance_list`` (vínculo real do aluno ao
    caderno/agendamento), de modo que cada aluno aparece apenas nos cadernos a que
    está vinculado — sem duplicar quando a turma tem vários cadernos — e mesmo sem
    respostas (status ``pending``, acertos zero). Acertos/itens respondidos vêm de
    ``vw_assessment_component_results``; o total de itens é a base do caderno
    (``questions_assessments``).
    """
    rows = await fetch_all(
        db,
        """
        SELECT al.student_id,
               COALESCE(p.full_name, '') AS full_name,
               sch.classroom_id,
               cl.name AS classroom_name,
               sch.assessment_id,
               a.title AS assessment_title,
               ar.status,
               ar.score,
               ar.submitted_at
        FROM assessment_attendance_list al
        JOIN assessment_schedules sch ON sch.id = al.assessment_schedules_id
        JOIN classrooms cl ON cl.id = sch.classroom_id
        JOIN assessments a ON a.id = sch.assessment_id
        LEFT JOIN assessment_results ar
               ON ar.student_id = al.student_id
              AND ar.assessment_id = sch.assessment_id
              AND ar.classroom_id = sch.classroom_id
        LEFT JOIN vw_profiles p ON p.id = al.student_id
        WHERE sch.assessment_id = ANY(CAST(:aids AS uuid[]))
          AND sch.classroom_id = ANY(CAST(:cids AS uuid[]))
        """,
        {"aids": assessment_ids, "cids": classroom_ids},
    )

    correct_rows = await fetch_all(
        db,
        """
        SELECT student_id, assessment_id, classroom_id,
               SUM(total_questions) AS tq, SUM(correct_answers) AS ca
        FROM vw_assessment_component_results
        WHERE assessment_id = ANY(CAST(:aids AS uuid[]))
          AND classroom_id = ANY(CAST(:cids AS uuid[]))
        GROUP BY student_id, assessment_id, classroom_id
        """,
        {"aids": assessment_ids, "cids": classroom_ids},
    )
    correct_by: dict[tuple[str, str, str], dict[str, Any]] = {}
    for r in correct_rows:
        key = (str(r.get("student_id")), str(r.get("assessment_id")), str(r.get("classroom_id")))
        correct_by[key] = r

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

    prof_by = await proficiencies_by_student_assessment(
        db, assessment_ids=assessment_ids
    )

    items: list[dict[str, Any]] = []
    for r in rows:
        sid = str(r.get("student_id"))
        aid = str(r.get("assessment_id"))
        cid = str(r.get("classroom_id"))
        agg = correct_by.get((sid, aid, cid)) or {}
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
                "classroomId": cid,
                "classroomName": r.get("classroom_name") or "",
                "status": r.get("status") or "pending",
                "score": float(r["score"]) if r.get("score") is not None else None,
                "correctAnswers": correct,
                "totalQuestions": total,
                "accuracyPercentage": accuracy,
                "submittedAt": str(r["submitted_at"]) if r.get("submitted_at") else None,
                "areaProficiencies": prof_by.get((sid, aid), []),
            }
        )

    items.sort(
        key=lambda x: (
            (x["classroomName"] or "").lower(),
            (x["studentName"] or "").lower(),
            (x["assessmentTitle"] or "").lower(),
        )
    )
    return items


async def macro_components_report_for_classrooms(
    db: AsyncSession,
    *,
    assessment_ids: list[str],
    classroom_ids: list[str],
) -> dict[str, Any]:
    """Componentes consolidados da macro avaliação para um conjunto de turmas.

    Versão multi-turma (escola): a lista de componentes vem da base de questões
    dos cadernos; médias usam acurácia por aluno. ``systemAverage`` considera
    todas as turmas que responderam aos cadernos (base do sistema).
    """
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
            SELECT student_id, classroom_id,
                   CASE WHEN tq > 0 THEN 100.0 * ca / tq ELSE 0 END AS accuracy
            FROM per_student
        )
        SELECT
            AVG(accuracy) FILTER (WHERE classroom_id = ANY(CAST(:cids AS uuid[]))) AS school_avg,
            AVG(accuracy) AS system_avg
        FROM acc
        """,
        {"aids": assessment_ids, "cids": classroom_ids},
    )
    school_avg = float((avg_row or {}).get("school_avg") or 0.0)
    system_avg = float((avg_row or {}).get("system_avg") or 0.0)

    component_performance = await _component_performance(
        db,
        assessment_ids=assessment_ids,
        classroom_ids=classroom_ids,
        reference_classroom_ids=None,
    )

    prof_avg_by_area = await average_proficiency_by_area(
        db, assessment_ids=assessment_ids, classroom_ids=classroom_ids
    )
    area_proficiencies = _area_proficiencies_from_averages(prof_avg_by_area)

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
        "accuracyPercentage": round(school_avg, 1),
        "classroomAverage": round(school_avg, 1),
        "schoolAverage": round(school_avg, 1),
        "systemAverage": round(system_avg, 1),
    }

    return {
        "summary": summary,
        "componentPerformance": component_performance,
        "pedagogicalReading": _build_pedagogical_reading(component_performance),
        "areaProficiencies": area_proficiencies,
    }


async def macro_students_report_for_classrooms(
    db: AsyncSession,
    *,
    assessment_ids: list[str],
    classroom_ids: list[str],
) -> dict[str, Any]:
    """Desempenho por aluno consolidando a macro para um conjunto de turmas.

    Uma linha por (aluno, caderno) em qualquer turma de ``classroom_ids`` — inclui
    todos os alunos matriculados, mesmo pendentes; ``classroomName`` identifica a
    turma de cada linha.
    """
    items = await _students_for_classrooms(
        db, assessment_ids=assessment_ids, classroom_ids=classroom_ids
    )
    return {"items": items}


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

    school_classroom_rows = await fetch_all(
        db,
        """
        SELECT DISTINCT c.id
        FROM classrooms c
        JOIN assessment_schedules s ON s.classroom_id = c.id
        WHERE s.assessment_id = ANY(CAST(:aids AS uuid[]))
          AND c.school_id = CAST(:sid AS uuid)
        """,
        {"aids": assessment_ids, "sid": str(school_id)},
    )
    reference_classroom_ids = [str(r["id"]) for r in school_classroom_rows]

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

    # Lista de componentes a partir da base de questões dos cadernos (aparecem
    # mesmo sem respostas); acertos vêm das respostas da turma.
    component_performance = await _component_performance(
        db,
        assessment_ids=assessment_ids,
        classroom_ids=[str(classroom_id)],
        reference_classroom_ids=reference_classroom_ids or None,
    )

    prof_avg_by_area = await average_proficiency_by_area(
        db,
        assessment_ids=assessment_ids,
        classroom_ids=[str(classroom_id)],
    )
    area_proficiencies = _area_proficiencies_from_averages(prof_avg_by_area)

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
        "areaProficiencies": area_proficiencies,
    }


async def macro_students_report(
    db: AsyncSession,
    *,
    assessment_ids: list[str],
    classroom_id: UUID,
) -> dict[str, Any]:
    """Desempenho por aluno consolidando a macro avaliação.

    Uma linha por (aluno, caderno), com `assessmentTitle`, status, acertos, total
    de itens, percentual e data de envio. Inclui todos os alunos matriculados na
    turma, mesmo pendentes (status ``pending``, acertos zero).
    """
    items = await _students_for_classrooms(
        db, assessment_ids=assessment_ids, classroom_ids=[str(classroom_id)]
    )
    return {"items": items}
