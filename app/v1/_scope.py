"""Escopo efetivo por perfil.

Centraliza:
- effective_school_ids: escola do perfil ativo + descendentes (CTE recursiva)
- effective_classroom_ids: por papel — admin sem limite; professor = turmas em `my_classrooms`
  (vínculo `classroom_teachers`); demais = turmas em escolas efetivas + vínculos explícitos
  `classroom_teachers` / `classroom_students`.
"""

from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import AuthContext
from app.v1._sql import fetch_all


def is_admin_like(role: str) -> bool:
    r = _norm_role(role)
    return r in (
        "admin",
        "administrator",
        "super_admin",
        "coordenador_master",
        "platform_admin",
    )


def _norm_role(role: str) -> str:
    return (role or "").lower().replace("-", "_")


def is_staff_admin_role(role: str) -> bool:
    """Grosseiro: pode aceder a rotas só staff (KPI admin, diretório, …).

    Diferente de [is_admin_like]: `school_admin` não é «admin global» para SQL sem
    filtro de escola — [get_effective_school_scope] continua a aplicar hierarquia.
    """
    if is_admin_like(role):
        return True
    return _norm_role(role) in ("platform_admin", "school_admin")


async def resolve_admin_dashboard_school_ids(
    db: AsyncSession,
    ctx: AuthContext,
    school_id: UUID | None,
) -> list[UUID] | None:
    """Subárvore de escolas para KPI/engagement admin.

    - `None`: sem filtro (todas as escolas) — só legacy admin / `platform_admin`.
    - Lista: raiz pedida + descendentes (intersecta com o escopo do `school_admin`).
    """
    if not is_staff_admin_role(ctx.role):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Apenas administradores")
    r = _norm_role(ctx.role)
    if is_admin_like(ctx.role) or r == "platform_admin":
        if school_id is None:
            return None
        return await get_descendant_school_ids(db, school_id)
    if r == "school_admin":
        scope = await get_effective_school_scope(db, ctx)
        allowed = list(scope.get("effective_school_ids") or [])
        if not allowed:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                "Sem escola associada ao perfil",
            )
        allowed_set = set(allowed)
        root = scope.get("direct_school_id") or allowed[0]
        effective_root = school_id if school_id is not None else root
        if effective_root not in allowed_set:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Escola fora do escopo")
        return await get_descendant_school_ids(db, effective_root)
    raise HTTPException(status.HTTP_403_FORBIDDEN, "Apenas administradores")


def is_teacher_like(role: str) -> bool:
    r = (role or "").lower()
    return "teacher" in r or "prof" in r


def resolve_bot_audience(role: str) -> str:
    """Papel normalizado para filtro de intenções do Avaliador."""
    r = _norm_role(role)
    if is_admin_like(role):
        return "platform_admin"
    if r == "school_admin":
        return "school_admin"
    if "student" in r or r == "aluno":
        return "student"
    if is_teacher_like(role):
        return "teacher"
    return "teacher"


def can_use_assistant(role: str) -> bool:
    """Professor ou admin de plataforma (incl. platform_admin via is_admin_like)."""
    return is_teacher_like(role) or is_admin_like(role)


def school_filter_sql(ctx: AuthContext, column: str = "school_id") -> tuple[str, dict[str, UUID]]:
    """Retorna fragmento SQL AND + params; admin sem filtro retorna ('', {})."""
    if is_admin_like(ctx.role) or _norm_role(ctx.role) == "platform_admin":
        return "", {}
    if ctx.school_id is None:
        return f" AND {column} IS NULL", {}
    return f" AND {column} = :_scope_school", {"_scope_school": ctx.school_id}


async def get_descendant_school_ids(db, school_id: UUID) -> list[UUID]:
    rows = await fetch_all(
        db,
        """
        WITH RECURSIVE descendants AS (
          SELECT id, parent
          FROM schools
          WHERE id = CAST(:sid AS uuid)
          UNION ALL
          SELECT s.id, s.parent
          FROM schools s
          JOIN descendants d ON s.parent = d.id
        )
        SELECT id FROM descendants
        """,
        {"sid": str(school_id)},
    )
    return [r["id"] for r in rows]


async def get_effective_school_scope(db, ctx: AuthContext) -> dict:
    """Escopo efetivo de escolas para o active_profile atual.

    Conjunto = **a escola ligada ao perfil** (`profiles.school_id`) **mais** todas as escolas
    cujo `parent` sobe até essa escola (subárvore para baixo). O CTE em
    `get_descendant_school_ids` já ancora no nó do perfil; abaixo reforçamos que o próprio
    `school_id` nunca fique de fora (defesa contra regressões / dados estranhos).

    A chave `is_admin_like` significa «sem teto de escola nesta consulta» — inclui
    `platform_admin` (JWT do app), além dos papéis legado em [is_admin_like].
    """
    if is_admin_like(ctx.role) or _norm_role(ctx.role) == "platform_admin":
        return {"effective_school_ids": None, "direct_school_id": None, "is_admin_like": True}
    if ctx.school_id is None:
        return {"effective_school_ids": [], "direct_school_id": None, "is_admin_like": False}
    ids = await get_descendant_school_ids(db, ctx.school_id)
    root = ctx.school_id
    seen: set[UUID] = set()
    ordered: list[UUID] = []
    for u in (root, *ids):
        if u not in seen:
            seen.add(u)
            ordered.append(u)
    return {
        "effective_school_ids": ordered,
        "direct_school_id": ctx.school_id,
        "is_admin_like": False,
    }


async def get_effective_classroom_scope(db, ctx: AuthContext) -> dict:
    """Escopo efetivo de turmas.

    - Admin: sem restrição.
    - Demais: turmas de escolas efetivas + turmas vinculadas explicitamente ao profile ativo.
    """
    s = await get_effective_school_scope(db, ctx)
    if s["is_admin_like"]:
        return {"effective_classroom_ids": None, **s}

    school_ids: list[UUID] = s["effective_school_ids"] or []

    # Regra específica de professor:
    # Todas as turmas em que o perfil ativo está vinculado como professor (`classroom_teachers`),
    # expostas via `my_classrooms` — **sem** recortar por `profiles.school_id` / hierarquia de escolas.
    # O vínculo na turma é a fonte de verdade do acesso (alinhado a `vw_teacher_classroom_options`).
    if is_teacher_like(ctx.role):
        params = {"pid": str(ctx.active_profile_id)}
        teacher_sql = """
        SELECT classroom_id
        FROM my_classrooms
        WHERE teacher_id = CAST(:pid AS uuid)
        """
        teacher_rows = await fetch_all(db, teacher_sql, params)
        teacher_classrooms = [r["classroom_id"] for r in teacher_rows]
        return {
            **s,
            "effective_classroom_ids": teacher_classrooms,
            "explicit_classroom_ids": teacher_classrooms,
        }

    explicit_rows = await fetch_all(
        db,
        """
        SELECT classroom_id
        FROM classroom_teachers
        WHERE teacher_id = CAST(:pid AS uuid)
        UNION
        SELECT classroom_id
        FROM classroom_students
        WHERE student_id = CAST(:pid AS uuid)
        """,
        {"pid": str(ctx.active_profile_id)},
    )
    explicit = [r["classroom_id"] for r in explicit_rows]

    school_classrooms = []
    if school_ids:
        school_classrooms_rows = await fetch_all(
            db,
            """
            SELECT id AS classroom_id
            FROM classrooms
            WHERE school_id = ANY(CAST(:school_ids AS uuid[]))
            """,
            {"school_ids": [str(x) for x in school_ids]},
        )
        school_classrooms = [r["classroom_id"] for r in school_classrooms_rows]

    unique = []
    seen = set()
    for cid in school_classrooms + explicit:
        k = str(cid)
        if k not in seen:
            seen.add(k)
            unique.append(cid)

    return {
        **s,
        "effective_classroom_ids": unique,
        "explicit_classroom_ids": explicit,
    }
