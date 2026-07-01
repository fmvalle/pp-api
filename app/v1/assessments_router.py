"""Avaliações, agendamentos e presença (Etapa 5)."""

import logging
import uuid
from datetime import datetime
from typing import Annotated, Any
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import AuthContext, get_auth_context
from app.db.session import get_db
from app.v1._academic_year import resolve_academic_year_id
from app.v1._paging import PageArgs, paged_response, pagination_params
from app.v1._scope import (
    _norm_role,
    get_descendant_school_ids,
    get_effective_classroom_scope,
    get_effective_school_scope,
    is_admin_like,
    is_staff_admin_role,
)
from app.v1._sql import execute, fetch_all, fetch_one
from app.v1.cartao_resposta_pdf import (
    build_cartao_resposta_pdf_bytes_from_view_row,
    merge_pdf_bytes,
    suggested_download_filename,
)
from app.v1.attendance_sheet_pdf import build_attendance_sheet_pdf_bytes
from app.v1.attendance_sheet_storage import load_attendance_sheet_bytes, store_attendance_sheet_bytes
from app.core.config import settings

router = APIRouter(tags=["v1-assessments"])
log = logging.getLogger(__name__)

_ALLOWED_ATTENDANCE_SHEET_MEDIA = frozenset(
    {
        "application/pdf",
        "image/jpeg",
        "image/png",
        "image/webp",
    }
)


def _looks_like_attendance_sheet_table_missing(exc: Exception) -> bool:
    if not isinstance(exc, ProgrammingError):
        return False
    msg = str(exc).lower()
    return "assessment_schedule_attendance_sheet" in msg and "does not exist" in msg


def _guess_sheet_content_type(raw_ct: str, filename: str | None, data: bytes) -> str:
    if raw_ct in _ALLOWED_ATTENDANCE_SHEET_MEDIA:
        return raw_ct
    lower_name = (filename or "").lower()
    if lower_name.endswith(".pdf") or data.startswith(b"%PDF"):
        return "application/pdf"
    if lower_name.endswith(".png") or data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if lower_name.endswith(".jpg") or lower_name.endswith(".jpeg") or data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if lower_name.endswith(".webp") or data[:12].startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    return ""


def _attendance_sheet_attachment_filename(original: str | None) -> str:
    safe = (original or "lista-presenca").replace('"', "").replace("\r", "").replace("\n", "")[:180]
    return safe if safe else "lista-presenca"


def _shape_assessment_schedule_list_item(row: dict[str, Any]) -> dict[str, Any]:
    """Contrato do app admin: anexa `classrooms` com `grades` e `academic_years` (paridade com expand PostgREST)."""
    r = dict(row)
    cname = r.pop("_classroom_name", None)
    ccode = r.pop("_classroom_code", None)
    gname = r.pop("_grade_name", None)
    ayear = r.pop("_academic_year_year", None)
    atitle = r.pop("_assessment_title", None)
    cid = r.get("classroom_id")
    aid = r.get("assessment_id")
    r["classrooms"] = {
        "id": str(cid) if cid is not None else None,
        "name": cname,
        "code": ccode,
        "grades": ({"name": gname} if gname is not None else None),
        "academic_years": ({"year": ayear} if ayear is not None else None),
    }
    if atitle is not None or aid is not None:
        r["assessments"] = {
            "id": str(aid) if aid is not None else None,
            "title": atitle,
        }
    return r


async def _schedule_teacher_can_manage(db: AsyncSession, ctx: AuthContext, classroom_id: UUID) -> bool:
    if is_staff_admin_role(ctx.role):
        return True
    row = await fetch_one(
        db,
        """
        SELECT 1 FROM classroom_teachers
        WHERE classroom_id = CAST(:cid AS uuid) AND teacher_id = CAST(:tid AS uuid)
        LIMIT 1
        """,
        {"cid": str(classroom_id), "tid": str(ctx.active_profile_id)},
    )
    return row is not None


async def _assert_schedule_attendance_editor(db: AsyncSession, ctx: AuthContext, schedule_row: dict[str, Any]) -> None:
    if await _schedule_teacher_can_manage(db, ctx, schedule_row["classroom_id"]):
        return
    raise HTTPException(status.HTTP_403_FORBIDDEN, "Apenas administradores ou professor vinculado à turma")


class AssessmentCreate(BaseModel):
    school_id: UUID
    title: str
    description: str | None = None
    assessment_type: str | None = Field(default=None, alias="type")
    created_by: UUID

    model_config = {"populate_by_name": True}


class AssessmentPatch(BaseModel):
    title: str | None = None
    description: str | None = None
    assessment_type: str | None = Field(default=None, alias="type")
    macro_assessment_id: UUID | None = None

    model_config = {"populate_by_name": True}


@router.get("/assessments")
async def list_assessments_v1(
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
    school_id: UUID | None = Query(None, description="Admin: subárvore (nó + descendentes). Não-admin: escola exata no escopo."),
    academic_year_id: UUID | None = Query(None, description="Ano letivo. Se omitido, usa is_primary=true."),
    pg: Annotated[PageArgs, Depends(pagination_params)] = PageArgs(1, 50),
):
    effective_ay = await resolve_academic_year_id(db, academic_year_id)
    sscope = await get_effective_school_scope(db, ctx)
    sql = "SELECT * FROM assessments WHERE 1=1"
    params: dict[str, Any] = {}
    sql += """
    AND EXISTS (
      SELECT 1
      FROM assessment_schedules ass
      JOIN classrooms c ON c.id = ass.classroom_id
      WHERE ass.assessment_id = assessments.id
        AND c.academic_year_id = CAST(:ay AS uuid)
    )
    """
    params["ay"] = str(effective_ay)
    if school_id:
        if not sscope["is_admin_like"]:
            if str(school_id) not in {str(x) for x in (sscope["effective_school_ids"] or [])}:
                raise HTTPException(status.HTTP_403_FORBIDDEN, "Escola fora do escopo")
            sql += " AND school_id = CAST(:sid AS uuid)"
            params["sid"] = str(school_id)
        else:
            subtree = await get_descendant_school_ids(db, school_id)
            sql += " AND school_id = ANY(CAST(:school_subtree AS uuid[]))"
            params["school_subtree"] = [str(x) for x in subtree]
    elif not sscope["is_admin_like"]:
        sql += " AND school_id = ANY(CAST(:sids AS uuid[]))"
        params["sids"] = [str(x) for x in (sscope["effective_school_ids"] or [])]
    count_row = await fetch_one(db, f"SELECT COUNT(*)::int AS total FROM ({sql}) q", params)
    items = await fetch_all(
        db,
        f"{sql} ORDER BY created_at DESC LIMIT {pg.per_page} OFFSET {pg.offset}",
        params,
    )
    return paged_response(page=pg.page, per_page=pg.per_page, total=(count_row or {}).get("total", 0), items=items)


@router.post("/assessments")
async def create_assessment_v1(
    body: AssessmentCreate,
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    sscope = await get_effective_school_scope(db, ctx)
    if not sscope["is_admin_like"] and str(body.school_id) not in {str(x) for x in (sscope["effective_school_ids"] or [])}:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Escola fora do escopo")
    row = await fetch_one(
        db,
        """
        INSERT INTO assessments (school_id, created_by, title, description, type)
        VALUES (CAST(:sch AS uuid), CAST(:cb AS uuid), :title, :desc, :typ)
        RETURNING *
        """,
        {
            "sch": str(body.school_id),
            "cb": str(body.created_by),
            "title": body.title,
            "desc": body.description,
            "typ": body.assessment_type,
        },
    )
    await db.commit()
    return row


@router.get("/assessments/{assessment_id}")
async def get_assessment_v1(
    assessment_id: UUID,
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    sscope = await get_effective_school_scope(db, ctx)
    row = await fetch_one(
        db,
        "SELECT * FROM assessments WHERE id = CAST(:id AS uuid)",
        {"id": str(assessment_id)},
    )
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Assessment not found")
    if not sscope["is_admin_like"] and str(row["school_id"]) not in {str(x) for x in (sscope["effective_school_ids"] or [])}:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Fora do escopo")
    return row


@router.patch("/assessments/{assessment_id}")
async def patch_assessment_v1(
    assessment_id: UUID,
    body: AssessmentPatch,
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    await get_assessment_v1(assessment_id, ctx, db)
    sets = []
    params: dict[str, Any] = {"id": str(assessment_id)}
    if body.title is not None:
        sets.append("title = :title")
        params["title"] = body.title
    if body.description is not None:
        sets.append("description = :d")
        params["d"] = body.description
    if body.assessment_type is not None:
        sets.append("type = :typ")
        params["typ"] = body.assessment_type
    if body.macro_assessment_id is not None:
        sets.append("macro_assessment_id = CAST(:mid AS uuid)")
        params["mid"] = str(body.macro_assessment_id)
    if not sets:
        return await get_assessment_v1(assessment_id, ctx, db)
    sql = f"UPDATE assessments SET {', '.join(sets)} WHERE id = CAST(:id AS uuid) RETURNING *"
    out = await fetch_one(db, sql, params)
    await db.commit()
    return out


@router.delete("/assessments/{assessment_id}")
async def delete_assessment_v1(
    assessment_id: UUID,
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    await get_assessment_v1(assessment_id, ctx, db)
    if not is_admin_like(ctx.role):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Apenas administradores")
    await execute(db, "DELETE FROM assessments WHERE id = CAST(:id AS uuid)", {"id": str(assessment_id)})
    await db.commit()
    return {"ok": True}


class AssessmentSchoolBody(BaseModel):
    school_id: UUID


@router.post("/assessments/{assessment_id}/schools")
async def post_assessment_school_v1(
    assessment_id: UUID,
    body: AssessmentSchoolBody,
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    await get_assessment_v1(assessment_id, ctx, db)
    sscope = await get_effective_school_scope(db, ctx)
    if not sscope["is_admin_like"]:
        if str(body.school_id) not in {str(x) for x in (sscope["effective_school_ids"] or [])}:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Escola fora do escopo")
    dup = await fetch_one(
        db,
        """
        SELECT * FROM assessment_school
        WHERE assessment_id = CAST(:a AS uuid) AND school_id = CAST(:s AS uuid)
        LIMIT 1
        """,
        {"a": str(assessment_id), "s": str(body.school_id)},
    )
    if dup:
        return dup
    row = await fetch_one(
        db,
        """
        INSERT INTO assessment_school (assessment_id, school_id)
        VALUES (CAST(:a AS uuid), CAST(:s AS uuid))
        RETURNING *
        """,
        {"a": str(assessment_id), "s": str(body.school_id)},
    )
    await db.commit()
    return row


@router.get("/assessments/{assessment_id}/schools")
async def list_assessment_schools_v1(
    assessment_id: UUID,
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
    pg: Annotated[PageArgs, Depends(pagination_params)] = PageArgs(1, 50),
):
    await get_assessment_v1(assessment_id, ctx, db)
    sql = "SELECT * FROM assessment_school WHERE assessment_id = CAST(:aid AS uuid)"
    params: dict[str, Any] = {"aid": str(assessment_id)}
    count_row = await fetch_one(db, f"SELECT COUNT(*)::int AS total FROM ({sql}) q", params)
    items = await fetch_all(
        db,
        f"{sql} ORDER BY created_at DESC NULLS LAST LIMIT {pg.per_page} OFFSET {pg.offset}",
        params,
    )
    return paged_response(page=pg.page, per_page=pg.per_page, total=(count_row or {}).get("total", 0), items=items)


@router.delete("/assessments/{assessment_id}/schools/{school_id}")
async def delete_assessment_school_v1(
    assessment_id: UUID,
    school_id: UUID,
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    await get_assessment_v1(assessment_id, ctx, db)
    sscope = await get_effective_school_scope(db, ctx)
    if not sscope["is_admin_like"]:
        if str(school_id) not in {str(x) for x in (sscope["effective_school_ids"] or [])}:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Escola fora do escopo")
    row = await fetch_one(
        db,
        """
        SELECT id FROM assessment_school
        WHERE assessment_id = CAST(:a AS uuid) AND school_id = CAST(:s AS uuid)
        LIMIT 1
        """,
        {"a": str(assessment_id), "s": str(school_id)},
    )
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Vínculo assessment_school não encontrado")
    await execute(
        db,
        "DELETE FROM assessment_school WHERE assessment_id = CAST(:a AS uuid) AND school_id = CAST(:s AS uuid)",
        {"a": str(assessment_id), "s": str(school_id)},
    )
    await db.commit()
    return {"ok": True}


async def _student_assessment_summary_rows(
    db: AsyncSession,
    ctx: AuthContext,
    student_id: UUID,
    effective_ay: UUID,
) -> list[dict[str, Any]]:
    """Linhas de `vw_student_assessment_sumarize` no ano letivo — mesmo escopo que o GET summary."""
    base_sql = """
        SELECT vs.*
        FROM vw_student_assessment_sumarize vs
        INNER JOIN classrooms c ON c.id = vs.classroom_id
        WHERE vs.student_id = CAST(:sid AS uuid)
          AND c.academic_year_id = CAST(:ay AS uuid)
    """
    params: dict[str, Any] = {"sid": str(student_id), "ay": str(effective_ay)}
    if (
        str(student_id) == str(ctx.active_profile_id)
        or is_admin_like(ctx.role)
        or _norm_role(ctx.role) == "platform_admin"
    ):
        return await fetch_all(db, base_sql, params)
    prow = await fetch_one(
        db,
        "SELECT school_id, role::text AS role FROM vw_profiles WHERE id = CAST(:id AS uuid)",
        {"id": str(student_id)},
    )
    if not prow:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Student not found")
    role_txt = (prow.get("role") or "").lower()
    if "student" not in role_txt:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Student not found")
    sscope = await get_effective_school_scope(db, ctx)
    pschool = prow.get("school_id")
    if pschool and str(pschool) in {str(x) for x in (sscope["effective_school_ids"] or [])}:
        return await fetch_all(db, base_sql, params)
    raise HTTPException(status.HTTP_403_FORBIDDEN, "Fora do escopo")


@router.get("/students/{student_id}/assessments/summary")
async def student_assessments_summary_v1(
    student_id: UUID,
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
    academic_year_id: UUID | None = Query(
        None,
        description="Ano letivo das turmas do resumo. Se omitido, usa is_primary=true.",
    ),
):
    """Aluno (self), admin-like, ou staff cuja escola efetiva contém a escola do perfil aluno."""
    effective_ay = await resolve_academic_year_id(db, academic_year_id)
    return await _student_assessment_summary_rows(db, ctx, student_id, effective_ay)


@router.get("/students/{student_id}/performance")
async def student_performance_v1(
    student_id: UUID,
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
    academic_year_id: UUID | None = Query(
        None,
        description="Ano letivo das turmas. Se omitido, usa is_primary=true.",
    ),
    include_items: bool = Query(False, description="Se true, inclui as linhas crus do resumo em `items`."),
):
    """Agregados de desempenho (admin / self / staff escola) — contrato estável para dashboards."""
    effective_ay = await resolve_academic_year_id(db, academic_year_id)
    rows = await _student_assessment_summary_rows(db, ctx, student_id, effective_ay)
    completed = 0
    scored: list[float] = []
    by_type: dict[str, dict[str, Any]] = {}
    for r in rows:
        st = (r.get("status") or "").lower()
        if st in ("submitted", "graded"):
            completed += 1
        sc = r.get("score")
        if isinstance(sc, (int, float)):
            scored.append(float(sc))
        tkey = (r.get("type") or "unknown").strip() or "unknown"
        bucket = by_type.setdefault(
            tkey,
            {"type": tkey, "count": 0, "completed": 0, "scores": []},
        )
        bucket["count"] += 1
        if st in ("submitted", "graded"):
            bucket["completed"] += 1
        if isinstance(sc, (int, float)):
            bucket["scores"].append(float(sc))
    by_type_out: list[dict[str, Any]] = []
    for b in by_type.values():
        scs = b.pop("scores", [])
        by_type_out.append(
            {
                **b,
                "avg_score": round(sum(scs) / len(scs), 2) if scs else 0.0,
            }
        )
    by_type_out.sort(key=lambda x: x["type"])
    out: dict[str, Any] = {
        "student_id": str(student_id),
        "academic_year_id": str(effective_ay),
        "total_assessments": len(rows),
        "completed_assessments": completed,
        "average_score": round(sum(scored) / len(scored), 2) if scored else 0.0,
        "by_type": by_type_out,
    }
    if include_items:
        out["items"] = rows
    return out


@router.get("/schools/{school_id}/assessments/active-schedules")
async def school_active_schedules_v1(
    school_id: UUID,
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
    academic_year_id: UUID | None = Query(
        None,
        description="Reservado para evolução (contagem por ano). Hoje alinhado a `vw_schedule_summary`.",
    ),
    pg: Annotated[PageArgs, Depends(pagination_params)] = PageArgs(1, 50),
):
    """Resumo por avaliação na escola (paridade com `vw_schedule_summary` / Supabase).

    Uma linha por vínculo `assessment_school` ativo: título, tipo, vigência, contagem de
    agendamentos com `school_id` coincidente. Não são linhas cruas de `assessment_schedules`.
    """
    _ = await resolve_academic_year_id(db, academic_year_id)  # valida ano quando enviado
    sscope = await get_effective_school_scope(db, ctx)
    if not sscope["is_admin_like"] and str(school_id) not in {str(x) for x in (sscope["effective_school_ids"] or [])}:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Fora do escopo")
    base_sql = """
    SELECT
      a.id AS assessment_id,
      a.id::text AS id,
      a.title,
      a.description,
      a.type,
      ash.school_id,
      ash.start_availability,
      ash.end_availability,
      ash.active,
      CASE
        WHEN ash.start_availability IS NOT NULL
             AND ash.end_availability IS NOT NULL
             AND ash.start_availability <= (now() AT TIME ZONE 'utc')
             AND ash.end_availability >= (now() AT TIME ZONE 'utc')
             AND ash.active = true
        THEN true
        ELSE false
      END AS visible,
      COALESCE(
        (
          SELECT COUNT(DISTINCT ass.id)::int
          FROM assessment_schedules ass
          WHERE ass.assessment_id = a.id
            AND ass.school_id = ash.school_id
        ),
        0
      ) AS schedules
    FROM assessments a
    INNER JOIN assessment_school ash
      ON ash.assessment_id = a.id AND ash.school_id = CAST(:sid AS uuid)
    WHERE ash.active = true
    """
    params: dict[str, Any] = {"sid": str(school_id)}
    count_row = await fetch_one(
        db,
        f"SELECT COUNT(*)::int AS total FROM ({base_sql}) q",
        params,
    )
    total = (count_row or {}).get("total", 0)
    items = await fetch_all(
        db,
        f"{base_sql} ORDER BY a.title ASC LIMIT {pg.per_page} OFFSET {pg.offset}",
        params,
    )
    return paged_response(page=pg.page, per_page=pg.per_page, total=total, items=items)


@router.get("/assessment-schedules")
async def list_schedules_v1(
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
    assessment_id: UUID | None = Query(None),
    classroom_id: UUID | None = Query(None),
    academic_year_id: UUID | None = Query(None, description="Ano letivo. Se omitido, usa is_primary=true."),
    pg: Annotated[PageArgs, Depends(pagination_params)] = PageArgs(1, 50),
):
    effective_ay = await resolve_academic_year_id(db, academic_year_id)
    cscope = await get_effective_classroom_scope(db, ctx)
    sql = """
SELECT
  ass.id,
  ass.assessment_id,
  ass.classroom_id,
  ass.scheduled_by,
  ass.start_time,
  ass.end_time,
  ass.created_at,
  ass.school_id,
  a.title AS _assessment_title,
  c."name" AS _classroom_name,
  c.code AS _classroom_code,
  g."name" AS _grade_name,
  ay."year" AS _academic_year_year
FROM assessment_schedules ass
JOIN assessments a ON a.id = ass.assessment_id
JOIN classrooms c ON c.id = ass.classroom_id
JOIN grades g ON g.id = c.grade_id
JOIN academic_years ay ON ay.id = c.academic_year_id
WHERE 1=1
""".strip()
    params: dict[str, Any] = {}
    sql += " AND c.academic_year_id = CAST(:ay AS uuid)"
    params["ay"] = str(effective_ay)
    if assessment_id:
        sql += " AND ass.assessment_id = CAST(:aid AS uuid)"
        params["aid"] = str(assessment_id)
    if classroom_id:
        sql += " AND ass.classroom_id = CAST(:cid AS uuid)"
        params["cid"] = str(classroom_id)
    if not cscope["is_admin_like"]:
        sql += " AND ass.classroom_id = ANY(CAST(:_scope_cids AS uuid[]))"
        params["_scope_cids"] = [str(x) for x in (cscope["effective_classroom_ids"] or [])]
    count_row = await fetch_one(db, f"SELECT COUNT(*)::int AS total FROM ({sql}) q", params)
    items_raw = await fetch_all(
        db,
        f"{sql} ORDER BY ass.created_at DESC LIMIT {pg.per_page} OFFSET {pg.offset}",
        params,
    )
    items = [_shape_assessment_schedule_list_item(r) for r in items_raw]
    return paged_response(page=pg.page, per_page=pg.per_page, total=(count_row or {}).get("total", 0), items=items)


def _coerce_timestamptz(value: Any, *, field: str) -> datetime:
    """asyncpg exige datetime para timestamptz; JSON envia ISO-8601 em string."""
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        s = value.strip().replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(s)
        except ValueError as e:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                f"{field} inválido: esperado ISO-8601 (ex.: …T…Z).",
            ) from e
    raise HTTPException(
        status.HTTP_422_UNPROCESSABLE_ENTITY,
        f"{field} deve ser string ISO-8601 ou datetime, recebido: {type(value).__name__}",
    )


class ScheduleCreate(BaseModel):
    assessment_id: UUID
    classroom_id: UUID
    scheduled_by: UUID
    start_time: datetime
    end_time: datetime
    school_id: UUID | None = None


async def _assessment_covers_classroom_school(
    db: AsyncSession,
    *,
    assessment_id: UUID,
    assessment_primary_school_id: Any,
    classroom_school_id: Any,
) -> bool:
    """True se a turma pertence à mesma escola «principal» da avaliação ou há vínculo em assessment_school."""
    if str(assessment_primary_school_id) == str(classroom_school_id):
        return True
    row = await fetch_one(
        db,
        """
        SELECT 1 AS ok FROM assessment_school
        WHERE assessment_id = CAST(:aid AS uuid)
          AND school_id = CAST(:sid AS uuid)
        LIMIT 1
        """,
        {"aid": str(assessment_id), "sid": str(classroom_school_id)},
    )
    return row is not None


def _is_assessment_schedule_duplicate(exc: IntegrityError) -> bool:
    raw = str(exc.orig) if getattr(exc, "orig", None) else str(exc)
    low = raw.lower()
    return "assessment_schedules_assessment_id_classroom_id_key" in low or (
        "duplicate key" in low and "assessment_schedules" in low
    )


@router.post("/assessment-schedules")
async def create_schedule_v1(
    body: ScheduleCreate,
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    if not is_staff_admin_role(ctx.role):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Apenas administradores nesta v1")
    assessment_row = await fetch_one(
        db,
        "SELECT id, school_id FROM assessments WHERE id = CAST(:id AS uuid)",
        {"id": str(body.assessment_id)},
    )
    if not assessment_row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Assessment not found")
    classroom_row = await fetch_one(
        db,
        "SELECT id, school_id FROM classrooms WHERE id = CAST(:id AS uuid)",
        {"id": str(body.classroom_id)},
    )
    if not classroom_row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Classroom not found")
    if _norm_role(ctx.role) == "school_admin":
        sscope = await get_effective_school_scope(db, ctx)
        if str(classroom_row["school_id"]) not in {str(x) for x in (sscope["effective_school_ids"] or [])}:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Turma fora do escopo")
    if not await _assessment_covers_classroom_school(
        db,
        assessment_id=body.assessment_id,
        assessment_primary_school_id=assessment_row["school_id"],
        classroom_school_id=classroom_row["school_id"],
    ):
        log.warning(
            "POST /v1/assessment-schedules 409: escola da avaliação não cobre a turma "
            "(assessment_id=%s assessment.school_id=%s classroom_id=%s classroom.school_id=%s)",
            body.assessment_id,
            assessment_row["school_id"],
            body.classroom_id,
            classroom_row["school_id"],
        )
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Assessment e classroom de escolas diferentes "
            f"(assessment.school_id={assessment_row['school_id']}, classroom.school_id={classroom_row['school_id']}). "
            "Confirme um vínculo em assessment_school para a escola da turma ou alinhe assessments.school_id.",
        )
    sch_sql = "CAST(:sch AS uuid)" if body.school_id else "NULL"
    try:
        row = await fetch_one(
            db,
            f"""
            INSERT INTO assessment_schedules (assessment_id, classroom_id, scheduled_by, start_time, end_time, school_id)
            VALUES (CAST(:a AS uuid), CAST(:c AS uuid), CAST(:sb AS uuid), CAST(:st AS timestamptz), CAST(:en AS timestamptz),
                    {sch_sql})
            RETURNING *
            """,
            {
                "a": str(body.assessment_id),
                "c": str(body.classroom_id),
                "sb": str(body.scheduled_by),
                "st": body.start_time,
                "en": body.end_time,
                **({"sch": str(body.school_id)} if body.school_id else {}),
            },
        )
        await db.commit()
        return row
    except IntegrityError as e:
        await db.rollback()
        if _is_assessment_schedule_duplicate(e):
            log.warning(
                "POST /v1/assessment-schedules 409: agendamento duplicado "
                "(assessment_id=%s classroom_id=%s)",
                body.assessment_id,
                body.classroom_id,
            )
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "Já existe agendamento para esta avaliação nesta turma. "
                "Apague ou edite o existente antes de criar outro.",
            ) from e
        raise


@router.get("/assessment-schedules/{schedule_id}")
async def get_schedule_v1(
    schedule_id: UUID,
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
    academic_year_id: UUID | None = None,
):
    effective_ay = await resolve_academic_year_id(db, academic_year_id)
    cscope = await get_effective_classroom_scope(db, ctx)
    row = await fetch_one(
        db,
        """
        SELECT ass.*, a.title AS _assessment_title
        FROM assessment_schedules ass
        JOIN assessments a ON a.id = ass.assessment_id
        JOIN classrooms c ON c.id = ass.classroom_id
        WHERE ass.id = CAST(:id AS uuid)
          AND c.academic_year_id = CAST(:ay AS uuid)
        """,
        {"id": str(schedule_id), "ay": str(effective_ay)},
    )
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Schedule not found")
    if not cscope["is_admin_like"] and str(row["classroom_id"]) not in {
        str(x) for x in (cscope["effective_classroom_ids"] or [])
    }:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Fora do escopo")
    return row


@router.get("/assessment-schedules/{schedule_id}/roster")
async def get_schedule_roster_v1(
    schedule_id: UUID,
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
    academic_year_id: UUID | None = Query(None, description="Ano letivo. Se omitido, usa is_primary=true."),
):
    """Contexto do agendamento: schedule, turma, professores, alunos matriculados (classroom_students) e presença."""
    sch = await get_schedule_v1(schedule_id, ctx, db, academic_year_id=academic_year_id)
    cid = sch["classroom_id"]
    classroom = await fetch_one(db, "SELECT * FROM classrooms WHERE id = CAST(:id AS uuid)", {"id": str(cid)})
    teachers = await fetch_all(
        db,
        """
        SELECT ct.*, p.role::text AS profile_role, p2.full_name, p2.email
        FROM classroom_teachers ct
        JOIN profiles p ON p.id = ct.teacher_id
        JOIN people p2 ON p2.id = p.person_id
        WHERE ct.classroom_id = CAST(:cid AS uuid)
        ORDER BY ct.teacher_id
        """,
        {"cid": str(cid)},
    )
    students = await fetch_all(
        db,
        """
        SELECT
          cs.classroom_id,
          cs.student_id,
          p.code,
          p2.full_name,
          p2.email,
          p2.metadata,
          NULL::uuid AS teacher_id
        FROM classroom_students cs
        INNER JOIN profiles p ON p.id = cs.student_id
        INNER JOIN people p2 ON p2.id = p.person_id
        WHERE cs.classroom_id = CAST(:cid AS uuid)
          AND p2.status = 'published'
        ORDER BY p2.full_name NULLS LAST, cs.student_id
        """,
        {"cid": str(cid)},
    )
    attendance = await fetch_all(
        db,
        """
        SELECT
          aal.*,
          p2.full_name AS student_full_name,
          p2.email AS student_email
        FROM assessment_attendance_list aal
        LEFT JOIN profiles p ON p.id = aal.student_id
        LEFT JOIN people p2 ON p2.id = p.person_id
        WHERE aal.assessment_schedules_id = CAST(:sid AS uuid)
        ORDER BY aal.id ASC
        """,
        {"sid": str(schedule_id)},
    )
    ay = classroom.get("academic_year_id") if classroom else None
    return {
        "schedule": sch,
        "classroom": classroom,
        "teachers": teachers,
        "students": students,
        "attendance": attendance,
        **({"academic_year_id": str(ay)} if ay is not None else {}),
    }


@router.get("/assessment-attendance/answer-card-pdf")
async def download_answer_card_pdf_by_attendance_code(
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
    code: str = Query(..., min_length=1, description="assessment_attendance_list.code"),
    academic_year_id: UUID | None = Query(
        None,
        description="Ano letivo do agendamento; se omitido, usa o da turma vinculada ao código.",
    ),
):
    """PDF do cartão de resposta (ReportLab) para um `codigo_cartao` (presença na lista)."""
    meta = await fetch_one(
        db,
        """
        SELECT ass.id AS schedule_id, c.academic_year_id
        FROM assessment_attendance_list aal
        JOIN assessment_schedules ass ON ass.id = aal.assessment_schedules_id
        JOIN classrooms c ON c.id = ass.classroom_id
        WHERE lower(trim(aal.code::text)) = lower(trim(:code))
        LIMIT 1
        """,
        {"code": code.strip()},
    )
    if not meta:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Código de presença não encontrado")
    sid = UUID(str(meta["schedule_id"]))
    ay = academic_year_id or UUID(str(meta["academic_year_id"]))
    await get_schedule_v1(sid, ctx, db, academic_year_id=ay)

    row = await fetch_one(
        db,
        """
        SELECT
          agendamento,
          caderno,
          ano_serie,
          turma,
          estudante,
          ra_codigo,
          codigo_cartao,
          escola,
          qr_code_text,
          titulo,
          logo_url,
          output
        FROM vw_student_assessment
        WHERE lower(trim(codigo_cartao::text)) = lower(trim(:code))
        LIMIT 1
        """,
        {"code": code.strip()},
    )
    if not row:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "View vw_student_assessment indisponível ou sem linha para este código, ou colunas "
            "desatualizadas. Aplique a migração 006_vw_student_assessment.sql (campos: "
            "codigo_cartao, qr_code_text, titulo, logo_url, output, …).",
        )
    try:
        pdf = build_cartao_resposta_pdf_bytes_from_view_row(dict(row))
    except Exception as e:  # noqa: BLE001
        log.exception("answer-card-pdf: falha ao montar PDF code=%s", code)
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "Falha ao gerar o PDF do cartão.",
        ) from e

    filename = suggested_download_filename(dict(row), fallback_code=code.strip())
    cd = f"attachment; filename=\"{filename}\"; filename*=UTF-8''{quote(filename)}"
    return Response(content=pdf, media_type="application/pdf", headers={"Content-Disposition": cd})


@router.get("/assessment-schedules/{schedule_id}/answer-cards-combined-pdf")
async def download_combined_answer_cards_pdf(
    schedule_id: UUID,
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
    academic_year_id: UUID | None = Query(
        None,
        description="Ano letivo do agendamento (mesmo critério que GET do schedule).",
    ),
):
    """Um único PDF com todas as folhas-resposta do agendamento (`vw_student_assessment.agendamento`)."""
    await get_schedule_v1(schedule_id, ctx, db, academic_year_id=academic_year_id)
    try:
        rows = await fetch_all(
            db,
            """
            SELECT
              agendamento,
              caderno,
              ano_serie,
              turma,
              estudante,
              ra_codigo,
              codigo_cartao,
              escola,
              qr_code_text,
              titulo,
              logo_url,
              output
            FROM vw_student_assessment
            WHERE agendamento = CAST(:sid AS uuid)
            ORDER BY estudante NULLS LAST, codigo_cartao
            """,
            {"sid": str(schedule_id)},
        )
    except ProgrammingError as e:
        log.exception("answer-cards-combined-pdf: view ou colunas inválidas schedule=%s", schedule_id)
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "View vw_student_assessment indisponível ou colunas desatualizadas.",
        ) from e
    if not rows:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "Nenhum cartão-resposta para este agendamento (lista vazia na view ou todos com status out).",
        )
    parts: list[bytes] = []
    try:
        for r in rows:
            parts.append(build_cartao_resposta_pdf_bytes_from_view_row(dict(r)))
        pdf = merge_pdf_bytes(parts)
    except Exception as e:  # noqa: BLE001
        log.exception("answer-cards-combined-pdf: falha ao gerar schedule=%s", schedule_id)
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "Falha ao gerar o PDF combinado dos cartões.",
        ) from e
    base = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in str(schedule_id))[:36]
    filename = f"cartoes-resposta-agendamento-{base}.pdf"
    cd = f"attachment; filename=\"{filename}\"; filename*=UTF-8''{quote(filename)}"
    return Response(content=pdf, media_type="application/pdf", headers={"Content-Disposition": cd})


@router.patch("/assessment-schedules/{schedule_id}")
async def patch_schedule_v1(
    schedule_id: UUID,
    body: dict[str, Any],
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
    academic_year_id: UUID | None = None,
):
    if not is_staff_admin_role(ctx.role):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Apenas administradores")
    row = await get_schedule_v1(schedule_id, ctx, db, academic_year_id=academic_year_id)
    sets = []
    params: dict[str, Any] = {"id": str(schedule_id)}
    if "start_time" in body and body["start_time"] is not None:
        sets.append("start_time = CAST(:st AS timestamptz)")
        params["st"] = _coerce_timestamptz(body["start_time"], field="start_time")
    if "end_time" in body and body["end_time"] is not None:
        sets.append("end_time = CAST(:en AS timestamptz)")
        params["en"] = _coerce_timestamptz(body["end_time"], field="end_time")
    if "classroom_id" in body and body["classroom_id"] is not None:
        classroom_row = await fetch_one(
            db,
            "SELECT id, school_id FROM classrooms WHERE id = CAST(:id AS uuid)",
            {"id": str(body["classroom_id"])},
        )
        if not classroom_row:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Classroom not found")
        assessment_row = await fetch_one(
            db,
            "SELECT id, school_id FROM assessments WHERE id = CAST(:id AS uuid)",
            {"id": str(row["assessment_id"])},
        )
        if assessment_row and not await _assessment_covers_classroom_school(
            db,
            assessment_id=UUID(str(assessment_row["id"])),
            assessment_primary_school_id=assessment_row["school_id"],
            classroom_school_id=classroom_row["school_id"],
        ):
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "Assessment e classroom de escolas diferentes "
                f"(assessment.school_id={assessment_row['school_id']}, classroom.school_id={classroom_row['school_id']}). "
                "Confirme um vínculo em assessment_school para a escola da turma ou alinhe assessments.school_id.",
            )
        sets.append("classroom_id = CAST(:cid AS uuid)")
        params["cid"] = str(body["classroom_id"])
    if "scheduled_by" in body and body["scheduled_by"] is not None:
        sets.append("scheduled_by = CAST(:sb AS uuid)")
        params["sb"] = str(body["scheduled_by"])
    if "school_id" in body:
        if body["school_id"] is None:
            sets.append("school_id = NULL")
        else:
            sets.append("school_id = CAST(:sid AS uuid)")
            params["sid"] = str(body["school_id"])
    if not sets:
        return row
    out = await fetch_one(
        db,
        f"UPDATE assessment_schedules SET {', '.join(sets)} WHERE id = CAST(:id AS uuid) RETURNING *",
        params,
    )
    await db.commit()
    return out


@router.delete("/assessment-schedules/{schedule_id}")
async def delete_schedule_v1(
    schedule_id: UUID,
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
    academic_year_id: UUID | None = None,
):
    if not is_staff_admin_role(ctx.role):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Apenas administradores")
    await get_schedule_v1(schedule_id, ctx, db, academic_year_id=academic_year_id)
    await execute(db, "DELETE FROM assessment_schedules WHERE id = CAST(:id AS uuid)", {"id": str(schedule_id)})
    await db.commit()
    return {"ok": True}


@router.get("/assessment-schedules/{schedule_id}/attendance")
async def list_attendance_v1(
    schedule_id: UUID,
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
    academic_year_id: UUID | None = Query(None, description="Ano letivo. Se omitido, usa is_primary=true."),
    pg: Annotated[PageArgs, Depends(pagination_params)] = PageArgs(1, 50),
):
    await get_schedule_v1(schedule_id, ctx, db, academic_year_id=academic_year_id)
    params = {"id": str(schedule_id)}
    attendance_from = """
        FROM assessment_attendance_list aal
        LEFT JOIN profiles p ON p.id = aal.student_id
        LEFT JOIN people p2 ON p2.id = p.person_id
        WHERE aal.assessment_schedules_id = CAST(:id AS uuid)
    """
    count_row = await fetch_one(
        db,
        f"SELECT COUNT(*)::int AS total {attendance_from}",
        params,
    )
    items = await fetch_all(
        db,
        f"""
        SELECT
          aal.*,
          p2.full_name AS student_full_name,
          p2.email AS student_email
        {attendance_from}
        ORDER BY aal.id DESC LIMIT {pg.per_page} OFFSET {pg.offset}
        """,
        params,
    )
    return paged_response(page=pg.page, per_page=pg.per_page, total=(count_row or {}).get("total", 0), items=items)


class AttendanceUpsert(BaseModel):
    student_id: UUID
    status: str | None = "registered"
    justification: str | None = None
    code: str | None = None


class AttendanceBulkPatchBody(BaseModel):
    items: list[AttendanceUpsert]


class ReinstateOutBody(BaseModel):
    student_id: UUID


def _attendance_code_conflict_message() -> str:
    return (
        "Este código já está em uso por outro aluno vinculado à avaliação. "
        "Use outro material ou confira o código digitado."
    )


def _is_attendance_code_unique_violation(exc: IntegrityError) -> bool:
    """Violação de unicidade na coluna `code` de `assessment_attendance_list`."""
    raw = str(exc.orig or exc).lower()
    if "23505" not in raw and "duplicate key" not in raw and "unique" not in raw:
        return False
    if "assessment_attendance_list_schedule_student" in raw:
        return False
    if "(code)=" in raw or "(code) =" in raw:
        return True
    if "code_key" in raw or "code_uidx" in raw or "assessment_attendance_list_code" in raw.replace(" ", ""):
        return True
    return False


async def _apply_attendance_upserts_and_commit(
    db: AsyncSession,
    schedule_id: UUID,
    updates: list[AttendanceUpsert],
) -> list[Any]:
    try:
        out = await _apply_attendance_upserts(db, schedule_id, updates)
        await db.commit()
        return out
    except IntegrityError as e:
        await db.rollback()
        if _is_attendance_code_unique_violation(e):
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                _attendance_code_conflict_message(),
            ) from e
        raise


async def _apply_attendance_upserts(
    db: AsyncSession,
    schedule_id: UUID,
    updates: list[AttendanceUpsert],
) -> list[Any]:
    out: list[Any] = []
    for up in updates:
        explicit_code = (up.code or "").strip() or None
        row = await fetch_one(
            db,
            """
            INSERT INTO assessment_attendance_list (
                assessment_schedules_id, student_id, status, justification, code
            )
            VALUES (
              CAST(:sid AS uuid),
              CAST(:st AS uuid),
              CAST(:status AS attendance_list),
              :just,
              :code
            )
            ON CONFLICT ON CONSTRAINT assessment_attendance_list_schedule_student_key
            DO UPDATE SET
              status = EXCLUDED.status,
              justification = COALESCE(EXCLUDED.justification, assessment_attendance_list.justification),
              code = COALESCE(EXCLUDED.code, assessment_attendance_list.code)
            RETURNING *
            """,
            {
                "sid": str(schedule_id),
                "st": str(up.student_id),
                "status": up.status or "registered",
                "just": up.justification,
                "code": explicit_code,
            },
        )
        if row is not None and explicit_code is None:
            c = row.get("code")
            if c is None or str(c).strip() == "":
                updated = await fetch_one(
                    db,
                    """
                    UPDATE assessment_attendance_list
                    SET code = 'QR' || id::text
                    WHERE id = CAST(:id AS bigint)
                      AND (code IS NULL OR BTRIM(code::text) = '')
                    RETURNING *
                    """,
                    {"id": row["id"]},
                )
                if updated is not None:
                    row = updated
        if row is not None:
            out.append(row)
    return out


@router.post("/assessment-schedules/{schedule_id}/attendance")
async def upsert_attendance_v1(
    schedule_id: UUID,
    body: AttendanceUpsert,
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
    academic_year_id: UUID | None = None,
):
    sch = await get_schedule_v1(schedule_id, ctx, db, academic_year_id=academic_year_id)
    await _assert_schedule_attendance_editor(db, ctx, sch)
    rows = await _apply_attendance_upserts_and_commit(db, schedule_id, [body])
    return rows[0]


@router.patch("/assessment-schedules/{schedule_id}/attendance")
async def patch_attendance_bulk_v1(
    schedule_id: UUID,
    updates: list[AttendanceUpsert],
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
    academic_year_id: UUID | None = None,
):
    sch = await get_schedule_v1(schedule_id, ctx, db, academic_year_id=academic_year_id)
    await _assert_schedule_attendance_editor(db, ctx, sch)
    return await _apply_attendance_upserts_and_commit(db, schedule_id, updates)


@router.patch("/assessment-schedules/{schedule_id}/attendance/bulk")
async def patch_attendance_bulk_explicit_v1(
    schedule_id: UUID,
    body: AttendanceBulkPatchBody,
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
    academic_year_id: UUID | None = Query(None, description="Ano letivo. Se omitido, usa is_primary=true."),
):
    sch = await get_schedule_v1(schedule_id, ctx, db, academic_year_id=academic_year_id)
    await _assert_schedule_attendance_editor(db, ctx, sch)
    out = await _apply_attendance_upserts_and_commit(db, schedule_id, body.items)
    return {"items": out, "count": len(out)}


@router.patch("/assessment-schedules/{schedule_id}/attendance/reinstate")
async def reinstate_out_student_v1(
    schedule_id: UUID,
    body: ReinstateOutBody,
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
    academic_year_id: UUID | None = Query(
        None,
        description="Ano letivo. Se omitido, usa is_primary=true.",
    ),
):
    """Volta aluno com status `out` para `registered` sem alterar `code` (evita upsert INSERT)."""
    sch = await get_schedule_v1(schedule_id, ctx, db, academic_year_id=academic_year_id)
    await _assert_schedule_attendance_editor(db, ctx, sch)
    row = await fetch_one(
        db,
        """
        UPDATE assessment_attendance_list
        SET status = 'registered'::attendance_list
        WHERE assessment_schedules_id = CAST(:sid AS uuid)
          AND student_id = CAST(:st AS uuid)
          AND status = 'out'::attendance_list
        RETURNING *
        """,
        {"sid": str(schedule_id), "st": str(body.student_id)},
    )
    if not row:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "Não há registro deste aluno em status «out» neste agendamento.",
        )
    await db.commit()
    return row


@router.delete("/assessment-schedules/{schedule_id}/attendance/{attendance_id}")
async def delete_attendance_v1(
    schedule_id: UUID,
    attendance_id: int,
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
    academic_year_id: UUID | None = None,
):
    if not is_staff_admin_role(ctx.role):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Apenas administradores")
    await get_schedule_v1(schedule_id, ctx, db, academic_year_id=academic_year_id)
    row = await fetch_one(
        db,
        """
        UPDATE assessment_attendance_list
        SET status = 'out'::attendance_list
        WHERE id = :aid AND assessment_schedules_id = CAST(:sid AS uuid)
        RETURNING *
        """,
        {"aid": attendance_id, "sid": str(schedule_id)},
    )
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Linha de presença não encontrada")
    await db.commit()
    return row


@router.post("/assessment-schedules/{schedule_id}/attendance-sheet/upload")
async def upload_schedule_attendance_sheet_v1(
    schedule_id: UUID,
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
    file: UploadFile = File(...),
    academic_year_id: UUID | None = Query(
        None,
        description="Ano letivo. Se omitido, usa is_primary=true.",
    ),
):
    """Armazena ficheiro de lista de presença (PDF ou imagem). Cada envio cria uma nova versão (não substitui anteriores)."""
    sch = await get_schedule_v1(schedule_id, ctx, db, academic_year_id=academic_year_id)
    await _assert_schedule_attendance_editor(db, ctx, sch)
    raw_ct = (file.content_type or "").split(";")[0].strip().lower()
    data = await file.read()
    guessed_ct = _guess_sheet_content_type(raw_ct, file.filename, data)
    if guessed_ct not in _ALLOWED_ATTENDANCE_SHEET_MEDIA:
        raise HTTPException(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            "Tipo de ficheiro não permitido (use PDF ou imagem JPEG/PNG/WebP).",
        )
    if len(data) > settings.attendance_sheet_max_bytes:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "Ficheiro demasiado grande")
    storage_key = f"{uuid.uuid4()}.upload"
    store_attendance_sheet_bytes(storage_key, data)
    safe_name = (file.filename or "lista-presenca").replace("..", "_").strip()[:255] or "lista-presenca"
    try:
        row = await fetch_one(
            db,
            """
            INSERT INTO assessment_schedule_attendance_sheet (
                assessment_schedules_id, storage_key, original_filename, content_type, size_bytes, uploaded_by
            )
            VALUES (
                CAST(:sid AS uuid), :sk, :ofn, :ct, :sz, CAST(:up AS uuid)
            )
            RETURNING *
            """,
            {
                "sid": str(schedule_id),
                "sk": storage_key,
                "ofn": safe_name,
                "ct": guessed_ct,
                "sz": len(data),
                "up": str(ctx.active_profile_id),
            },
        )
    except Exception as exc:
        if _looks_like_attendance_sheet_table_missing(exc):
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "Recurso de lista de presença ainda não está disponível neste ambiente (migração pendente).",
            )
        raise
    await db.commit()
    return row


@router.get("/assessment-schedules/{schedule_id}/attendance-sheet")
async def get_schedule_attendance_sheet_latest_v1(
    schedule_id: UUID,
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
    academic_year_id: UUID | None = Query(None, description="Ano letivo. Se omitido, usa is_primary=true."),
):
    """Último upload de lista de presença para este agendamento (ou objeto vazio)."""
    sch = await get_schedule_v1(schedule_id, ctx, db, academic_year_id=academic_year_id)
    await _assert_schedule_attendance_editor(db, ctx, sch)
    try:
        row = await fetch_one(
            db,
            """
            SELECT * FROM assessment_schedule_attendance_sheet
            WHERE assessment_schedules_id = CAST(:sid AS uuid)
            ORDER BY created_at DESC
            LIMIT 1
            """,
            {"sid": str(schedule_id)},
        )
    except Exception as exc:
        if _looks_like_attendance_sheet_table_missing(exc):
            return {}
        raise
    return row if row else {}


@router.get("/assessment-schedules/{schedule_id}/attendance-sheet/versions")
async def list_schedule_attendance_sheet_versions_v1(
    schedule_id: UUID,
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
    academic_year_id: UUID | None = Query(None, description="Ano letivo. Se omitido, usa is_primary=true."),
):
    """Todas as versões de upload para este agendamento (mais recente primeiro)."""
    sch = await get_schedule_v1(schedule_id, ctx, db, academic_year_id=academic_year_id)
    await _assert_schedule_attendance_editor(db, ctx, sch)
    try:
        rows = await fetch_all(
            db,
            """
            SELECT id, original_filename, content_type, size_bytes, uploaded_by, created_at
            FROM assessment_schedule_attendance_sheet
            WHERE assessment_schedules_id = CAST(:sid AS uuid)
            ORDER BY created_at DESC
            """,
            {"sid": str(schedule_id)},
        )
    except Exception as exc:
        if _looks_like_attendance_sheet_table_missing(exc):
            return {"items": []}
        raise
    return {"items": rows}


@router.get("/assessment-schedules/{schedule_id}/attendance-sheet/file")
async def download_schedule_attendance_sheet_upload_v1(
    schedule_id: UUID,
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
    academic_year_id: UUID | None = Query(None, description="Ano letivo. Se omitido, usa is_primary=true."),
    attendance_sheet_id: UUID | None = Query(
        None,
        description="ID da versão (registo em assessment_schedule_attendance_sheet). Omitido = última versão.",
    ),
):
    """Download do ficheiro enviado (última versão ou uma versão indicada por attendance_sheet_id)."""
    sch = await get_schedule_v1(schedule_id, ctx, db, academic_year_id=academic_year_id)
    await _assert_schedule_attendance_editor(db, ctx, sch)
    try:
        if attendance_sheet_id is not None:
            row = await fetch_one(
                db,
                """
                SELECT * FROM assessment_schedule_attendance_sheet
                WHERE id = CAST(:id AS uuid)
                  AND assessment_schedules_id = CAST(:sid AS uuid)
                """,
                {"id": str(attendance_sheet_id), "sid": str(schedule_id)},
            )
        else:
            row = await fetch_one(
                db,
                """
                SELECT * FROM assessment_schedule_attendance_sheet
                WHERE assessment_schedules_id = CAST(:sid AS uuid)
                ORDER BY created_at DESC
                LIMIT 1
                """,
                {"sid": str(schedule_id)},
            )
    except Exception as exc:
        if _looks_like_attendance_sheet_table_missing(exc):
            raise HTTPException(
                status.HTTP_404_NOT_FOUND,
                "Nenhum ficheiro de lista de presença para este agendamento.",
            )
        raise
    if not row:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "Nenhum ficheiro de lista de presença para este agendamento.",
        )
    sk = row.get("storage_key")
    if not sk:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Metadados de armazenamento inválidos.")
    try:
        body = load_attendance_sheet_bytes(str(sk))
    except FileNotFoundError:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "Ficheiro não encontrado no armazenamento.",
        )
    ct = row.get("content_type") or "application/octet-stream"
    ofn = row.get("original_filename")
    fname = _attendance_sheet_attachment_filename(str(ofn) if ofn is not None else None)
    return Response(
        content=body,
        media_type=str(ct),
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@router.get("/assessment-schedules/{schedule_id}/attendance-sheet/pdf")
async def download_schedule_attendance_sheet_pdf_v1(
    schedule_id: UUID,
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
    academic_year_id: UUID | None = Query(None, description="Ano letivo. Se omitido, usa is_primary=true."),
):
    """PDF gerado no servidor com alunos da turma e coluna para assinatura."""
    sch = await get_schedule_v1(schedule_id, ctx, db, academic_year_id=academic_year_id)
    await _assert_schedule_attendance_editor(db, ctx, sch)
    roster = await get_schedule_roster_v1(schedule_id, ctx, db, academic_year_id=academic_year_id)
    schedule = roster["schedule"] or {}
    classroom = roster["classroom"] or {}
    students = list(roster.get("students") or [])
    aid = schedule.get("assessment_id")
    arow = await fetch_one(
        db,
        """
        SELECT a.title AS title, s.name AS school_name, s.id AS school_id
        FROM assessments a
        JOIN schools s ON s.id = a.school_id
        WHERE a.id = CAST(:id AS uuid)
        """,
        {"id": str(aid)},
    )
    if not arow:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Avaliação não encontrada")
    ay_label: str | None = None
    ay_id = classroom.get("academic_year_id")
    if ay_id:
        ay = await fetch_one(
            db,
            "SELECT year FROM academic_years WHERE id = CAST(:id AS uuid)",
            {"id": str(ay_id)},
        )
        if ay and ay.get("year") is not None:
            ay_label = str(ay["year"])
    pdf_bytes = build_attendance_sheet_pdf_bytes(
        school_name=str(arow.get("school_name") or "—"),
        classroom_name=str(classroom.get("name") or "—"),
        classroom_code=(str(classroom["code"]) if classroom.get("code") else None),
        assessment_title=str(arow.get("title") or "—"),
        schedule_start=schedule.get("start_time"),
        schedule_end=schedule.get("end_time"),
        academic_year_label=ay_label,
        school_id=(str(arow["school_id"]) if arow.get("school_id") else None),
        classroom_id=(str(classroom["id"]) if classroom.get("id") else None),
        assessment_id=(str(aid) if aid else None),
        students=students,
    )
    fname = f"lista-presenca-{schedule_id}.pdf"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


class MacroAssessmentCreate(BaseModel):
    title: str
    description: str | None = None
    start_date: datetime | None = None
    end_date: datetime | None = None


class MacroAssessmentPatch(BaseModel):
    title: str | None = None
    description: str | None = None
    start_date: datetime | None = None
    end_date: datetime | None = None


def _assert_platform_catalog_admin(ctx: AuthContext) -> None:
    if not is_admin_like(ctx.role):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Apenas administradores da plataforma")


@router.get("/macro-assessments")
async def list_macro_assessments_admin_v1(
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
    pg: Annotated[PageArgs, Depends(pagination_params)] = PageArgs(1, 50),
):
    """Lista macro avaliações para gestão administrativa."""
    _assert_platform_catalog_admin(ctx)
    count_row = await fetch_one(db, "SELECT COUNT(*)::int AS total FROM macro_assessments", {})
    items = await fetch_all(
        db,
        f"""
        SELECT ma.*,
               (SELECT COUNT(*)::int FROM assessments a WHERE a.macro_assessment_id = ma.id) AS caderno_count
        FROM macro_assessments ma
        ORDER BY ma.created_at DESC NULLS LAST
        LIMIT {pg.per_page} OFFSET {pg.offset}
        """,
        {},
    )
    return paged_response(page=pg.page, per_page=pg.per_page, total=(count_row or {}).get("total", 0), items=items)


@router.post("/macro-assessments")
async def create_macro_assessment_admin_v1(
    body: MacroAssessmentCreate,
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    _assert_platform_catalog_admin(ctx)
    row = await fetch_one(
        db,
        """
        INSERT INTO macro_assessments (title, description, start_date, end_date)
        VALUES (:title, :description, :start_date, :end_date)
        RETURNING *
        """,
        {
            "title": body.title,
            "description": body.description,
            "start_date": body.start_date,
            "end_date": body.end_date,
        },
    )
    await db.commit()
    return row


@router.get("/macro-assessments/{macro_id}")
async def get_macro_assessment_admin_v1(
    macro_id: UUID,
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    _assert_platform_catalog_admin(ctx)
    row = await fetch_one(
        db,
        "SELECT * FROM macro_assessments WHERE id = CAST(:id AS uuid)",
        {"id": str(macro_id)},
    )
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Macro avaliação não encontrada")
    cadernos = await fetch_all(
        db,
        """
        SELECT id, title, type, school_id
        FROM assessments
        WHERE macro_assessment_id = CAST(:mid AS uuid)
        ORDER BY title
        """,
        {"mid": str(macro_id)},
    )
    return {"macro": row, "cadernos": cadernos}


@router.patch("/macro-assessments/{macro_id}")
async def patch_macro_assessment_admin_v1(
    macro_id: UUID,
    body: MacroAssessmentPatch,
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    _assert_platform_catalog_admin(ctx)
    row = await fetch_one(
        db,
        "SELECT id FROM macro_assessments WHERE id = CAST(:id AS uuid)",
        {"id": str(macro_id)},
    )
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Macro avaliação não encontrada")
    sets = []
    params: dict[str, Any] = {"id": str(macro_id)}
    if body.title is not None:
        sets.append("title = :title")
        params["title"] = body.title
    if body.description is not None:
        sets.append("description = :description")
        params["description"] = body.description
    if body.start_date is not None:
        sets.append("start_date = :start_date")
        params["start_date"] = body.start_date
    if body.end_date is not None:
        sets.append("end_date = :end_date")
        params["end_date"] = body.end_date
    if not sets:
        return await get_macro_assessment_admin_v1(macro_id, ctx, db)
    sql = f"UPDATE macro_assessments SET {', '.join(sets)} WHERE id = CAST(:id AS uuid) RETURNING *"
    out = await fetch_one(db, sql, params)
    await db.commit()
    return out


@router.delete("/macro-assessments/{macro_id}")
async def delete_macro_assessment_admin_v1(
    macro_id: UUID,
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    _assert_platform_catalog_admin(ctx)
    linked = await fetch_one(
        db,
        "SELECT id FROM assessments WHERE macro_assessment_id = CAST(:id AS uuid) LIMIT 1",
        {"id": str(macro_id)},
    )
    if linked:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Macro possui cadernos vinculados; desvincule ou remova os cadernos antes.",
        )
    await execute(db, "DELETE FROM macro_assessments WHERE id = CAST(:id AS uuid)", {"id": str(macro_id)})
    await db.commit()
    return {"ok": True}
