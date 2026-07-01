"""Pacote de dados estruturados para LLM e respostas factuais do Avaliador."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.v1._sql import fetch_all, fetch_one
from app.v1.report_bundle import compact_pedagogical_snapshot_for_llm


def _iso_dt(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _format_date_br(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.strftime("%d/%m/%Y")
    text = str(value)
    if len(text) >= 10 and text[4:5] == "-":
        y, m, d = text[:10].split("-")
        return f"{d}/{m}/{y}"
    return text


async def _load_pedagogical_reports(
    db: AsyncSession,
    *,
    pid: str,
    academic_year_id: UUID,
    classroom_id: UUID,
    limit: int = 2,
) -> list[dict[str, Any]]:
    """Relatórios pedagógicos compactos das avaliações mais recentes com respostas."""
    rows = await fetch_all(
        db,
        """
        SELECT
          sch.id AS schedule_id,
          sch.assessment_id,
          sch.start_time,
          a.title AS assessment_title
        FROM assessment_schedules sch
        JOIN assessments a ON a.id = sch.assessment_id
        JOIN classrooms c ON c.id = sch.classroom_id
        JOIN my_classrooms mc ON mc.classroom_id = sch.classroom_id
        WHERE mc.teacher_id = CAST(:pid AS uuid)
          AND sch.classroom_id = CAST(:cid AS uuid)
          AND c.academic_year_id = CAST(:ay AS uuid)
          AND EXISTS (
            SELECT 1
            FROM question_student_responsed qsr
            WHERE qsr.schedule_id = sch.id
              AND qsr.response_id IS NOT NULL
            LIMIT 1
          )
        ORDER BY sch.start_time DESC NULLS LAST
        LIMIT :limit
        """,
        {
            "pid": pid,
            "cid": str(classroom_id),
            "ay": str(academic_year_id),
            "limit": limit,
        },
    )

    reports: list[dict[str, Any]] = []
    for row in rows:
        snapshot = await compact_pedagogical_snapshot_for_llm(
            db,
            assessment_id=row["assessment_id"],
            classroom_id=classroom_id,
            schedule_id=row.get("schedule_id"),
            schedule_start=row.get("start_time"),
        )
        if snapshot:
            reports.append(snapshot)
    return reports


async def load_teacher_data_pack(
    db: AsyncSession,
    *,
    pid: str,
    academic_year_id: UUID,
    classroom_id: UUID | None,
    schedule_limit: int = 25,
    pedagogical_limit: int = 2,
) -> dict[str, Any]:
    """Snapshot compacto: agendamentos, relatórios pedagógicos e métricas — base para NL + LLM."""
    params: dict[str, Any] = {
        "pid": pid,
        "ay": str(academic_year_id),
        "limit": schedule_limit,
    }
    classroom_filter = ""
    if classroom_id:
        classroom_filter = " AND sch.classroom_id = CAST(:cid AS uuid)"
        params["cid"] = str(classroom_id)

    rows = await fetch_all(
        db,
        f"""
        SELECT
          sch.id AS schedule_id,
          a.title AS assessment_title,
          c.name AS classroom_name,
          sch.start_time,
          sch.end_time,
          COALESCE(
            (
              SELECT COUNT(*) FILTER (WHERE ar.status IN ('submitted', 'completed'))::int
              FROM assessment_results ar
              WHERE ar.schedule_id = sch.id
            ),
            0
          ) AS completed_count,
          COALESCE(
            (
              SELECT COUNT(*)::int FROM classroom_students cs
              WHERE cs.classroom_id = sch.classroom_id
            ),
            0
          ) AS student_count
        FROM assessment_schedules sch
        JOIN assessments a ON a.id = sch.assessment_id
        JOIN classrooms c ON c.id = sch.classroom_id
        JOIN my_classrooms mc ON mc.classroom_id = sch.classroom_id
        WHERE mc.teacher_id = CAST(:pid AS uuid)
          AND c.academic_year_id = CAST(:ay AS uuid)
          {classroom_filter}
        ORDER BY sch.start_time ASC NULLS LAST
        LIMIT :limit
        """,
        params,
    )

    schedules: list[dict[str, Any]] = []
    for row in rows:
        completed = int(row.get("completed_count") or 0)
        students = int(row.get("student_count") or 0)
        if completed <= 0:
            status = "pendente"
        elif completed >= students and students > 0:
            status = "concluída"
        else:
            status = "em andamento"
        schedules.append(
            {
                "schedule_id": str(row["schedule_id"]),
                "assessment_title": row.get("assessment_title"),
                "classroom": row.get("classroom_name"),
                "start_date": _format_date_br(row.get("start_time")),
                "start_time": _iso_dt(row.get("start_time")),
                "end_time": _iso_dt(row.get("end_time")),
                "status": status,
                "completed_students": completed,
                "total_students": students,
            }
        )

    assessment_rows = await fetch_all(
        db,
        f"""
        SELECT
          vas.assessment_id,
          vas.title,
          vas.classroom_id,
          c.name AS classroom_name,
          vas.pending,
          vas.completed,
          vas.did_not_deliver
        FROM vw_assessment_summary vas
        JOIN classrooms c ON c.id = vas.classroom_id
        JOIN my_classrooms mc ON mc.classroom_id = vas.classroom_id
        WHERE mc.teacher_id = CAST(:pid AS uuid)
          AND c.academic_year_id = CAST(:ay AS uuid)
          {classroom_filter.replace("sch.", "vas.") if classroom_filter else ""}
        ORDER BY vas.completed DESC, vas.title ASC
        LIMIT 15
        """,
        params,
    )
    assessments: list[dict[str, Any]] = []
    for row in assessment_rows:
        pending = int(row.get("pending") or 0)
        completed = int(row.get("completed") or 0)
        did_not = int(row.get("did_not_deliver") or 0)
        assessments.append(
            {
                "assessment_id": str(row["assessment_id"]),
                "title": row.get("title"),
                "classroom": row.get("classroom_name"),
                "pending": pending,
                "completed": completed,
                "did_not_deliver": did_not,
                "total_students": pending + completed + did_not,
            }
        )

    now_row = await fetch_one(db, "SELECT now() AS ts", {}) or {}
    now_ts = now_row.get("ts")

    first_applied = schedules[0] if schedules else None
    last_applied = schedules[-1] if schedules else None
    next_upcoming = None
    if now_ts and schedules:
        for item in schedules:
            start = item.get("start_time")
            if start and str(start) >= str(now_ts):
                next_upcoming = item
                break

    pedagogical_reports: list[dict[str, Any]] = []
    if classroom_id:
        pedagogical_reports = await _load_pedagogical_reports(
            db,
            pid=pid,
            academic_year_id=academic_year_id,
            classroom_id=classroom_id,
            limit=pedagogical_limit,
        )

    return {
        "schedules": schedules,
        "schedule_count": len(schedules),
        "schedule_facts": {
            "first_applied": first_applied,
            "last_applied": last_applied,
            "next_upcoming": next_upcoming,
        },
        "assessments": assessments,
        "assessment_count": len(assessments),
        "pedagogical_reports": pedagogical_reports,
        "pedagogical_report_count": len(pedagogical_reports),
    }
