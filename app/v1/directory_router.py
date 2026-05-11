"""Usuários, alunos e professores (Etapa 4)."""

import hashlib
import json
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from firebase_admin import auth as firebase_auth
from pydantic import BaseModel, Field, model_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import AuthContext, get_auth_context
from app.core.firebase import ensure_firebase_initialized
from app.db.session import get_db
from app.v1._academic_year import resolve_academic_year_id
from app.v1._paging import PageArgs, paged_response, pagination_params
from app.v1._scope import (
    get_descendant_school_ids,
    get_effective_classroom_scope,
    get_effective_school_scope,
    is_admin_like,
    is_staff_admin_role,
)
from app.v1._sql import execute, fetch_all, fetch_one

router = APIRouter(tags=["v1-directory"])

_ALLOWED_USER_ROLE_FILTERS = frozenset(
    {"STUDENT", "TEACHER", "SCHOOL_ADMIN", "PLATFORM_ADMIN"}
)


def _normalize_role_filter_param(role: str | None) -> str | None:
    """Converte query `role` (ex.: STUDENT) para rótulo [user_role] ou None."""
    if role is None:
        return None
    r = str(role).strip().upper().replace("-", "_")
    if not r:
        return None
    if r in ("ALUNO", "ESTUDANTE"):
        r = "STUDENT"
    if r == "PROFESSOR":
        r = "TEACHER"
    if r not in _ALLOWED_USER_ROLE_FILTERS:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Papel inválido para filtro: {role!r}",
        )
    return r


async def _student_scope_read(db: AsyncSession, ctx: AuthContext, student_id: UUID) -> dict[str, Any]:
    row = await fetch_one(db, "SELECT * FROM vw_profiles WHERE id = CAST(:id AS uuid)", {"id": str(student_id)})
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Student not found")
    if is_admin_like(ctx.role):
        return row
    if str(row.get("person_id")) == str(ctx.person_id):
        return row
    sscope = await get_effective_school_scope(db, ctx)
    if str(row.get("school_id")) in {str(x) for x in (sscope["effective_school_ids"] or [])}:
        return row
    raise HTTPException(status.HTTP_403_FORBIDDEN, "Fora do escopo")


def _assert_admin(ctx: AuthContext) -> None:
    """Operações de diretório (users/students/teachers/import): staff administrativo.

    Ver [is_staff_admin_role]: JWT `platform_admin` / `school_admin` sem tratar como
    [is_admin_like] global em `get_effective_school_scope`.
    """
    if not is_staff_admin_role(ctx.role):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Apenas administradores")


class UserCreateBody(BaseModel):
    full_name: str
    email: str
    role: str | None = None
    school_id: UUID | None = None
    phone: str | None = None
    document: str | None = None
    birthdate: str | None = None
    metadata: str | None = None
    create_firebase_user: bool = True
    temporary_password: str | None = None


class UserPatchBody(BaseModel):
    full_name: str | None = None
    phone: str | None = None
    document: str | None = None
    birthdate: str | None = None
    metadata: str | None = None
    role: str | None = None
    school_id: UUID | None = None
    neurotypical: bool | None = None
    neurotypical_description: str | None = None


async def _create_user_with_profile(
    db: AsyncSession,
    *,
    body: UserCreateBody,
) -> dict:
    if not body.role:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "role é obrigatório")
    people_row = await fetch_one(
        db,
        """
        INSERT INTO people (
            id, status, full_name, email, phone, document, birthdate, metadata,
            auth_provider, can_login, date_created
        )
        VALUES (
            gen_random_uuid(), 'published', :full_name, :email, :phone, :document,
            CAST(:birthdate AS date), :metadata, 'firebase', true, now()
        )
        RETURNING id
        """,
        {
            "full_name": body.full_name,
            "email": body.email,
            "phone": body.phone,
            "document": body.document,
            "birthdate": body.birthdate,
            "metadata": body.metadata,
        },
    )
    assert people_row is not None
    person_id = people_row["id"]

    firebase_uid = None
    if body.create_firebase_user:
        try:
            ensure_firebase_initialized()
            kwargs = {"email": body.email, "display_name": body.full_name}
            if body.temporary_password:
                kwargs["password"] = body.temporary_password
            u = firebase_auth.create_user(**kwargs)
            firebase_uid = u.uid
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status.HTTP_409_CONFLICT, f"Falha ao criar usuário no Firebase: {exc!s}") from exc

    if firebase_uid:
        await execute(
            db,
            """
            UPDATE people
            SET firebase_uid = :uid, last_sync_at = now(), sync_error = NULL
            WHERE id = CAST(:pid AS uuid)
            """,
            {"uid": firebase_uid, "pid": str(person_id)},
        )

    profile_row = await fetch_one(
        db,
        """
        INSERT INTO profiles (id, role, school_id, code, person_id)
        VALUES (gen_random_uuid(), CAST(:role AS user_role), CAST(:school_id AS uuid), :code, CAST(:person_id AS uuid))
        RETURNING id
        """,
        {
            "role": body.role,
            "school_id": str(body.school_id) if body.school_id else None,
            "code": None,
            "person_id": str(person_id),
        },
    )
    if not profile_row:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Falha ao criar profile")

    out = await fetch_one(
        db,
        "SELECT * FROM vw_profiles WHERE id = CAST(:id AS uuid)",
        {"id": str(profile_row["id"])},
    )
    await db.commit()
    if not out:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Falha ao carregar usuário criado")
    return out


@router.get("/users")
async def list_users_v1(
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
    school_id: UUID | None = None,
    role: str | None = Query(
        None,
        description="Filtrar por papel (STUDENT, TEACHER, SCHOOL_ADMIN, PLATFORM_ADMIN)",
    ),
    pg: Annotated[PageArgs, Depends(pagination_params)] = PageArgs(1, 50),
):
    """Lista consolidada via vw_profiles (admin: opcional school_id e role; demais: escopo escola/self)."""
    role_filter = _normalize_role_filter_param(role)
    sscope = await get_effective_school_scope(db, ctx)
    if is_admin_like(ctx.role):
        sql = "SELECT * FROM vw_profiles WHERE 1=1"
        params: dict[str, Any] = {}
        if school_id:
            subtree = await get_descendant_school_ids(db, school_id)
            sql += " AND school_id = ANY(CAST(:subtree AS uuid[]))"
            params["subtree"] = [str(x) for x in subtree]
        if role_filter is not None:
            sql += " AND role = CAST(:role_filter AS user_role)"
            params["role_filter"] = role_filter
        count_row = await fetch_one(db, f"SELECT COUNT(*)::int AS total FROM ({sql}) q", params)
        items = await fetch_all(
            db,
            f"{sql} ORDER BY created_at NULLS LAST LIMIT {pg.per_page} OFFSET {pg.offset}",
            params,
        )
        return paged_response(page=pg.page, per_page=pg.per_page, total=(count_row or {}).get("total", 0), items=items)
    sql = "SELECT * FROM vw_profiles WHERE 1=1"
    params: dict[str, Any] = {}
    if sscope["effective_school_ids"]:
        sql += " AND (school_id = ANY(CAST(:sids AS uuid[])) OR person_id = CAST(:pid AS uuid))"
        params["sids"] = [str(x) for x in sscope["effective_school_ids"]]
        params["pid"] = str(ctx.person_id)
    else:
        sql += " AND person_id = CAST(:pid AS uuid)"
        params["pid"] = str(ctx.person_id)
    if role_filter is not None:
        sql += " AND role = CAST(:role_filter AS user_role)"
        params["role_filter"] = role_filter
    count_row = await fetch_one(db, f"SELECT COUNT(*)::int AS total FROM ({sql}) q", params)
    items = await fetch_all(
        db,
        f"{sql} ORDER BY created_at NULLS LAST LIMIT {pg.per_page} OFFSET {pg.offset}",
        params,
    )
    return paged_response(page=pg.page, per_page=pg.per_page, total=(count_row or {}).get("total", 0), items=items)


@router.post("/users")
async def create_user_v1(
    body: UserCreateBody,
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    _assert_admin(ctx)
    return await _create_user_with_profile(db, body=body)


async def _assert_user_row_readable(
    db: AsyncSession,
    ctx: AuthContext,
    row: dict[str, Any],
) -> None:
    """Alinha com a listagem [list_users_v1]: admin global, staff por escola, ou própria pessoa."""
    if str(row.get("person_id")) == str(ctx.person_id):
        return
    if is_admin_like(ctx.role):
        return
    if not is_staff_admin_role(ctx.role):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Fora do escopo")
    sscope = await get_effective_school_scope(db, ctx)
    eff = {str(x) for x in (sscope.get("effective_school_ids") or [])}
    sid = row.get("school_id")
    if sid is not None and str(sid) in eff:
        return
    raise HTTPException(status.HTTP_403_FORBIDDEN, "Fora do escopo")


@router.get("/users/{user_id}")
async def get_user_v1(
    user_id: UUID,
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    row = await fetch_one(db, "SELECT * FROM vw_profiles WHERE id = CAST(:id AS uuid)", {"id": str(user_id)})
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User/profile not found")
    await _assert_user_row_readable(db, ctx, row)
    return row


@router.patch("/users/{user_id}")
async def patch_user_v1(
    user_id: UUID,
    body: UserPatchBody,
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    _assert_admin(ctx)
    row = await fetch_one(db, "SELECT * FROM vw_profiles WHERE id = CAST(:id AS uuid)", {"id": str(user_id)})
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User/profile not found")
    sets_people = []
    p_params: dict[str, Any] = {"pid": str(row["person_id"])}
    if body.full_name is not None:
        sets_people.append("full_name = :full_name")
        p_params["full_name"] = body.full_name
    if body.phone is not None:
        sets_people.append("phone = :phone")
        p_params["phone"] = body.phone
    if body.document is not None:
        sets_people.append('"document" = :document')
        p_params["document"] = body.document
    if body.birthdate is not None:
        sets_people.append("birthdate = CAST(:birthdate AS date)")
        p_params["birthdate"] = body.birthdate
    if body.metadata is not None:
        sets_people.append("metadata = :metadata")
        p_params["metadata"] = body.metadata
    if body.neurotypical is not None:
        sets_people.append("neurotypical = CAST(:neurotypical AS boolean)")
        p_params["neurotypical"] = body.neurotypical
    if body.neurotypical_description is not None:
        sets_people.append("neurotypical_description = :neurotypical_description")
        p_params["neurotypical_description"] = body.neurotypical_description
    if sets_people:
        await execute(
            db,
            f"UPDATE people SET {', '.join(sets_people)}, date_updated = now() WHERE id = CAST(:pid AS uuid)",
            p_params,
        )
    sets_profile = []
    pr_params: dict[str, Any] = {"id": str(user_id)}
    if body.role is not None:
        sets_profile.append("role = CAST(:role AS user_role)")
        pr_params["role"] = body.role
    if body.school_id is not None:
        sets_profile.append("school_id = CAST(:school_id AS uuid)")
        pr_params["school_id"] = str(body.school_id)
    if sets_profile:
        await execute(db, f"UPDATE profiles SET {', '.join(sets_profile)}, updated_at = now() WHERE id = CAST(:id AS uuid)", pr_params)
    await db.commit()
    out = await fetch_one(db, "SELECT * FROM vw_profiles WHERE id = CAST(:id AS uuid)", {"id": str(user_id)})
    if not out:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Falha ao recarregar usuário")
    return out


@router.delete("/users/{user_id}")
async def delete_user_v1(
    user_id: UUID,
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    _assert_admin(ctx)
    row = await fetch_one(
        db,
        "SELECT id, person_id, firebase_uid FROM vw_profiles WHERE id = CAST(:id AS uuid)",
        {"id": str(user_id)},
    )
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User/profile not found")
    await execute(db, "DELETE FROM profiles WHERE id = CAST(:id AS uuid)", {"id": str(user_id)})
    still = await fetch_one(
        db,
        "SELECT 1 FROM profiles WHERE person_id = CAST(:pid AS uuid) LIMIT 1",
        {"pid": str(row["person_id"])},
    )
    if not still:
        await execute(db, "DELETE FROM people WHERE id = CAST(:pid AS uuid)", {"pid": str(row["person_id"])})
        if row.get("firebase_uid"):
            try:
                ensure_firebase_initialized()
                firebase_auth.delete_user(str(row["firebase_uid"]))
            except Exception:  # noqa: BLE001
                pass
    await db.commit()
    return {"ok": True}


class EmailPatch(BaseModel):
    email: str = Field(..., min_length=3)


@router.patch("/users/{user_id}/email")
async def patch_user_email_v1(
    user_id: UUID,
    body: EmailPatch,
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    _assert_admin(ctx)
    row = await fetch_one(
        db,
        "SELECT person_id, firebase_uid FROM vw_profiles WHERE id = CAST(:id AS uuid)",
        {"id": str(user_id)},
    )
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User/profile not found")
    await execute(
        db,
        "UPDATE people SET email = :email, date_updated = now() WHERE id = CAST(:pid AS uuid)",
        {"email": body.email, "pid": str(row["person_id"])},
    )
    if row.get("firebase_uid"):
        try:
            ensure_firebase_initialized()
            firebase_auth.update_user(str(row["firebase_uid"]), email=body.email)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status.HTTP_409_CONFLICT, f"Falha ao atualizar e-mail no Firebase: {exc!s}") from exc
    await db.commit()
    return {"ok": True, "email": body.email}


class PasswordResetBody(BaseModel):
    new_password: str | None = Field(default=None, description="Se omitido, gera fluxo de reset Firebase.")


@router.post("/users/{user_id}/password-reset")
async def post_user_password_reset_v1(
    user_id: UUID,
    body: PasswordResetBody,
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    _assert_admin(ctx)
    row = await fetch_one(
        db,
        "SELECT email, firebase_uid FROM vw_profiles WHERE id = CAST(:id AS uuid)",
        {"id": str(user_id)},
    )
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User/profile not found")
    if not row.get("firebase_uid"):
        raise HTTPException(status.HTTP_409_CONFLICT, "Usuário sem firebase_uid")
    ensure_firebase_initialized()
    if body.new_password:
        firebase_auth.update_user(str(row["firebase_uid"]), password=body.new_password)
        return {"ok": True, "mode": "set_password"}
    link = firebase_auth.generate_password_reset_link(str(row["email"]))
    return {"ok": True, "mode": "reset_link", "reset_link": link}


def _norm_role_str(role: str | None) -> str:
    return (role or "").strip().upper()


def _parse_uuid_opt(val: Any) -> UUID | None:
    if val is None:
        return None
    s = str(val).strip()
    if not s:
        return None
    return UUID(s)


def _dict_csv_row_to_user_create(row: dict[str, Any]) -> UserCreateBody:
    """Converte linha CSV do app Flutter em [UserCreateBody] (POST /v1/users/import rows=…)."""
    fn = str(row.get("full_name") or "").strip()
    em = str(row.get("email") or "").strip()
    role_raw = str(row.get("role") or "").strip()
    if not fn:
        raise ValueError("full_name é obrigatório")
    if not em:
        raise ValueError("email é obrigatório")
    if not role_raw:
        raise ValueError("role é obrigatório")
    role_u = _norm_role_str(role_raw)
    school_raw = row.get("school_id")
    school_uuid: UUID | None
    if role_u in ("PLATFORM_ADMIN", "PLATFORMADMIN"):
        school_uuid = None
    else:
        if school_raw is None or str(school_raw).strip() == "":
            raise ValueError("school_id é obrigatório para este papel")
        school_uuid = _parse_uuid_opt(school_raw)
    pwd = row.get("password")
    pwd_s = str(pwd).strip() if pwd is not None else ""
    return UserCreateBody(
        full_name=fn,
        email=em,
        role=role_raw,
        school_id=school_uuid,
        temporary_password=pwd_s if pwd_s else None,
        create_firebase_user=True,
    )


async def _link_import_profile_to_classroom(
    db: AsyncSession,
    *,
    role: str | None,
    profile_id: UUID,
    classroom_id: UUID,
) -> None:
    role_u = _norm_role_str(role or "")
    pid = str(profile_id)
    cid = str(classroom_id)
    if "STUDENT" in role_u or role_u in ("ALUNO", "ESTUDANTE"):
        await execute(
            db,
            """
            INSERT INTO classroom_students (classroom_id, student_id, enrollment_code)
            VALUES (CAST(:cid AS uuid), CAST(:sid AS uuid), NULL)
            ON CONFLICT ON CONSTRAINT classroom_students_classroom_id_student_id_key DO NOTHING
            """,
            {"cid": cid, "sid": pid},
        )
        return
    if role_u in ("PLATFORM_ADMIN", "PLATFORMADMIN"):
        return
    await execute(
        db,
        """
        INSERT INTO classroom_teachers (classroom_id, teacher_id)
        VALUES (CAST(:cid AS uuid), CAST(:tid AS uuid))
        ON CONFLICT ON CONSTRAINT classroom_teachers_classroom_id_teacher_id_key DO NOTHING
        """,
        {"cid": cid, "tid": pid},
    )


class ImportUsersRequest(BaseModel):
    """Import em lote: `users` (contrato tipado) **ou** `rows` (mapas estilo CSV do app)."""

    users: list[UserCreateBody] | None = None
    rows: list[dict[str, Any]] | None = None
    continue_on_error: bool = True
    skip_existing_email: bool = True
    idempotency_key: str | None = None

    @model_validator(mode="after")
    def _exactly_one_source(self) -> "ImportUsersRequest":
        has_u = bool(self.users)
        has_r = bool(self.rows)
        if has_u == has_r:
            raise ValueError("Informe exatamente um de: users, rows (lista não vazia).")
        return self


@router.post("/users/import")
async def import_users_v1(
    payload: ImportUsersRequest,
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    _assert_admin(ctx)
    bodies: list[tuple[int, UserCreateBody, dict[str, Any]]] = []
    errors: list[dict[str, Any]] = []
    if payload.rows is not None:
        for i, raw in enumerate(payload.rows):
            try:
                bodies.append((i, _dict_csv_row_to_user_create(raw), raw))
            except ValueError as ve:
                errors.append({"index": i, "email": raw.get("email"), "error": str(ve)})
                if not payload.continue_on_error:
                    break
        total_in = len(payload.rows)
    else:
        users = payload.users or []
        for i, u in enumerate(users):
            bodies.append((i, u, {}))
        total_in = len(users)

    await execute(
        db,
        """
        CREATE TABLE IF NOT EXISTS app_import_jobs (
          id text PRIMARY KEY,
          payload_hash text NOT NULL,
          result_json jsonb NOT NULL,
          created_at timestamptz NOT NULL DEFAULT now()
        )
        """,
        {},
    )
    if payload.rows is not None:
        normalized_payload = json.dumps(payload.rows, sort_keys=True, default=str, ensure_ascii=True)
    else:
        normalized_payload = json.dumps(
            [u.model_dump(mode="json") for u in (payload.users or [])],
            sort_keys=True,
            ensure_ascii=True,
        )
    payload_hash = hashlib.sha256(normalized_payload.encode("utf-8")).hexdigest()
    if payload.idempotency_key:
        existing = await fetch_one(
            db,
            "SELECT payload_hash, result_json FROM app_import_jobs WHERE id = :id",
            {"id": payload.idempotency_key},
        )
        if existing:
            if existing["payload_hash"] != payload_hash:
                raise HTTPException(status.HTTP_409_CONFLICT, "idempotency_key reutilizada com payload diferente")
            return {
                **existing["result_json"],
                "idempotency": {"reused": True, "idempotency_key": payload.idempotency_key},
            }

    created: list[dict[str, Any]] = []
    seen_emails: set[str] = set()
    for i, u, orig in bodies:
        email_key = (u.email or "").strip().lower()
        if not email_key:
            errors.append({"index": i, "email": u.email, "error": "email obrigatório"})
            if not payload.continue_on_error:
                break
            continue
        if email_key in seen_emails:
            errors.append({"index": i, "email": u.email, "error": "email duplicado no payload"})
            if not payload.continue_on_error:
                break
            continue
        seen_emails.add(email_key)
        if payload.skip_existing_email:
            exists = await fetch_one(
                db,
                "SELECT id FROM people WHERE lower(email) = :email LIMIT 1",
                {"email": email_key},
            )
            if exists:
                created.append({"index": i, "profile_id": None, "email": u.email, "skipped": "existing_email"})
                continue
        try:
            out = await _create_user_with_profile(db, body=u)
            pid = UUID(str(out["id"]))
            entry: dict[str, Any] = {"index": i, "profile_id": out["id"], "email": out.get("email")}
            class_id = _parse_uuid_opt(orig.get("classroom_id")) if orig else None
            if class_id:
                try:
                    await _link_import_profile_to_classroom(
                        db, role=u.role, profile_id=pid, classroom_id=class_id
                    )
                    await db.commit()
                    entry["classroom_linked"] = True
                except Exception as link_exc:  # noqa: BLE001
                    await db.rollback()
                    errors.append(
                        {
                            "index": i,
                            "email": u.email,
                            "error": f"Usuário criado; falha ao vincular turma: {link_exc!s}",
                        }
                    )
            created.append(entry)
        except Exception as exc:  # noqa: BLE001
            await db.rollback()
            errors.append({"index": i, "email": u.email, "error": str(exc)})
            if not payload.continue_on_error:
                break
    result = {
        "ok": len(errors) == 0,
        "created": created,
        "errors": errors,
        "summary": {
            "received": total_in,
            "created_count": len([x for x in created if not x.get("skipped")]),
            "skipped_count": len([x for x in created if x.get("skipped")]),
            "error_count": len(errors),
            "linked_count": len([x for x in created if x.get("classroom_linked")]),
        },
    }
    if payload.idempotency_key:
        await execute(
            db,
            """
            INSERT INTO app_import_jobs (id, payload_hash, result_json)
            VALUES (:id, :payload_hash, CAST(:result_json AS jsonb))
            ON CONFLICT (id) DO NOTHING
            """,
            {
                "id": payload.idempotency_key,
                "payload_hash": payload_hash,
                "result_json": json.dumps(result, ensure_ascii=True),
            },
        )
    await db.commit()
    return {
        **result,
        "idempotency": {
            "reused": False,
            "idempotency_key": payload.idempotency_key,
            "payload_hash": payload_hash,
        },
    }


@router.get("/users/import-jobs/{idempotency_key}")
async def get_import_job_v1(
    idempotency_key: str,
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Base para processamento assíncrono futuro: consulta status/resultado por idempotency key."""
    _assert_admin(ctx)
    row = await fetch_one(
        db,
        """
        SELECT id, payload_hash, result_json, created_at
        FROM app_import_jobs
        WHERE id = :id
        """,
        {"id": idempotency_key},
    )
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Import job não encontrado")
    return {
        "idempotency_key": row["id"],
        "payload_hash": row["payload_hash"],
        "result": row["result_json"],
        "created_at": row["created_at"],
    }


@router.get("/students")
async def list_students_v1(
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
    school_id: UUID | None = None,
    pg: Annotated[PageArgs, Depends(pagination_params)] = PageArgs(1, 50),
):
    sscope = await get_effective_school_scope(db, ctx)
    sql = "SELECT * FROM vw_profiles WHERE role::text ILIKE '%student%'"
    params: dict[str, Any] = {}
    if school_id:
        if not is_admin_like(ctx.role):
            if str(school_id) not in {str(x) for x in (sscope["effective_school_ids"] or [])}:
                raise HTTPException(status.HTTP_403_FORBIDDEN, "Escola fora do escopo")
            sql += " AND school_id = CAST(:sid AS uuid)"
            params["sid"] = str(school_id)
        else:
            subtree = await get_descendant_school_ids(db, school_id)
            sql += " AND school_id = ANY(CAST(:subtree AS uuid[]))"
            params["subtree"] = [str(x) for x in subtree]
    elif not is_admin_like(ctx.role):
        sql += " AND school_id = ANY(CAST(:sids AS uuid[]))"
        params["sids"] = [str(x) for x in (sscope["effective_school_ids"] or [])]
    count_row = await fetch_one(db, f"SELECT COUNT(*)::int AS total FROM ({sql}) q", params)
    items = await fetch_all(
        db,
        f"{sql} ORDER BY created_at NULLS LAST LIMIT {pg.per_page} OFFSET {pg.offset}",
        params,
    )
    return paged_response(page=pg.page, per_page=pg.per_page, total=(count_row or {}).get("total", 0), items=items)


@router.post("/students")
async def create_student_v1(
    body: UserCreateBody,
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    _assert_admin(ctx)
    b = body.model_copy(update={"role": body.role or "student"})
    return await _create_user_with_profile(db, body=b)


@router.get("/students/{student_id}")
async def get_student_v1(
    student_id: UUID,
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    return await _student_scope_read(db, ctx, student_id)


@router.get("/students/{student_id}/classrooms")
async def list_student_classrooms_v1(
    student_id: UUID,
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
    academic_year_id: UUID | None = Query(None, description="Ano letivo. Se omitido, usa is_primary=true."),
    pg: Annotated[PageArgs, Depends(pagination_params)] = PageArgs(1, 50),
):
    row = await _student_scope_read(db, ctx, student_id)
    if "student" not in str(row.get("role") or "").lower():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Student not found")
    effective_ay = await resolve_academic_year_id(db, academic_year_id)
    params: dict[str, Any] = {"sid": str(student_id), "ay": str(effective_ay)}
    sql = """
        SELECT c.*
        FROM classrooms c
        INNER JOIN classroom_students cs ON cs.classroom_id = c.id AND cs.student_id = CAST(:sid AS uuid)
        WHERE c.academic_year_id = CAST(:ay AS uuid)
    """
    if not is_admin_like(ctx.role):
        cscope = await get_effective_classroom_scope(db, ctx)
        cids = cscope["effective_classroom_ids"] or []
        if not cids:
            return paged_response(page=pg.page, per_page=pg.per_page, total=0, items=[])
        sql += " AND cs.classroom_id = ANY(CAST(:cids AS uuid[]))"
        params["cids"] = [str(x) for x in cids]
    count_row = await fetch_one(db, f"SELECT COUNT(*)::int AS total FROM ({sql}) q", params)
    items = await fetch_all(
        db,
        f"{sql} ORDER BY c.name NULLS LAST LIMIT {pg.per_page} OFFSET {pg.offset}",
        params,
    )
    return paged_response(page=pg.page, per_page=pg.per_page, total=(count_row or {}).get("total", 0), items=items)


@router.patch("/students/{student_id}")
async def patch_student_v1(
    student_id: UUID,
    body: UserPatchBody,
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    return await patch_user_v1(student_id, body, ctx, db)


@router.delete("/students/{student_id}")
async def delete_student_v1(
    student_id: UUID,
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    return await delete_user_v1(student_id, ctx, db)


@router.get("/teachers")
async def list_teachers_v1(
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
    school_id: UUID | None = None,
    pg: Annotated[PageArgs, Depends(pagination_params)] = PageArgs(1, 50),
):
    sscope = await get_effective_school_scope(db, ctx)
    sql = "SELECT * FROM vw_profiles WHERE role::text ILIKE '%teacher%'"
    params: dict[str, Any] = {}
    if school_id:
        if not is_admin_like(ctx.role):
            if str(school_id) not in {str(x) for x in (sscope["effective_school_ids"] or [])}:
                raise HTTPException(status.HTTP_403_FORBIDDEN, "Escola fora do escopo")
            sql += " AND school_id = CAST(:sid AS uuid)"
            params["sid"] = str(school_id)
        else:
            subtree = await get_descendant_school_ids(db, school_id)
            sql += " AND school_id = ANY(CAST(:subtree AS uuid[]))"
            params["subtree"] = [str(x) for x in subtree]
    elif not is_admin_like(ctx.role):
        sql += " AND school_id = ANY(CAST(:sids AS uuid[]))"
        params["sids"] = [str(x) for x in (sscope["effective_school_ids"] or [])]
    count_row = await fetch_one(db, f"SELECT COUNT(*)::int AS total FROM ({sql}) q", params)
    items = await fetch_all(
        db,
        f"{sql} ORDER BY created_at NULLS LAST LIMIT {pg.per_page} OFFSET {pg.offset}",
        params,
    )
    return paged_response(page=pg.page, per_page=pg.per_page, total=(count_row or {}).get("total", 0), items=items)


@router.post("/teachers")
async def create_teacher_v1(
    body: UserCreateBody,
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    _assert_admin(ctx)
    b = body.model_copy(update={"role": body.role or "teacher"})
    return await _create_user_with_profile(db, body=b)


@router.get("/teachers/{teacher_id}")
async def get_teacher_v1(
    teacher_id: UUID,
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    row = await fetch_one(db, "SELECT * FROM vw_profiles WHERE id = CAST(:id AS uuid)", {"id": str(teacher_id)})
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Teacher not found")
    if is_admin_like(ctx.role):
        return row
    if str(teacher_id) == str(ctx.active_profile_id):
        return row
    sscope = await get_effective_school_scope(db, ctx)
    if str(row.get("school_id")) in {str(x) for x in (sscope["effective_school_ids"] or [])}:
        return row
    raise HTTPException(status.HTTP_403_FORBIDDEN, "Fora do escopo")


@router.get("/teachers/{teacher_id}/students")
async def list_teacher_students_v1(
    teacher_id: UUID,
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
    q: str | None = Query(None, description="Busca em nome, e-mail ou código"),
    classroom_id: UUID | None = Query(None),
    academic_year_id: UUID | None = Query(None, description="Ano letivo. Se omitido, usa is_primary=true."),
    pg: Annotated[PageArgs, Depends(pagination_params)] = PageArgs(1, 50),
):
    await get_teacher_v1(teacher_id, ctx, db)
    effective_ay = await resolve_academic_year_id(db, academic_year_id)
    params: dict[str, Any] = {"tid": str(teacher_id), "ay": str(effective_ay)}
    where_parts = [
        "v.teacher_id = CAST(:tid AS uuid)",
        "c.academic_year_id = CAST(:ay AS uuid)",
    ]
    if not is_admin_like(ctx.role):
        cscope = await get_effective_classroom_scope(db, ctx)
        scope_cids = cscope["effective_classroom_ids"] or []
        if not scope_cids:
            return paged_response(page=pg.page, per_page=pg.per_page, total=0, items=[])
        where_parts.append("v.classroom_id = ANY(CAST(:scope_cids AS uuid[]))")
        params["scope_cids"] = [str(x) for x in scope_cids]
    if classroom_id:
        where_parts.append("v.classroom_id = CAST(:cid AS uuid)")
        params["cid"] = str(classroom_id)
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
        INNER JOIN classrooms c ON c.id = v.classroom_id
        WHERE {where_sql}
        ORDER BY v.student_id, v.classroom_id
    """
    count_row = await fetch_one(db, f"SELECT COUNT(*)::int AS total FROM ({base}) _x", params)
    total = (count_row or {}).get("total", 0)
    items = await fetch_all(
        db,
        f"{base} LIMIT {pg.per_page} OFFSET {pg.offset}",
        params,
    )
    return paged_response(page=pg.page, per_page=pg.per_page, total=total, items=items)


@router.patch("/teachers/{teacher_id}")
async def patch_teacher_v1(
    teacher_id: UUID,
    body: UserPatchBody,
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    return await patch_user_v1(teacher_id, body, ctx, db)


@router.delete("/teachers/{teacher_id}")
async def delete_teacher_v1(
    teacher_id: UUID,
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    return await delete_user_v1(teacher_id, ctx, db)


@router.get("/teachers/eligible-for-classroom")
async def teachers_eligible_v1(
    classroom_id: UUID,
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Substitui school_self_and_ancestors com CTE recursiva de ancestrais da escola da turma."""
    c = await fetch_one(
        db,
        "SELECT school_id FROM classrooms WHERE id = CAST(:id AS uuid)",
        {"id": str(classroom_id)},
    )
    if not c:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Classroom not found")
    sid = c["school_id"]
    sscope = await get_effective_school_scope(db, ctx)
    if not sscope["is_admin_like"] and str(sid) not in {str(x) for x in (sscope["effective_school_ids"] or [])}:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Fora do escopo")
    return await fetch_all(
        db,
        """
        WITH RECURSIVE ancestors AS (
          SELECT id, parent FROM schools WHERE id = CAST(:sid AS uuid)
          UNION ALL
          SELECT s.id, s.parent FROM schools s
          JOIN ancestors a ON a.parent = s.id
        )
        SELECT vp.*
        FROM vw_profiles vp
        WHERE vp.role::text ILIKE '%teacher%'
          AND vp.school_id IN (SELECT id FROM ancestors)
        """,
        {"sid": str(sid)},
    )
