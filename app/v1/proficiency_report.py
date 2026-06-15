"""Consultas de proficiência TRI (Prova São Paulo) para relatórios."""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.v1._sql import fetch_all

logger = logging.getLogger(__name__)


def proficiency_table_missing(exc: Exception) -> bool:
    msg = str(exc).lower()
    return "student_assessment_area_proficiency" in msg and "does not exist" in msg


def _proficiency_item(
    *,
    area_slug: Any,
    area_name: Any,
    proficiency: Any,
    level_code: Any = None,
    level_label: Any = None,
) -> dict[str, Any]:
    return {
        "areaSlug": area_slug,
        "areaName": area_name or "",
        "proficiency": float(proficiency) if proficiency is not None else None,
        "levelCode": level_code,
        "levelLabel": level_label,
    }


async def proficiencies_by_student_assessment(
    db: AsyncSession,
    *,
    assessment_ids: list[str],
) -> dict[tuple[str, str], list[dict[str, Any]]]:
    """Proficiências por (student_id, assessment_id), agrupadas por área."""
    if not assessment_ids:
        return {}
    try:
        rows = await fetch_all(
            db,
            """
            SELECT sap.student_id,
                   sap.assessment_id,
                   sap.area_slug,
                   COALESCE(ca.name, sap.area_slug) AS area_name,
                   sap.proficiency,
                   sap.level_code,
                   pl.label AS level_label
            FROM student_assessment_area_proficiency sap
            LEFT JOIN curricular_areas ca ON ca.slug = sap.area_slug
            LEFT JOIN proficiency_levels pl ON pl.code = sap.level_code
            WHERE sap.assessment_id = ANY(CAST(:aids AS uuid[]))
            ORDER BY sap.area_slug
            """,
            {"aids": assessment_ids},
        )
    except Exception as exc:
        if proficiency_table_missing(exc):
            logger.debug("student_assessment_area_proficiency ausente; proficiências omitidas")
            return {}
        raise

    out: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for r in rows:
        key = (str(r.get("student_id")), str(r.get("assessment_id")))
        out.setdefault(key, []).append(
            _proficiency_item(
                area_slug=r.get("area_slug"),
                area_name=r.get("area_name"),
                proficiency=r.get("proficiency"),
                level_code=r.get("level_code"),
                level_label=r.get("level_label"),
            )
        )
    return out


async def proficiencies_for_student(
    db: AsyncSession,
    *,
    student_id: str,
    assessment_id: str,
) -> list[dict[str, Any]]:
    """Proficiências do aluno em um caderno (lista por área)."""
    by_key = await proficiencies_by_student_assessment(
        db, assessment_ids=[assessment_id]
    )
    return by_key.get((student_id, assessment_id), [])


async def average_proficiency_by_area(
    db: AsyncSession,
    *,
    assessment_ids: list[str],
    classroom_ids: list[str] | None = None,
) -> dict[str, dict[str, Any]]:
    """Média de proficiência por área no escopo (cadernos × turmas opcionais)."""
    if not assessment_ids:
        return {}
    filter_classrooms = bool(classroom_ids)
    try:
        rows = await fetch_all(
            db,
            """
            SELECT sap.area_slug,
                   COALESCE(ca.name, sap.area_slug) AS area_name,
                   AVG(sap.proficiency) AS avg_proficiency,
                   COUNT(DISTINCT sap.student_id)::int AS student_count
            FROM student_assessment_area_proficiency sap
            LEFT JOIN curricular_areas ca ON ca.slug = sap.area_slug
            WHERE sap.assessment_id = ANY(CAST(:aids AS uuid[]))
              AND (
                CAST(:filter_classrooms AS boolean) = false
                OR sap.classroom_id = ANY(CAST(:cids AS uuid[]))
              )
            GROUP BY sap.area_slug, ca.name
            ORDER BY sap.area_slug
            """,
            {
                "aids": assessment_ids,
                "filter_classrooms": filter_classrooms,
                "cids": classroom_ids or [],
            },
        )
    except Exception as exc:
        if proficiency_table_missing(exc):
            return {}
        raise

    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        slug = str(r.get("area_slug") or "")
        if not slug:
            continue
        avg = r.get("avg_proficiency")
        out[slug] = {
            **_proficiency_item(
                area_slug=slug,
                area_name=r.get("area_name"),
                proficiency=float(avg) if avg is not None else None,
            ),
            "studentCount": int(r.get("student_count") or 0),
        }
    return out
