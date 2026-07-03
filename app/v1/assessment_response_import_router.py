"""Importação administrativa de respostas TRI (ADMIN)."""

from __future__ import annotations

import asyncio
import json
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Query, UploadFile, status
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import AuthContext, get_auth_context
from app.db.session import get_db
from app.v1._scope import is_admin_like
from app.v1._sql import execute, fetch_all, fetch_one
from app.v1.assessment_response_import_parser import MAX_FILE_BYTES, parse_tabular_bytes
from app.v1.assessment_response_import_service import (
    assert_import_schema_ready,
    batch_status_payload,
    build_log_csv,
    mapping_key,
    normalize_caderno,
    normalize_codigo_cartao,
    resolve_cartao_suggestions,
    run_import_background,
    run_validation_background,
    start_import_batch,
    validation_summary_dict,
)

router = APIRouter(prefix="/admin/assessment-response-imports", tags=["v1-assessment-import"])


def _assert_platform_admin(ctx: AuthContext) -> None:
    if not is_admin_like(ctx.role):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Apenas administradores da plataforma")


def _summary_dict(summary: Any) -> dict[str, Any]:
    return validation_summary_dict(summary)


def _detect_caderno_keys(
    rows: list[dict[str, str]],
    suggestions: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    counts: dict[str, dict[str, Any]] = {}
    suggestions = suggestions or {}
    for row in rows:
        caderno = normalize_caderno(row.get("caderno"))
        ano_raw = row.get("ano_detectado") or row.get("serie") or ""
        try:
            ano = int(str(ano_raw).strip())
        except ValueError:
            ano = None
        if not caderno:
            continue
        key = mapping_key(ano, caderno) if ano else caderno.lower()
        if key not in counts:
            counts[key] = {
                "key": key,
                "ano": ano,
                "caderno": caderno,
                "row_count": 0,
                "suggested_assessment_id": suggestions.get(key),
            }
        counts[key]["row_count"] += 1
    return sorted(counts.values(), key=lambda x: (x.get("ano") or 0, x.get("caderno") or ""))


@router.post("/parse")
async def parse_assessment_response_import_v1(
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
    file: UploadFile = File(...),
):
    """Lê CSV/XLSX e retorna colunas/cadernos detectados (sem gravar no banco)."""
    _assert_platform_admin(ctx)

    filename = file.filename or "upload.csv"
    content = await file.read()
    if len(content) > MAX_FILE_BYTES:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "Arquivo excede 10 MB")
    if not filename.lower().endswith((".csv", ".xlsx")):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Extensão permitida: .csv ou .xlsx")

    headers, rows, extras = parse_tabular_bytes(content, filename)

    suggestions: dict[str, str] = {}
    view_row = await fetch_one(
        db,
        "SELECT to_regclass('public.vw_attendance_info') AS attendance_view",
        {},
    )
    if view_row and view_row.get("attendance_view"):
        suggestions = await resolve_cartao_suggestions(db, rows)

    return {
        "filename": filename,
        "headers": headers,
        "total_rows": len(rows),
        "cadernos": _detect_caderno_keys(rows, suggestions),
        "unprocessed_columns": extras,
        "sample_rows": rows[:5],
    }


def _json_safe_row(row: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in row.items():
        if isinstance(value, UUID):
            out[key] = str(value)
        elif isinstance(value, (list, dict)):
            out[key] = value
        else:
            out[key] = value
    return out


@router.post("/validate")
async def validate_assessment_response_import_v1(
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    caderno_mapping: str = Form("{}"),
):
    """Inicia validação em background; use GET /{batch_id} para acompanhar progresso."""
    _assert_platform_admin(ctx)
    await assert_import_schema_ready(db)

    filename = file.filename or "upload.csv"
    content = await file.read()
    if len(content) > MAX_FILE_BYTES:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "Arquivo excede 10 MB")

    try:
        mapping_raw = json.loads(caderno_mapping or "{}")
        if not isinstance(mapping_raw, dict):
            raise ValueError("caderno_mapping deve ser objeto JSON")
    except json.JSONDecodeError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "caderno_mapping JSON inválido") from exc
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    headers, rows, extras = parse_tabular_bytes(content, filename)

    batch = await fetch_one(
        db,
        """
        INSERT INTO assessment_response_import_batch (
          original_filename, status, uploaded_by, total_rows, metadata
        ) VALUES (
          :fname, 'pending', CAST(:uid AS uuid), :total,
          CAST(:meta AS jsonb)
        )
        RETURNING id
        """,
        {
            "fname": filename,
            "uid": str(ctx.active_profile_id),
            "total": len(rows),
            "meta": json.dumps(
                {
                    "headers": headers,
                    "caderno_mapping": mapping_raw,
                    "unprocessed_columns": extras,
                    "validating": True,
                    "processed_rows": 0,
                    "total_rows": len(rows),
                },
                ensure_ascii=False,
            ),
        },
    )
    if not batch:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Falha ao criar lote")
    batch_id = batch["id"]
    await db.commit()

    background_tasks.add_task(
        run_validation_background,
        batch_id,
        rows,
        mapping_raw,
        extras,
    )

    return {
        "batch_id": str(batch_id),
        "status": "pending",
        "validating": True,
        "total_rows": len(rows),
        "processed_rows": 0,
        "can_import": False,
        "summary": {},
        "rows": [],
    }


@router.get("")
async def list_assessment_response_imports_v1(
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    status_filter: str | None = Query(None, alias="status"),
):
    """Lista lotes de importação (mais recentes primeiro)."""
    _assert_platform_admin(ctx)
    offset = (page - 1) * per_page
    params: dict[str, Any] = {"lim": per_page, "off": offset}
    where = ""
    if status_filter:
        where = "WHERE b.status = :status"
        params["status"] = status_filter

    count_row = await fetch_one(
        db,
        f"SELECT COUNT(*)::int AS total FROM assessment_response_import_batch b {where}",
        params,
    )
    total = int((count_row or {}).get("total") or 0)

    rows = await fetch_all(
        db,
        f"""
        SELECT b.id, b.original_filename, b.status, b.total_rows, b.valid_rows,
               b.invalid_rows, b.imported_responses, b.updated_responses,
               b.imported_proficiencies, b.error_count, b.created_at,
               b.validated_at, b.imported_at, b.uploaded_by,
               COALESCE(p.full_name, '') AS uploaded_by_name
        FROM assessment_response_import_batch b
        LEFT JOIN vw_profiles p ON p.id = b.uploaded_by
        {where}
        ORDER BY b.created_at DESC
        LIMIT :lim OFFSET :off
        """,
        params,
    )
    items = [
        {
            "batch_id": str(r["id"]),
            "original_filename": r.get("original_filename"),
            "status": r.get("status"),
            "total_rows": int(r.get("total_rows") or 0),
            "valid_rows": int(r.get("valid_rows") or 0),
            "invalid_rows": int(r.get("invalid_rows") or 0),
            "imported_responses": int(r.get("imported_responses") or 0),
            "updated_responses": int(r.get("updated_responses") or 0),
            "imported_proficiencies": int(r.get("imported_proficiencies") or 0),
            "error_count": int(r.get("error_count") or 0),
            "uploaded_by": str(r["uploaded_by"]) if r.get("uploaded_by") else None,
            "uploaded_by_name": r.get("uploaded_by_name") or None,
            "created_at": str(r["created_at"]) if r.get("created_at") else None,
            "validated_at": str(r["validated_at"]) if r.get("validated_at") else None,
            "imported_at": str(r["imported_at"]) if r.get("imported_at") else None,
        }
        for r in rows
    ]
    return {"items": items, "total": total, "page": page, "per_page": per_page}


@router.delete("/{batch_id}")
async def delete_assessment_response_import_v1(
    batch_id: UUID,
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Remove lote e logs (respostas já importadas no banco não são revertidas)."""
    _assert_platform_admin(ctx)
    row = await fetch_one(
        db,
        "SELECT id, status FROM assessment_response_import_batch WHERE id = CAST(:id AS uuid)",
        {"id": str(batch_id)},
    )
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Lote não encontrado")
    if row.get("status") in ("importing", "pending"):
        meta = await fetch_one(
            db,
            "SELECT metadata FROM assessment_response_import_batch WHERE id = CAST(:id AS uuid)",
            {"id": str(batch_id)},
        )
        md = meta.get("metadata") if meta else {}
        if isinstance(md, str):
            try:
                md = json.loads(md)
            except json.JSONDecodeError:
                md = {}
        if row.get("status") == "importing" or md.get("validating"):
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "Lote em processamento; aguarde a conclusão antes de excluir",
            )

    await execute(
        db,
        "DELETE FROM assessment_response_import_batch WHERE id = CAST(:id AS uuid)",
        {"id": str(batch_id)},
    )
    await db.commit()
    return {"batch_id": str(batch_id), "deleted": True}


@router.post("/{batch_id}/commit")
async def commit_assessment_response_import_v1(
    batch_id: UUID,
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    _assert_platform_admin(ctx)
    await assert_import_schema_ready(db)
    payload = await start_import_batch(db, batch_id)
    await db.commit()
    asyncio.create_task(run_import_background(batch_id))
    return payload


@router.get("/{batch_id}")
async def get_assessment_response_import_batch_v1(
    batch_id: UUID,
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
    include_rows: bool = Query(False, alias="include_rows"),
    rows_limit: int = Query(500, ge=1, le=2000),
):
    _assert_platform_admin(ctx)
    row = await fetch_one(
        db,
        "SELECT * FROM assessment_response_import_batch WHERE id = CAST(:id AS uuid)",
        {"id": str(batch_id)},
    )
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Lote não encontrado")
    payload = batch_status_payload(row)
    if include_rows and payload["status"] in ("validated", "validation_failed", "imported"):
        log_rows = await fetch_all(
            db,
            """
            SELECT row_number, status, codigo_cartao, ra, student_name, student_id,
                   assessment_id, caderno, ano_detectado, errors, warnings
            FROM assessment_response_import_row_log
            WHERE batch_id = CAST(:bid AS uuid)
            ORDER BY row_number
            LIMIT :lim
            """,
            {"bid": str(batch_id), "lim": rows_limit},
        )
        payload["rows"] = [_json_safe_row(r) for r in log_rows]
    return payload


@router.get("/{batch_id}/logs")
async def list_assessment_response_import_logs_v1(
    batch_id: UUID,
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
    status_filter: str | None = Query(None, alias="status"),
    limit: int = Query(500, ge=1, le=2000),
):
    _assert_platform_admin(ctx)
    sql = """
        SELECT row_number, status, codigo_cartao, ra, student_name, student_id,
               assessment_id, caderno, ano_detectado, errors, warnings,
               answers_summary, proficiency_summary, created_at
        FROM assessment_response_import_row_log
        WHERE batch_id = CAST(:bid AS uuid)
    """
    params: dict[str, Any] = {"bid": str(batch_id), "lim": limit}
    if status_filter:
        sql += " AND status = :status"
        params["status"] = status_filter
    sql += " ORDER BY row_number LIMIT :lim"
    items = await fetch_all(db, sql, params)
    return {"batch_id": str(batch_id), "items": items, "total": len(items)}


@router.get("/{batch_id}/download-log")
async def download_assessment_response_import_log_v1(
    batch_id: UUID,
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    _assert_platform_admin(ctx)
    batch = await fetch_one(
        db,
        "SELECT original_filename FROM assessment_response_import_batch WHERE id = CAST(:id AS uuid)",
        {"id": str(batch_id)},
    )
    if not batch:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Lote não encontrado")
    rows = await fetch_all(
        db,
        """
        SELECT * FROM assessment_response_import_row_log
        WHERE batch_id = CAST(:bid AS uuid)
        ORDER BY row_number
        """,
        {"bid": str(batch_id)},
    )
    csv_bytes = build_log_csv(rows)
    base = str(batch["original_filename"]).rsplit(".", 1)[0]
    fname = f"{base}-import-log.csv"
    return Response(
        content=csv_bytes,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )
