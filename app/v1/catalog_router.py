"""Escolas, turmas e filtros (Etapa 3). Escopo mínimo por perfil; árvore via view schools_hierarchy."""

import logging
from datetime import date
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
)
from app.v1._sql import execute, fetch_all, fetch_one

router = APIRouter(tags=["v1-catalog"])
logger = logging.getLogger(__name__)


async def _fetch_classroom_scoped(
    db: AsyncSession,
    ctx: AuthContext,
    classroom_id: UUID,
    academic_year_id: UUID | None,
) -> dict[str, Any]:
    effective_ay = await resolve_academic_year_id(db, academic_year_id)
    cscope = await get_effective_classroom_scope(db, ctx)
    row = await fetch_one(
        db,
        "SELECT * FROM classrooms WHERE id = CAST(:id AS uuid) AND academic_year_id = CAST(:ay AS uuid)",
        {"id": str(classroom_id), "ay": str(effective_ay)},
    )
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Classroom not found")
    if not cscope["is_admin_like"] and str(row["id"]) not in {str(x) for x in cscope["effective_classroom_ids"]}:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Fora do escopo")
    return row


async def _fetch_classroom_by_id_scoped(
    db: AsyncSession,
    ctx: AuthContext,
    classroom_id: UUID,
) -> dict[str, Any]:
    """Turma por `id` (qualquer ano letivo) + escopo — para mutações sem `academic_year_id` na query."""
    cscope = await get_effective_classroom_scope(db, ctx)
    row = await fetch_one(
        db,
        "SELECT * FROM classrooms WHERE id = CAST(:id AS uuid)",
        {"id": str(classroom_id)},
    )
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Classroom not found")
    if not cscope["is_admin_like"] and str(row["id"]) not in {str(x) for x in cscope["effective_classroom_ids"]}:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Fora do escopo")
    return row


# --- Schools ---


@router.get("/schools")
async def list_schools_v1(
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
    tree: bool = Query(False, description="Se true, retorna schools_tree"),
    root_id: UUID | None = Query(None, description="Filtrar raiz da árvore (opcional)"),
    school_type: str | None = Query(
        None,
        alias="type",
        description="Filtrar por tipo de escola (ex.: school unit)",
    ),
    pg: Annotated[PageArgs, Depends(pagination_params)] = PageArgs(1, 50),
):
    scope = await get_effective_school_scope(db, ctx)
    if tree:
        sql = "SELECT * FROM schools_hierarchy WHERE 1=1"
        params: dict[str, Any] = {}
        if root_id:
            sql += " AND root_id = CAST(:root_id AS uuid)"
            params["root_id"] = str(root_id)
        if school_type:
            sql += " AND type = CAST(:school_type AS school_type)"
            params["school_type"] = school_type
        if not scope["is_admin_like"]:
            sql += " AND id = ANY(CAST(:effective_school_ids AS uuid[]))"
            params["effective_school_ids"] = [str(x) for x in scope["effective_school_ids"]]
        count_row = await fetch_one(db, f"SELECT COUNT(*)::int AS total FROM ({sql}) q", params)
        page_sql = f"{sql} ORDER BY root_id, id LIMIT {pg.per_page} OFFSET {pg.offset}"
        items = await fetch_all(db, page_sql, params)
        return paged_response(page=pg.page, per_page=pg.per_page, total=(count_row or {}).get("total", 0), items=items)
    sql = "SELECT * FROM schools WHERE 1=1"
    params = {}
    if school_type:
        sql += " AND type = CAST(:school_type AS school_type)"
        params["school_type"] = school_type
    if not scope["is_admin_like"]:
        sql += " AND id = ANY(CAST(:effective_school_ids AS uuid[]))"
        params["effective_school_ids"] = [str(x) for x in scope["effective_school_ids"]]
    count_row = await fetch_one(db, f"SELECT COUNT(*)::int AS total FROM ({sql}) q", params)
    items = await fetch_all(db, f"{sql} ORDER BY name LIMIT {pg.per_page} OFFSET {pg.offset}", params)
    return paged_response(page=pg.page, per_page=pg.per_page, total=(count_row or {}).get("total", 0), items=items)


class SchoolCreate(BaseModel):
    name: str
    code: str
    city: str | None = None
    state: str | None = None
    parent: UUID | None = None


class SchoolPatch(BaseModel):
    name: str | None = None
    code: str | None = None
    city: str | None = None
    state: str | None = None
    parent: UUID | None = None
    bot_enabled: bool | None = None


@router.get("/schools/scope")
async def schools_scope_v1(
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    sscope = await get_effective_school_scope(db, ctx)
    cscope = await get_effective_classroom_scope(db, ctx)
    return {
        "person_id": str(ctx.person_id),
        "active_profile_id": str(ctx.active_profile_id),
        "role": ctx.role,
        "school_id": str(ctx.school_id) if ctx.school_id else None,
        "is_admin_like": is_admin_like(ctx.role),
        "effective_school_ids": None
        if sscope["effective_school_ids"] is None
        else [str(x) for x in sscope["effective_school_ids"]],
        "effective_classroom_ids": None
        if cscope["effective_classroom_ids"] is None
        else [str(x) for x in cscope["effective_classroom_ids"]],
    }


@router.post("/schools")
async def create_school_v1(
    body: SchoolCreate,
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    if not is_admin_like(ctx.role):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Apenas administradores")
    q = """
    INSERT INTO schools (name, code, city, state, parent)
    VALUES (:name, :code, :city, :state, :parent)
    RETURNING *
    """
    row = await fetch_one(
        db,
        q,
        {
            "name": body.name,
            "code": body.code,
            "city": body.city,
            "state": body.state,
            "parent": str(body.parent) if body.parent else None,
        },
    )
    await db.commit()
    return row


@router.get("/schools/{school_id}")
async def get_school_v1(
    school_id: UUID,
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    scope = await get_effective_school_scope(db, ctx)
    row = await fetch_one(db, "SELECT * FROM schools WHERE id = CAST(:id AS uuid)", {"id": str(school_id)})
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "School not found")
    if not scope["is_admin_like"] and str(row["id"]) not in {str(x) for x in scope["effective_school_ids"]}:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Fora do escopo")
    return row


@router.patch("/schools/{school_id}")
async def patch_school_v1(
    school_id: UUID,
    body: SchoolPatch,
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    if not is_admin_like(ctx.role):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Apenas administradores")
    sets = []
    params: dict[str, Any] = {"id": str(school_id)}
    if body.name is not None:
        sets.append("name = :name")
        params["name"] = body.name
    if body.code is not None:
        sets.append("code = :code")
        params["code"] = body.code
    if body.city is not None:
        sets.append("city = :city")
        params["city"] = body.city
    if body.state is not None:
        sets.append("state = :state")
        params["state"] = body.state
    if body.parent is not None:
        sets.append("parent = CAST(:parent AS uuid)")
        params["parent"] = str(body.parent)
    if body.bot_enabled is not None:
        sets.append("bot_enabled = :bot_enabled")
        params["bot_enabled"] = body.bot_enabled
    if not sets:
        return await get_school_v1(school_id, ctx, db)
    sql = f"UPDATE schools SET {', '.join(sets)} WHERE id = CAST(:id AS uuid) RETURNING *"
    row = await fetch_one(db, sql, params)
    await db.commit()
    return row


@router.delete("/schools/{school_id}")
async def delete_school_v1(
    school_id: UUID,
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    if not is_admin_like(ctx.role):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Apenas administradores")
    kids = await fetch_all(
        db,
        "SELECT id FROM schools WHERE parent = CAST(:id AS uuid) LIMIT 1",
        {"id": str(school_id)},
    )
    if kids:
        raise HTTPException(status.HTTP_409_CONFLICT, "Escola com filhos; remova ou mova antes.")
    await execute(db, "DELETE FROM schools WHERE id = CAST(:id AS uuid)", {"id": str(school_id)})
    await db.commit()
    return {"ok": True}


@router.get("/schools/{school_id}/is-parent")
async def school_is_parent_v1(
    school_id: UUID,
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    await get_school_v1(school_id, ctx, db)
    kids = await fetch_all(
        db,
        "SELECT 1 FROM schools WHERE parent = CAST(:id AS uuid) LIMIT 1",
        {"id": str(school_id)},
    )
    return {"school_id": str(school_id), "is_parent": len(kids) > 0}


# --- Classrooms ---


@router.get("/classrooms")
async def list_classrooms_v1(
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
    school_id: UUID | None = None,
    academic_year_id: UUID | None = Query(None, description="Ano letivo. Se omitido, usa is_primary=true."),
    grade_id: UUID | None = Query(None, description="Filtra turmas pela série (grade)."),
    segment_id: UUID | None = Query(None, description="Filtra turmas pelo segmento (via grade.segment_id)."),
    pg: Annotated[PageArgs, Depends(pagination_params)] = PageArgs(1, 50),
):
    effective_ay = await resolve_academic_year_id(db, academic_year_id)
    cscope = await get_effective_classroom_scope(db, ctx)
    sscope = await get_effective_school_scope(db, ctx)
    eff_cids = cscope.get("effective_classroom_ids")
    eff_cids_preview = (
        None if eff_cids is None else [str(x) for x in eff_cids[:30]] + (["…"] if len(eff_cids) > 30 else [])
    )
    logger.info(
        "[v1/classrooms] ctx person_id=%s active_profile_id=%s role=%r school_id=%s "
        "query_school_id=%s resolved_academic_year_id=%s is_admin_like=%s "
        "effective_classroom_ids_count=%s effective_classroom_ids_preview=%s "
        "effective_school_ids_count=%s",
        ctx.person_id,
        ctx.active_profile_id,
        ctx.role,
        str(ctx.school_id) if ctx.school_id else None,
        str(school_id) if school_id else None,
        str(effective_ay),
        cscope.get("is_admin_like"),
        None if eff_cids is None else len(eff_cids),
        eff_cids_preview,
        None if sscope.get("effective_school_ids") is None else len(sscope["effective_school_ids"] or []),
    )
    sql = "SELECT * FROM vw_classroom_list WHERE 1=1"
    params: dict[str, Any] = {}
    sql += " AND classroom_id IN (SELECT id FROM classrooms WHERE academic_year_id = CAST(:ay AS uuid))"
    params["ay"] = str(effective_ay)
    if school_id:
        if not cscope["is_admin_like"]:
            if is_teacher_like(ctx.role):
                allowed = await fetch_one(
                    db,
                    """
                    SELECT 1 AS ok FROM my_classrooms mc
                    WHERE mc.teacher_id = CAST(:pid AS uuid)
                      AND mc.school_id = CAST(:sid AS uuid)
                    LIMIT 1
                    """,
                    {"pid": str(ctx.active_profile_id), "sid": str(school_id)},
                )
                if not allowed:
                    raise HTTPException(
                        status.HTTP_403_FORBIDDEN,
                        "Sem turma sua nesta escola ou escola inválida para o filtro",
                    )
            else:
                eff_schools = sscope.get("effective_school_ids") or []
                if str(school_id) not in {str(x) for x in eff_schools}:
                    raise HTTPException(status.HTTP_403_FORBIDDEN, "Escola fora do escopo")
        # Subárvore da escola pedida (nó + descendentes), consistente com o KPI do dashboard.
        # Um admin escolar deve ver TODAS as turmas da sua escola e das escolas filhas;
        # filtrar por school_id exato escondia turmas de unidades-filhas (lista vazia × contador > 0).
        school_subtree = await get_descendant_school_ids(db, school_id)
        sql += " AND school_id = ANY(CAST(:school_subtree AS uuid[]))"
        params["school_subtree"] = [str(x) for x in school_subtree]
    if not cscope["is_admin_like"]:
        # Professor: turmas diretamente de `my_classrooms` (view base = `vw_teacher_classroom_options`)
        # + ano letivo já aplicado acima — não depender de `effective_classroom_ids` (pode divergir e zerar a lista).
        if is_teacher_like(ctx.role):
            sql += (
                " AND classroom_id IN ("
                " SELECT mc.classroom_id FROM my_classrooms mc"
                " INNER JOIN classrooms _cc ON _cc.id = mc.classroom_id"
                " WHERE mc.teacher_id = CAST(:_teacher_profile_id AS uuid)"
                " AND _cc.academic_year_id = CAST(:ay AS uuid))"
            )
            params["_teacher_profile_id"] = str(ctx.active_profile_id)
        else:
            sql += " AND classroom_id = ANY(CAST(:effective_classroom_ids AS uuid[]))"
            params["effective_classroom_ids"] = [str(x) for x in cscope["effective_classroom_ids"]]
    if grade_id is not None:
        sql += " AND classroom_id IN (SELECT id FROM classrooms WHERE grade_id = CAST(:grade_id AS uuid))"
        params["grade_id"] = str(grade_id)
    if segment_id is not None:
        sql += (
            " AND classroom_id IN ("
            " SELECT c.id FROM classrooms c"
            " INNER JOIN grades g ON g.id = c.grade_id"
            " WHERE g.segment_id = CAST(:segment_id AS uuid))"
        )
        params["segment_id"] = str(segment_id)
    page_sql = f"{sql} ORDER BY classroom_id LIMIT {pg.per_page} OFFSET {pg.offset}"
    count_sql = f"SELECT COUNT(*)::int AS total FROM ({sql}) q"
    logger.info("[v1/classrooms] count_sql=%r count_params=%r", count_sql, params)
    logger.info("[v1/classrooms] page_sql=%r page_params=%r", page_sql, params)
    count_row = await fetch_one(db, count_sql, params)
    items = await fetch_all(db, page_sql, params)
    return paged_response(page=pg.page, per_page=pg.per_page, total=(count_row or {}).get("total", 0), items=items)


class ClassroomCreate(BaseModel):
    school_id: UUID
    academic_year_id: UUID
    grade_id: UUID
    name: str
    code: str | None = None
    shift: str | None = None


class ClassroomPatch(BaseModel):
    name: str | None = None
    code: str | None = None
    shift: str | None = None
    grade_id: UUID | None = None
    academic_year_id: UUID | None = None


@router.post("/classrooms")
async def create_classroom_v1(
    body: ClassroomCreate,
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    if not is_admin_like(ctx.role):
        sscope = await get_effective_school_scope(db, ctx)
        allowed = {str(x) for x in (sscope["effective_school_ids"] or [])}
        if str(body.school_id) not in allowed:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Escola fora do escopo")
    row = await fetch_one(
        db,
        """
        INSERT INTO classrooms (school_id, academic_year_id, grade_id, code, name, shift)
        VALUES (CAST(:sid AS uuid), CAST(:ay AS uuid), CAST(:gid AS uuid), :code, :name, :shift)
        RETURNING *
        """,
        {
            "sid": str(body.school_id),
            "ay": str(body.academic_year_id),
            "gid": str(body.grade_id),
            "code": body.code,
            "name": body.name,
            "shift": body.shift,
        },
    )
    await db.commit()
    return row


@router.get("/classrooms/{classroom_id}")
async def get_classroom_v1(
    classroom_id: UUID,
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
    academic_year_id: UUID | None = Query(None, description="Ano letivo. Se omitido, usa is_primary=true."),
):
    """Detalhe da turma: linha `classrooms` + resumo de catálogo (`vw_classroom_list`) quando existir."""
    effective_ay = await resolve_academic_year_id(db, academic_year_id)
    row = await _fetch_classroom_scoped(db, ctx, classroom_id, academic_year_id)
    catalog = await fetch_one(
        db,
        "SELECT * FROM vw_classroom_list WHERE classroom_id = CAST(:id AS uuid)",
        {"id": str(classroom_id)},
    )
    return {
        "classroom": row,
        "catalog": catalog,
        "academic_year_id": str(effective_ay),
    }


@router.patch("/classrooms/{classroom_id}")
async def patch_classroom_v1(
    classroom_id: UUID,
    body: ClassroomPatch,
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    row = await _fetch_classroom_by_id_scoped(db, ctx, classroom_id)
    sets = []
    params: dict[str, Any] = {"id": str(classroom_id)}
    if body.name is not None:
        sets.append("name = :name")
        params["name"] = body.name
    if body.code is not None:
        sets.append("code = :code")
        params["code"] = body.code
    if body.shift is not None:
        sets.append("shift = :shift")
        params["shift"] = body.shift
    if body.grade_id is not None:
        sets.append("grade_id = CAST(:gid AS uuid)")
        params["gid"] = str(body.grade_id)
    if body.academic_year_id is not None:
        sets.append("academic_year_id = CAST(:ay AS uuid)")
        params["ay"] = str(body.academic_year_id)
    if not sets:
        return row
    sql = f"UPDATE classrooms SET {', '.join(sets)} WHERE id = CAST(:id AS uuid) RETURNING *"
    out = await fetch_one(db, sql, params)
    await db.commit()
    return out


@router.delete("/classrooms/{classroom_id}")
async def delete_classroom_v1(
    classroom_id: UUID,
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    await _fetch_classroom_by_id_scoped(db, ctx, classroom_id)
    if not is_admin_like(ctx.role):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Apenas administradores")
    await execute(db, "DELETE FROM classrooms WHERE id = CAST(:id AS uuid)", {"id": str(classroom_id)})
    await db.commit()
    return {"ok": True}


@router.get("/classrooms/{classroom_id}/students")
async def list_classroom_students_v1(
    classroom_id: UUID,
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
    q: str | None = Query(None, description="Busca em nome, e-mail ou código do aluno"),
    teacher_id: UUID | None = Query(None, description="Filtrar linhas vinculadas a este professor (profile_id)"),
    academic_year_id: UUID | None = Query(None, description="Ano letivo da turma. Se omitido, usa is_primary=true."),
    pg: Annotated[PageArgs, Depends(pagination_params)] = PageArgs(1, 50),
):
    effective_ay = await resolve_academic_year_id(db, academic_year_id)
    await _fetch_classroom_scoped(db, ctx, classroom_id, academic_year_id)
    params: dict[str, Any] = {"cid": str(classroom_id)}
    where_parts = ["v.classroom_id = CAST(:cid AS uuid)"]
    if teacher_id:
        where_parts.append("v.teacher_id = CAST(:tid AS uuid)")
        params["tid"] = str(teacher_id)
    if q and q.strip():
        where_parts.append(
            "(v.full_name ILIKE :qpat OR v.email ILIKE :qpat OR (v.code IS NOT NULL AND v.code::text ILIKE :qpat))"
        )
        params["qpat"] = f"%{q.strip()}%"
    where_sql = " AND ".join(where_parts)
    base = f"""
        SELECT DISTINCT ON (v.student_id)
          v.classroom_id, v.student_id, v.code, v.full_name, v.email, v.metadata, v.teacher_id
        FROM vw_classroom_students v
        WHERE {where_sql}
        ORDER BY v.student_id, v.teacher_id
    """
    count_sql = f"SELECT COUNT(*)::int AS total FROM ({base}) _distinct_students"
    count_row = await fetch_one(db, count_sql, params)
    total = (count_row or {}).get("total", 0)
    items = await fetch_all(
        db,
        f"{base} LIMIT {pg.per_page} OFFSET {pg.offset}",
        params,
    )
    return paged_response_with_academic_year(
        academic_year_id=effective_ay,
        page=pg.page,
        per_page=pg.per_page,
        total=total,
        items=items,
    )


class ClassroomStudentBody(BaseModel):
    student_id: UUID
    enrollment_code: str | None = None


@router.post("/classrooms/{classroom_id}/students")
async def add_classroom_student_v1(
    classroom_id: UUID,
    body: ClassroomStudentBody,
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    await _fetch_classroom_by_id_scoped(db, ctx, classroom_id)
    if not is_admin_like(ctx.role):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Apenas administradores")
    row = await fetch_one(
        db,
        """
        INSERT INTO classroom_students (classroom_id, student_id, enrollment_code)
        VALUES (CAST(:cid AS uuid), CAST(:sid AS uuid), :ecode)
        ON CONFLICT ON CONSTRAINT classroom_students_classroom_id_student_id_key
        DO UPDATE SET enrollment_code = COALESCE(EXCLUDED.enrollment_code, classroom_students.enrollment_code)
        RETURNING *
        """,
        {"cid": str(classroom_id), "sid": str(body.student_id), "ecode": body.enrollment_code},
    )
    await db.commit()
    return row


@router.delete("/classrooms/{classroom_id}/students/{student_id}")
async def remove_classroom_student_v1(
    classroom_id: UUID,
    student_id: UUID,
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    await _fetch_classroom_by_id_scoped(db, ctx, classroom_id)
    if not is_admin_like(ctx.role):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Apenas administradores")
    await execute(
        db,
        """
        DELETE FROM classroom_students
        WHERE classroom_id = CAST(:cid AS uuid) AND student_id = CAST(:sid AS uuid)
        """,
        {"cid": str(classroom_id), "sid": str(student_id)},
    )
    await db.commit()
    return {"ok": True}


@router.get("/classrooms/{classroom_id}/teachers")
async def list_classroom_teachers_v1(
    classroom_id: UUID,
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
    academic_year_id: UUID | None = Query(None, description="Ano letivo da turma. Se omitido, usa is_primary=true."),
    pg: Annotated[PageArgs, Depends(pagination_params)] = PageArgs(1, 50),
):
    effective_ay = await resolve_academic_year_id(db, academic_year_id)
    await _fetch_classroom_scoped(db, ctx, classroom_id, academic_year_id)
    params = {"cid": str(classroom_id)}
    sql = """
        SELECT ct.*, p.role::text AS profile_role, p2.full_name, p2.email
        FROM classroom_teachers ct
        JOIN profiles p ON p.id = ct.teacher_id
        JOIN people p2 ON p2.id = p.person_id
        WHERE ct.classroom_id = CAST(:cid AS uuid)
    """
    count_row = await fetch_one(db, f"SELECT COUNT(*)::int AS total FROM ({sql}) q", params)
    items = await fetch_all(
        db,
        f"{sql} ORDER BY ct.teacher_id LIMIT {pg.per_page} OFFSET {pg.offset}",
        params,
    )
    return paged_response_with_academic_year(
        academic_year_id=effective_ay,
        page=pg.page,
        per_page=pg.per_page,
        total=(count_row or {}).get("total", 0),
        items=items,
    )


class ClassroomTeacherBody(BaseModel):
    teacher_id: UUID


@router.post("/classrooms/{classroom_id}/teachers")
async def add_classroom_teacher_v1(
    classroom_id: UUID,
    body: ClassroomTeacherBody,
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    await _fetch_classroom_by_id_scoped(db, ctx, classroom_id)
    if not is_admin_like(ctx.role):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Apenas administradores")
    row = await fetch_one(
        db,
        """
        INSERT INTO classroom_teachers (classroom_id, teacher_id)
        VALUES (CAST(:cid AS uuid), CAST(:tid AS uuid))
        ON CONFLICT ON CONSTRAINT classroom_teachers_classroom_id_teacher_id_key DO NOTHING
        RETURNING *
        """,
        {"cid": str(classroom_id), "tid": str(body.teacher_id)},
    )
    await db.commit()
    return row or {"ok": True, "note": "already_linked"}


@router.delete("/classrooms/{classroom_id}/teachers/{teacher_id}")
async def remove_classroom_teacher_v1(
    classroom_id: UUID,
    teacher_id: UUID,
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    await _fetch_classroom_by_id_scoped(db, ctx, classroom_id)
    if not is_admin_like(ctx.role):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Apenas administradores")
    await execute(
        db,
        """
        DELETE FROM classroom_teachers
        WHERE classroom_id = CAST(:cid AS uuid) AND teacher_id = CAST(:tid AS uuid)
        """,
        {"cid": str(classroom_id), "tid": str(teacher_id)},
    )
    await db.commit()
    return {"ok": True}


@router.get("/classrooms/{classroom_id}/assessments")
async def list_classroom_assessments_v1(
    classroom_id: UUID,
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
    academic_year_id: UUID | None = Query(None, description="Ano letivo. Se omitido, usa is_primary=true."),
    active_only: bool = Query(False, description="Somente avaliações com assessment_school ativo na escola da turma"),
    pg: Annotated[PageArgs, Depends(pagination_params)] = PageArgs(1, 50),
):
    effective_ay = await resolve_academic_year_id(db, academic_year_id)
    await _fetch_classroom_scoped(db, ctx, classroom_id, academic_year_id)
    params: dict[str, Any] = {"cid": str(classroom_id)}
    sql = """
        SELECT DISTINCT a.*
        FROM assessments a
        INNER JOIN assessment_schedules sch ON sch.assessment_id = a.id
        INNER JOIN classrooms c ON c.id = sch.classroom_id
        WHERE sch.classroom_id = CAST(:cid AS uuid)
          AND c.academic_year_id = CAST(:ay AS uuid)
    """
    params["ay"] = str(effective_ay)
    if active_only:
        sql += """
          AND EXISTS (
            SELECT 1 FROM assessment_school ash
            WHERE ash.assessment_id = a.id AND ash.school_id = c.school_id AND ash.active IS TRUE
          )
        """
    count_row = await fetch_one(db, f"SELECT COUNT(*)::int AS total FROM ({sql}) q", params)
    items = await fetch_all(
        db,
        f"{sql} ORDER BY a.created_at DESC NULLS LAST LIMIT {pg.per_page} OFFSET {pg.offset}",
        params,
    )
    return paged_response_with_academic_year(
        academic_year_id=effective_ay,
        page=pg.page,
        per_page=pg.per_page,
        total=(count_row or {}).get("total", 0),
        items=items,
    )


@router.get("/classrooms/{classroom_id}/assessment-schedules")
async def list_classroom_assessment_schedules_v1(
    classroom_id: UUID,
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
    assessment_id: UUID | None = Query(None),
    academic_year_id: UUID | None = Query(None, description="Ano letivo. Se omitido, usa is_primary=true."),
    pg: Annotated[PageArgs, Depends(pagination_params)] = PageArgs(1, 50),
):
    effective_ay = await resolve_academic_year_id(db, academic_year_id)
    await _fetch_classroom_scoped(db, ctx, classroom_id, academic_year_id)
    params: dict[str, Any] = {"cid": str(classroom_id), "ay": str(effective_ay)}
    sql = """
        SELECT
          sch.*,
          a.title AS _assessment_title,
          c.name AS _classroom_name,
          g.name AS _grade_name
        FROM assessment_schedules sch
        INNER JOIN assessments a ON a.id = sch.assessment_id
        INNER JOIN classrooms c ON c.id = sch.classroom_id
        INNER JOIN grades g ON g.id = c.grade_id
        WHERE sch.classroom_id = CAST(:cid AS uuid)
          AND c.academic_year_id = CAST(:ay AS uuid)
    """
    if assessment_id:
        sql += " AND sch.assessment_id = CAST(:aid AS uuid)"
        params["aid"] = str(assessment_id)
    count_row = await fetch_one(db, f"SELECT COUNT(*)::int AS total FROM ({sql}) q", params)
    items_raw = await fetch_all(
        db,
        f"{sql} ORDER BY sch.start_time DESC NULLS LAST LIMIT {pg.per_page} OFFSET {pg.offset}",
        params,
    )
    items = []
    for raw in items_raw:
        row = dict(raw)
        cname = row.pop("_classroom_name", None)
        gname = row.pop("_grade_name", None)
        atitle = row.pop("_assessment_title", None)
        cid = row.get("classroom_id")
        aid = row.get("assessment_id")
        row["classrooms"] = {
            "id": str(cid) if cid is not None else None,
            "name": cname,
            "grades": ({"name": gname} if gname is not None else None),
        }
        if atitle is not None or aid is not None:
            row["assessments"] = {
                "id": str(aid) if aid is not None else None,
                "title": atitle,
            }
        items.append(row)
    return paged_response_with_academic_year(
        academic_year_id=effective_ay,
        page=pg.page,
        per_page=pg.per_page,
        total=(count_row or {}).get("total", 0),
        items=items,
    )


# --- Filters ---


@router.get("/filters/segments")
async def filters_segments_v1(
    _: Annotated[AuthContext, Depends(get_auth_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    return await fetch_all(db, "SELECT * FROM segments ORDER BY name", {})


@router.get("/filters/grades")
async def filters_grades_v1(
    _: Annotated[AuthContext, Depends(get_auth_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
    segment_id: UUID | None = None,
):
    sql = "SELECT * FROM grades WHERE 1=1"
    params: dict[str, Any] = {}
    if segment_id:
        sql += " AND segment_id = CAST(:seg AS uuid)"
        params["seg"] = str(segment_id)
    sql += " ORDER BY name"
    return await fetch_all(db, sql, params)


@router.get("/filters/academic-years")
async def filters_academic_years_v1(
    _: Annotated[AuthContext, Depends(get_auth_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
    active_only: bool = Query(
        False,
        description="Se true, retorna apenas anos com is_active=true (para seletor global na UI).",
    ),
):
    sql = "SELECT * FROM academic_years WHERE 1=1"
    params: dict[str, Any] = {}
    if active_only:
        sql += " AND is_active = true"
    sql += " ORDER BY year DESC"
    return await fetch_all(db, sql, params)


@router.get("/filters/assessment-type-labels")
async def filters_assessment_type_labels_v1(
    _: Annotated[AuthContext, Depends(get_auth_context)],
):
    """Rótulos amigáveis para `assessments.type` / `vw_student_assessment_sumarize.type` (UI admin analítico)."""
    return {
        "labels": {
            "diagnostic": "Diagnóstico",
            "diagnostico": "Diagnóstico",
            "summative": "Somativa",
            "sumativa": "Somativa",
            "summative_assessment": "Avaliação somativa",
            "formative": "Formativa",
            "formativa": "Formativa",
            "simulation": "Simulação",
            "simulacao": "Simulação",
            "practice": "Prática",
            "pratica": "Prática",
            "homework": "Tarefa de casa",
            "quiz": "Questionário",
            "exam": "Prova",
            "prova": "Prova",
            "unknown": "Outro",
        },
        "default_label": "Avaliação",
    }


# --- Catálogo pedagógico (CRUD admin) ---


async def _clear_other_primary_academic_years(db: AsyncSession, keep_id: UUID | None = None) -> None:
    sql = "UPDATE academic_years SET is_primary = false WHERE is_primary = true"
    params: dict[str, Any] = {}
    if keep_id is not None:
        sql += " AND id <> CAST(:keep_id AS uuid)"
        params["keep_id"] = str(keep_id)
    await execute(db, sql, params)


class AcademicYearCreate(BaseModel):
    year: int = Field(..., ge=2000, le=2100)
    start_date: date | None = None
    end_date: date | None = None
    is_active: bool = True
    is_primary: bool = False


class AcademicYearPatch(BaseModel):
    year: int | None = Field(None, ge=2000, le=2100)
    start_date: date | None = None
    end_date: date | None = None
    is_active: bool | None = None
    is_primary: bool | None = None


@router.post("/academic-years")
async def create_academic_year_v1(
    body: AcademicYearCreate,
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    if not is_admin_like(ctx.role):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Apenas administradores")
    if body.is_primary:
        await _clear_other_primary_academic_years(db)
    row = await fetch_one(
        db,
        """
        INSERT INTO academic_years (year, start_date, end_date, is_active, is_primary)
        VALUES (:year, :start_date, :end_date, :is_active, :is_primary)
        RETURNING *
        """,
        {
            "year": body.year,
            "start_date": body.start_date,
            "end_date": body.end_date,
            "is_active": body.is_active,
            "is_primary": body.is_primary,
        },
    )
    await db.commit()
    return row


@router.patch("/academic-years/{academic_year_id}")
async def patch_academic_year_v1(
    academic_year_id: UUID,
    body: AcademicYearPatch,
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    if not is_admin_like(ctx.role):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Apenas administradores")
    existing = await fetch_one(
        db,
        "SELECT * FROM academic_years WHERE id = CAST(:id AS uuid)",
        {"id": str(academic_year_id)},
    )
    if not existing:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Ano letivo não encontrado")
    if body.is_primary:
        await _clear_other_primary_academic_years(db, keep_id=academic_year_id)
    sets: list[str] = []
    params: dict[str, Any] = {"id": str(academic_year_id)}
    if body.year is not None:
        sets.append("year = :year")
        params["year"] = body.year
    if body.start_date is not None:
        sets.append("start_date = :start_date")
        params["start_date"] = body.start_date
    if body.end_date is not None:
        sets.append("end_date = :end_date")
        params["end_date"] = body.end_date
    if body.is_active is not None:
        sets.append("is_active = :is_active")
        params["is_active"] = body.is_active
    if body.is_primary is not None:
        sets.append("is_primary = :is_primary")
        params["is_primary"] = body.is_primary
    if not sets:
        return existing
    row = await fetch_one(
        db,
        f"UPDATE academic_years SET {', '.join(sets)} WHERE id = CAST(:id AS uuid) RETURNING *",
        params,
    )
    await db.commit()
    return row


@router.delete("/academic-years/{academic_year_id}")
async def delete_academic_year_v1(
    academic_year_id: UUID,
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    if not is_admin_like(ctx.role):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Apenas administradores")
    existing = await fetch_one(
        db,
        "SELECT id FROM academic_years WHERE id = CAST(:id AS uuid)",
        {"id": str(academic_year_id)},
    )
    if not existing:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Ano letivo não encontrado")
    in_use = await fetch_one(
        db,
        "SELECT 1 AS x FROM classrooms WHERE academic_year_id = CAST(:id AS uuid) LIMIT 1",
        {"id": str(academic_year_id)},
    )
    if in_use:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Ano letivo com turmas vinculadas; remova ou mova as turmas antes.",
        )
    await execute(db, "DELETE FROM academic_years WHERE id = CAST(:id AS uuid)", {"id": str(academic_year_id)})
    await db.commit()
    return {"ok": True}


class SegmentCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)


class SegmentPatch(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=255)


@router.post("/segments")
async def create_segment_v1(
    body: SegmentCreate,
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    if not is_admin_like(ctx.role):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Apenas administradores")
    row = await fetch_one(
        db,
        "INSERT INTO segments (name) VALUES (:name) RETURNING *",
        {"name": body.name.strip()},
    )
    await db.commit()
    return row


@router.patch("/segments/{segment_id}")
async def patch_segment_v1(
    segment_id: UUID,
    body: SegmentPatch,
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    if not is_admin_like(ctx.role):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Apenas administradores")
    if body.name is None:
        row = await fetch_one(
            db,
            "SELECT * FROM segments WHERE id = CAST(:id AS uuid)",
            {"id": str(segment_id)},
        )
        if not row:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Segmento não encontrado")
        return row
    row = await fetch_one(
        db,
        "UPDATE segments SET name = :name WHERE id = CAST(:id AS uuid) RETURNING *",
        {"id": str(segment_id), "name": body.name.strip()},
    )
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Segmento não encontrado")
    await db.commit()
    return row


@router.delete("/segments/{segment_id}")
async def delete_segment_v1(
    segment_id: UUID,
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    if not is_admin_like(ctx.role):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Apenas administradores")
    existing = await fetch_one(
        db,
        "SELECT id FROM segments WHERE id = CAST(:id AS uuid)",
        {"id": str(segment_id)},
    )
    if not existing:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Segmento não encontrado")
    in_use = await fetch_one(
        db,
        """
        SELECT 1 AS x
        FROM classrooms c
        JOIN grades g ON g.id = c.grade_id
        WHERE g.segment_id = CAST(:id AS uuid)
        LIMIT 1
        """,
        {"id": str(segment_id)},
    )
    if in_use:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Segmento com séries/turmas vinculadas; remova as turmas antes.",
        )
    await execute(db, "DELETE FROM segments WHERE id = CAST(:id AS uuid)", {"id": str(segment_id)})
    await db.commit()
    return {"ok": True}


class GradeCreate(BaseModel):
    segment_id: UUID
    name: str = Field(..., min_length=1, max_length=255)


class GradePatch(BaseModel):
    segment_id: UUID | None = None
    name: str | None = Field(None, min_length=1, max_length=255)


@router.post("/grades")
async def create_grade_v1(
    body: GradeCreate,
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    if not is_admin_like(ctx.role):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Apenas administradores")
    segment = await fetch_one(
        db,
        "SELECT id FROM segments WHERE id = CAST(:id AS uuid)",
        {"id": str(body.segment_id)},
    )
    if not segment:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Segmento não encontrado")
    row = await fetch_one(
        db,
        """
        INSERT INTO grades (segment_id, name)
        VALUES (CAST(:segment_id AS uuid), :name)
        RETURNING *
        """,
        {"segment_id": str(body.segment_id), "name": body.name.strip()},
    )
    await db.commit()
    return row


@router.patch("/grades/{grade_id}")
async def patch_grade_v1(
    grade_id: UUID,
    body: GradePatch,
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    if not is_admin_like(ctx.role):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Apenas administradores")
    if body.segment_id is not None:
        segment = await fetch_one(
            db,
            "SELECT id FROM segments WHERE id = CAST(:id AS uuid)",
            {"id": str(body.segment_id)},
        )
        if not segment:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Segmento não encontrado")
    sets: list[str] = []
    params: dict[str, Any] = {"id": str(grade_id)}
    if body.name is not None:
        sets.append("name = :name")
        params["name"] = body.name.strip()
    if body.segment_id is not None:
        sets.append("segment_id = CAST(:segment_id AS uuid)")
        params["segment_id"] = str(body.segment_id)
    if not sets:
        row = await fetch_one(
            db,
            "SELECT * FROM grades WHERE id = CAST(:id AS uuid)",
            {"id": str(grade_id)},
        )
        if not row:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Série não encontrada")
        return row
    row = await fetch_one(
        db,
        f"UPDATE grades SET {', '.join(sets)} WHERE id = CAST(:id AS uuid) RETURNING *",
        params,
    )
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Série não encontrada")
    await db.commit()
    return row


@router.delete("/grades/{grade_id}")
async def delete_grade_v1(
    grade_id: UUID,
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    if not is_admin_like(ctx.role):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Apenas administradores")
    existing = await fetch_one(
        db,
        "SELECT id FROM grades WHERE id = CAST(:id AS uuid)",
        {"id": str(grade_id)},
    )
    if not existing:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Série não encontrada")
    in_use = await fetch_one(
        db,
        "SELECT 1 AS x FROM classrooms WHERE grade_id = CAST(:id AS uuid) LIMIT 1",
        {"id": str(grade_id)},
    )
    if in_use:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Série com turmas vinculadas; remova ou mova as turmas antes.",
        )
    await execute(db, "DELETE FROM grades WHERE id = CAST(:id AS uuid)", {"id": str(grade_id)})
    await db.commit()
    return {"ok": True}
