"""Validação e commit de importação de respostas/proficiências TRI."""

from __future__ import annotations

import csv
import io
import json
import logging
import re
import time
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Awaitable, Callable
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.v1._sql import execute, execute_many, fetch_all, fetch_one

YEAR_QUESTION_LIMITS: dict[int, int] = {2: 25, 5: 45, 9: 60}
VALIDATION_CHUNK_SIZE = 500
VALIDATION_PROGRESS_EVERY = 250
IMPORT_CHUNK_SIZE = 50
_IMPORT_JOBS: dict[str, float] = {}
_IMPORT_JOB_STALE_SEC = 7200
logger = logging.getLogger(__name__)
AREA_LP = "linguagens"
AREA_MT = "matematica"
VALID_LABELS = frozenset({"A", "B", "C", "D"})


def normalize_codigo_cartao(value: str | None) -> str:
    return re.sub(r"\s+", "", str(value or "")).upper()


def normalize_ra(value: str | None) -> str:
    return re.sub(r"\s+", "", str(value or ""))


def ras_equivalent(a: str | None, b: str | None) -> bool:
    """Compara RA por texto exato ou valor inteiro (quando ambos são só dígitos)."""
    left = normalize_ra(a)
    right = normalize_ra(b)
    if not left or not right:
        return False
    if left == right:
        return True
    if left.isdigit() and right.isdigit():
        return int(left) == int(right)
    return False


def _index_ra_rows(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = str(row.get("lookup_key") or "")
        if not key:
            continue
        index[key] = row
        if key.isdigit():
            index[str(int(key))] = row
    return index


def normalize_caderno(value: str | None) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())


def mapping_key(ano: int, caderno: str) -> str:
    return f"{ano}|{normalize_caderno(caderno).lower()}"


def _chunked(items: list[Any], size: int):
    for i in range(0, len(items), size):
        yield items[i : i + size]


_BLANK_ANSWERS = frozenset({"NA", "N/A", "N.A.", "-", "*", "—"})


def parse_answer(raw: str | None) -> tuple[str | None, bool]:
    """Retorna (label válido ou None, is_invalid)."""
    if raw is None:
        return None, False
    text = str(raw).strip().upper()
    if not text or text in _BLANK_ANSWERS:
        return None, False
    if text in VALID_LABELS:
        return text, False
    return None, True


def parse_proficiency(raw: str | None) -> tuple[float | None, str | None]:
    if raw is None:
        return None, None
    text = str(raw).strip().replace(",", ".")
    if not text:
        return None, None
    try:
        val = float(Decimal(text))
    except (InvalidOperation, ValueError):
        return None, "Proficiência inválida"
    return val, None


def question_count_for_year(ano: int) -> int | None:
    return YEAR_QUESTION_LIMITS.get(ano)


def parse_grade_year(value: str | int | None) -> int | None:
    """Extrai ano/série (2, 5 ou 9) de grade_name ou valor numérico."""
    if value is None:
        return None
    if isinstance(value, int):
        return value if value in YEAR_QUESTION_LIMITS else None
    text = str(value).strip()
    if not text:
        return None
    if text.isdigit():
        year = int(text)
        return year if year in YEAR_QUESTION_LIMITS else None
    match = re.search(r"\b([259])\b", text)
    if match:
        return int(match.group(1))
    lead = re.match(r"^(\d+)", text)
    if lead:
        year = int(lead.group(1))
        return year if year in YEAR_QUESTION_LIMITS else None
    return None


@dataclass
class ValidationSummary:
    total_rows: int = 0
    found_by_cartao: int = 0
    found_by_ra: int = 0
    students_not_found: int = 0
    cartao_ra_mismatch: int = 0
    cadernos_identified: int = 0
    questions_mapped: int = 0
    questions_unmapped: int = 0
    valid_answers: int = 0
    blank_answers: int = 0
    invalid_answers: int = 0
    alternatives_not_found: int = 0
    prof_lp_valid: int = 0
    prof_mt_valid: int = 0
    rows_ready: int = 0
    rows_blocked: int = 0
    unprocessed_columns: list[str] = field(default_factory=list)
    caderno_stats: dict[str, int] = field(default_factory=dict)
    detected_years: list[int] = field(default_factory=list)
    detected_cadernos: list[str] = field(default_factory=list)


def validation_summary_dict(summary: ValidationSummary) -> dict[str, Any]:
    return {
        "total_rows": summary.total_rows,
        "found_by_cartao": summary.found_by_cartao,
        "found_by_ra": summary.found_by_ra,
        "students_not_found": summary.students_not_found,
        "cartao_ra_mismatch": summary.cartao_ra_mismatch,
        "cadernos_identified": summary.cadernos_identified,
        "questions_mapped": summary.questions_mapped,
        "questions_unmapped": summary.questions_unmapped,
        "valid_answers": summary.valid_answers,
        "blank_answers": summary.blank_answers,
        "invalid_answers": summary.invalid_answers,
        "alternatives_not_found": summary.alternatives_not_found,
        "prof_lp_valid": summary.prof_lp_valid,
        "prof_mt_valid": summary.prof_mt_valid,
        "rows_ready": summary.rows_ready,
        "rows_blocked": summary.rows_blocked,
        "unprocessed_columns": summary.unprocessed_columns,
        "caderno_stats": summary.caderno_stats,
        "detected_years": summary.detected_years,
        "detected_cadernos": summary.detected_cadernos,
    }


ProgressCallback = Callable[[int, ValidationSummary], Awaitable[None]]


async def update_validation_progress(
    db: AsyncSession,
    batch_id: UUID,
    *,
    processed_rows: int,
    summary: ValidationSummary,
) -> None:
    await execute(
        db,
        """
        UPDATE assessment_response_import_batch
        SET valid_rows = :ready,
            invalid_rows = :blocked,
            metadata = COALESCE(metadata, '{}'::jsonb) || CAST(:meta AS jsonb)
        WHERE id = CAST(:id AS uuid)
        """,
        {
            "id": str(batch_id),
            "ready": summary.rows_ready,
            "blocked": summary.rows_blocked,
            "meta": json.dumps(
                {
                    "validating": True,
                    "processed_rows": processed_rows,
                    "total_rows": summary.total_rows,
                    "validation": validation_summary_dict(summary),
                },
                ensure_ascii=False,
            ),
        },
    )


async def finalize_validation_batch(
    db: AsyncSession,
    batch_id: UUID,
    summary: ValidationSummary,
) -> str:
    batch_status = "validated" if summary.rows_ready > 0 else "validation_failed"
    await execute(
        db,
        """
        UPDATE assessment_response_import_batch
        SET status = :status,
            valid_rows = :valid,
            invalid_rows = :invalid,
            error_count = :errors,
            warning_count = :warnings,
            validated_at = now(),
            metadata = COALESCE(metadata, '{}'::jsonb) || CAST(:summary AS jsonb)
        WHERE id = CAST(:id AS uuid)
        """,
        {
            "id": str(batch_id),
            "status": batch_status,
            "valid": summary.rows_ready,
            "invalid": summary.rows_blocked,
            "errors": summary.rows_blocked,
            "warnings": summary.invalid_answers + summary.alternatives_not_found,
            "summary": json.dumps(
                {
                    "validating": False,
                    "processed_rows": summary.total_rows,
                    "total_rows": summary.total_rows,
                    "can_import": summary.rows_ready > 0,
                    "validation": validation_summary_dict(summary),
                },
                ensure_ascii=False,
            ),
        },
    )
    return batch_status


async def run_validation_background(
    batch_id: UUID,
    rows: list[dict[str, str]],
    caderno_mapping: dict[str, str],
    unprocessed_columns: list[str],
) -> None:
    from app.db.session import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        try:

            async def on_progress(processed: int, summary: ValidationSummary) -> None:
                await update_validation_progress(
                    db, batch_id, processed_rows=processed, summary=summary
                )
                await db.commit()

            summary = await validate_import_batch(
                db,
                batch_id=batch_id,
                rows=rows,
                caderno_mapping=caderno_mapping,
                unprocessed_columns=unprocessed_columns,
                on_progress=on_progress,
            )
            await finalize_validation_batch(db, batch_id, summary)
            await db.commit()
        except Exception as exc:
            await db.rollback()
            async with AsyncSessionLocal() as err_db:
                await execute(
                    err_db,
                    """
                    UPDATE assessment_response_import_batch
                    SET status = 'validation_failed',
                        metadata = COALESCE(metadata, '{}'::jsonb) || CAST(:meta AS jsonb)
                    WHERE id = CAST(:id AS uuid)
                    """,
                    {
                        "id": str(batch_id),
                        "meta": json.dumps(
                            {"validating": False, "error": str(exc)},
                            ensure_ascii=False,
                        ),
                    },
                )
                await err_db.commit()


def batch_status_payload(batch: dict[str, Any]) -> dict[str, Any]:
    meta = batch.get("metadata") or {}
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except json.JSONDecodeError:
            meta = {}
    validation = meta.get("validation") or {}
    total_rows = int(meta.get("total_rows") or batch.get("total_rows") or 0)
    processed_rows = int(meta.get("processed_rows") or 0)
    validating = bool(meta.get("validating"))
    status = str(batch.get("status") or "pending")
    importing = bool(meta.get("importing")) or status == "importing"
    rows_ready = int(batch.get("valid_rows") or validation.get("rows_ready") or 0)
    rows_blocked = int(batch.get("invalid_rows") or validation.get("rows_blocked") or 0)
    can_import = bool(meta.get("can_import")) if "can_import" in meta else rows_ready > 0
    if status in ("validated", "validation_failed"):
        validating = False
        if status == "validated":
            can_import = rows_ready > 0
    if status in ("importing", "imported", "import_failed"):
        validating = False
        can_import = False
    import_stats = meta.get("import_stats") or {}
    return {
        "batch_id": str(batch.get("id")),
        "status": status,
        "total_rows": total_rows,
        "processed_rows": processed_rows,
        "valid_rows": rows_ready,
        "invalid_rows": rows_blocked,
        "validating": validating and status == "pending",
        "importing": importing and status == "importing",
        "import_processed_rows": int(meta.get("import_processed_rows") or 0),
        "import_total_rows": int(meta.get("import_total_rows") or rows_ready),
        "import_stats": import_stats,
        "can_import": can_import and status == "validated",
        "summary": validation,
        "error": meta.get("error"),
    }


async def assert_import_schema_ready(db: AsyncSession) -> None:
    """Exige migrations 020 + 021 aplicadas (sem DDL na requisição)."""
    row = await fetch_one(
        db,
        """
        SELECT to_regclass('public.assessment_response_import_batch') AS batch_tbl,
               to_regclass('public.assessment_response_import_row_log') AS log_tbl,
               to_regclass('public.vw_attendance_info') AS attendance_view
        """,
        {},
    )
    if not row or not row.get("batch_tbl") or not row.get("log_tbl"):
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Tabelas de importação não encontradas. Aplique pp-bo/migrations/020_assessment_response_import.sql",
        )
    if not row.get("attendance_view"):
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "View vw_attendance_info não encontrada. Aplique pp-bo/migrations/021_vw_attendance_info.sql",
        )


async def _bulk_lookup_by_cartao(
    db: AsyncSession, codes: list[str]
) -> dict[str, dict[str, Any]]:
    if not codes:
        return {}
    rows = await fetch_all(
        db,
        """
        SELECT v.code AS lookup_key,
               v.student_id,
               v.assessment_schedules_id AS schedule_id,
               v.assessment_id,
               v.classroom_id,
               v.school_id,
               v.macro_assessment_id,
               v.ra AS ra_code
        FROM vw_attendance_info v
        WHERE v.code = ANY(:codes)
        """,
        {"codes": codes},
    )
    index = {str(r["lookup_key"]): dict(r) for r in rows if r.get("lookup_key")}
    student_ids = sorted({str(r["student_id"]) for r in rows if r.get("student_id")})
    if student_ids:
        meta = await _bulk_student_meta(db, student_ids)
        for entry in index.values():
            sid = str(entry.get("student_id"))
            if sid in meta:
                entry.update(meta[sid])
    return index


async def _bulk_student_meta(
    db: AsyncSession, student_ids: list[str]
) -> dict[str, dict[str, Any]]:
    if not student_ids:
        return {}
    rows = await fetch_all(
        db,
        """
        SELECT p.id AS student_id,
               pe.full_name AS student_name
        FROM profiles p
        LEFT JOIN people pe ON pe.id = p.person_id
        WHERE p.id = ANY(CAST(:sids AS uuid[]))
        """,
        {"sids": student_ids},
    )
    return {str(r["student_id"]): r for r in rows if r.get("student_id")}


async def _bulk_lookup_by_ra(db: AsyncSession, ras: list[str]) -> dict[str, dict[str, Any]]:
    if not ras:
        return {}
    numeric_ras = sorted({int(ra) for ra in ras if ra.isdigit()})
    rows = await fetch_all(
        db,
        """
        SELECT DISTINCT ON (trim(p.code))
               trim(p.code) AS lookup_key,
               p.id AS student_id
        FROM profiles p
        WHERE p.role::text ILIKE '%student%'
          AND (
            trim(p.code) = ANY(:ras)
            OR (
              trim(p.code) ~ '^[0-9]+$'
              AND trim(p.code)::bigint = ANY(:numeric_ras)
            )
          )
        ORDER BY trim(p.code), p.created_at DESC NULLS LAST
        """,
        {"ras": ras, "numeric_ras": numeric_ras},
    )
    return _index_ra_rows(rows)


async def _lookup_by_cartao(db: AsyncSession, code: str) -> dict[str, Any] | None:
    if not code:
        return None
    row = await fetch_one(
        db,
        """
        SELECT v.code AS lookup_key,
               v.student_id,
               v.assessment_schedules_id AS schedule_id,
               v.assessment_id,
               v.classroom_id,
               v.school_id,
               v.macro_assessment_id,
               v.ra AS ra_code
        FROM vw_attendance_info v
        WHERE v.code = :code
        LIMIT 1
        """,
        {"code": code},
    )
    if not row:
        return None
    entry = dict(row)
    meta = await _bulk_student_meta(db, [str(entry["student_id"])])
    entry.update(meta.get(str(entry["student_id"]), {}))
    return entry


async def resolve_cartao_suggestions(
    db: AsyncSession, rows: list[dict[str, str]]
) -> dict[str, str]:
    """Resolve cartões do arquivo e sugere assessment_id por chave ano|caderno."""
    cartao_codes = sorted({normalize_codigo_cartao(r.get("codigo_cartao")) for r in rows} - {""})
    if not cartao_codes:
        return {}
    cartao_index = await _bulk_lookup_by_cartao(db, cartao_codes)
    return suggest_caderno_assessments(rows, cartao_index)


def suggest_caderno_assessments(
    rows: list[dict[str, str]],
    cartao_index: dict[str, dict[str, Any]],
) -> dict[str, str]:
    """Sugere assessment_id por chave ano|caderno a partir dos cartões resolvidos."""
    votes: dict[str, dict[str, int]] = {}
    for row in rows:
        cartao = normalize_codigo_cartao(row.get("codigo_cartao"))
        if not cartao:
            continue
        hit = cartao_index.get(cartao)
        if not hit or not hit.get("assessment_id"):
            continue
        caderno = normalize_caderno(row.get("caderno"))
        if not caderno:
            continue
        ano_raw = row.get("ano_detectado") or row.get("serie") or ""
        try:
            ano = int(str(ano_raw).strip())
        except ValueError:
            ano = 0
        key = mapping_key(ano, caderno) if ano else caderno.lower()
        aid = str(hit["assessment_id"])
        votes.setdefault(key, {})
        votes[key][aid] = votes[key].get(aid, 0) + 1
    return {
        key: max(counts, key=counts.get)
        for key, counts in votes.items()
        if counts
    }


async def _lookup_by_ra(db: AsyncSession, ra: str) -> dict[str, Any] | None:
    if not ra:
        return None
    return await fetch_one(
        db,
        """
        SELECT p.id AS student_id, p.code AS ra_code, pe.full_name
        FROM profiles p
        LEFT JOIN people pe ON pe.id = p.person_id
        WHERE trim(p.code) = :ra
          AND p.role::text ILIKE '%student%'
        ORDER BY p.created_at DESC NULLS LAST
        LIMIT 1
        """,
        {"ra": ra},
    )


async def _resolve_classroom_for_student_assessment(
    db: AsyncSession,
    *,
    student_id: UUID,
    assessment_id: UUID,
    schedule_classroom_id: UUID | None,
) -> UUID | None:
    """Fallback: turma quando não veio do cartão (ex.: linha só com RA)."""
    if schedule_classroom_id:
        return schedule_classroom_id
    row = await fetch_one(
        db,
        """
        SELECT ass.classroom_id
        FROM assessment_schedules ass
        JOIN classroom_students cs ON cs.classroom_id = ass.classroom_id
        WHERE ass.assessment_id = CAST(:aid AS uuid)
          AND cs.student_id = CAST(:sid AS uuid)
        ORDER BY ass.start_time DESC NULLS LAST
        LIMIT 1
        """,
        {"aid": str(assessment_id), "sid": str(student_id)},
    )
    if row and row.get("classroom_id"):
        return row["classroom_id"]
    row2 = await fetch_one(
        db,
        """
        SELECT cs.classroom_id
        FROM classroom_students cs
        JOIN classrooms c ON c.id = cs.classroom_id
        WHERE cs.student_id = CAST(:sid AS uuid)
        ORDER BY c.created_at DESC NULLS LAST
        LIMIT 1
        """,
        {"sid": str(student_id)},
    )
    return row2.get("classroom_id") if row2 else None


async def _load_question_map(db: AsyncSession, assessment_id: UUID) -> dict[int, UUID]:
    rows = await fetch_all(
        db,
        """
        SELECT question_id, order_index
        FROM questions_assessments
        WHERE assessment_id = CAST(:aid AS uuid)
        ORDER BY order_index
        """,
        {"aid": str(assessment_id)},
    )
    out: dict[int, UUID] = {}
    for row in rows:
        oi = row.get("order_index")
        qid = row.get("question_id")
        if oi is None or qid is None:
            continue
        out[int(oi)] = qid
    return out


async def _load_alternative_map(db: AsyncSession, question_ids: list[UUID]) -> dict[tuple[UUID, str], UUID]:
    if not question_ids:
        return {}
    rows = await fetch_all(
        db,
        """
        SELECT id, question_id, upper(trim(label)) AS label
        FROM question_alternative
        WHERE question_id = ANY(CAST(:qids AS uuid[]))
        """,
        {"qids": [str(q) for q in question_ids]},
    )
    return {(row["question_id"], row["label"]): row["id"] for row in rows if row.get("label")}


def _parse_caderno_mapping(raw: dict[str, str] | None) -> dict[str, UUID]:
    if not raw:
        return {}
    out: dict[str, UUID] = {}
    for key, val in raw.items():
        if not val:
            continue
        try:
            out[key.strip().lower()] = UUID(str(val).strip())
        except ValueError:
            continue
    return out


async def validate_import_batch(
    db: AsyncSession,
    *,
    batch_id: UUID,
    rows: list[dict[str, str]],
    caderno_mapping: dict[str, str],
    unprocessed_columns: list[str],
    on_progress: ProgressCallback | None = None,
) -> ValidationSummary:
    mapping = _parse_caderno_mapping(caderno_mapping)
    summary = ValidationSummary(
        total_rows=len(rows),
        unprocessed_columns=unprocessed_columns,
    )
    caderno_counts: dict[str, int] = {}
    years: set[int] = set()
    cadernos: set[str] = set()

    await execute(
        db,
        "DELETE FROM assessment_response_import_row_log WHERE batch_id = CAST(:bid AS uuid)",
        {"bid": str(batch_id)},
    )

    question_cache: dict[UUID, dict[int, UUID]] = {}
    alt_cache: dict[UUID, dict[tuple[UUID, str], UUID]] = {}
    classroom_cache: dict[tuple[str, str], UUID | None] = {}

    cartao_codes = sorted({normalize_codigo_cartao(r.get("codigo_cartao")) for r in rows} - {""})
    ra_codes = sorted({normalize_ra(r.get("ra")) for r in rows} - {""})
    cartao_index = await _bulk_lookup_by_cartao(db, cartao_codes)
    ra_index = await _bulk_lookup_by_ra(db, ra_codes)

    pending_logs: list[dict[str, Any]] = []

    async def _flush_logs() -> None:
        if not pending_logs:
            return
        for log in _chunked(pending_logs, 200):
            await execute_many(
                db,
                """
                INSERT INTO assessment_response_import_row_log (
                  batch_id, row_number, codigo_cartao, ra, student_name, student_id,
                  assessment_id, schedule_id, classroom_id, caderno, ano_detectado,
                  status, errors, warnings, answers_summary, proficiency_summary
                ) VALUES (
                  CAST(:bid AS uuid), :rn, :cartao, :ra, :sname,
                  CAST(:sid AS uuid), CAST(:aid AS uuid),
                  CAST(:sch AS uuid), CAST(:cid AS uuid),
                  :caderno, :ano, :status,
                  CAST(:errors AS jsonb), CAST(:warnings AS jsonb),
                  CAST(:answers AS jsonb), CAST(:prof AS jsonb)
                )
                """,
                [{"bid": str(batch_id), **entry} for entry in log],
            )
        pending_logs.clear()

    for idx, row in enumerate(rows, start=1):
        errors: list[str] = []
        warnings: list[str] = []
        answers_summary: dict[str, Any] = {}
        proficiency_summary: dict[str, Any] = {}

        cartao = normalize_codigo_cartao(row.get("codigo_cartao"))
        ra = normalize_ra(row.get("ra"))
        caderno = normalize_caderno(row.get("caderno"))
        student_name = row.get("estudante") or row.get("aluno") or ""

        ano_raw = row.get("ano_detectado") or row.get("serie") or ""
        try:
            ano = int(str(ano_raw).strip())
        except ValueError:
            ano = 0
        if ano in YEAR_QUESTION_LIMITS:
            years.add(ano)
        if caderno:
            cadernos.add(caderno)
            ck = mapping_key(ano, caderno) if ano else caderno.lower()
            caderno_counts[ck] = caderno_counts.get(ck, 0) + 1

        if not cartao and not ra:
            errors.append("Informe codigo_cartao ou RA")

        by_cartao = cartao_index.get(cartao) if cartao else None
        by_ra = ra_index.get(ra) if ra else None

        student_id: UUID | None = None
        schedule_id: UUID | None = None
        classroom_id: UUID | None = None

        if by_cartao:
            summary.found_by_cartao += 1
            student_id = by_cartao["student_id"]
            schedule_id = by_cartao.get("schedule_id")
            classroom_id = by_cartao.get("classroom_id")
            if not student_name and by_cartao.get("student_name"):
                student_name = str(by_cartao["student_name"])
            view_ra = normalize_ra(by_cartao.get("ra_code"))
            if ra and view_ra and not ras_equivalent(ra, view_ra):
                errors.append("RA do arquivo difere do cadastro do cartão")
                summary.cartao_ra_mismatch += 1
        if by_ra:
            summary.found_by_ra += 1
            if student_id is None:
                student_id = by_ra["student_id"]
            elif str(student_id) != str(by_ra["student_id"]):
                errors.append("Divergência entre codigo_cartao e RA")
                summary.cartao_ra_mismatch += 1

        if student_id is None:
            errors.append("Aluno não encontrado")
            summary.students_not_found += 1

        assessment_id: UUID | None = None
        if by_cartao and by_cartao.get("assessment_id"):
            assessment_id = by_cartao["assessment_id"]
            summary.cadernos_identified += 1
            if ano and caderno:
                mapped_id = mapping.get(mapping_key(ano, caderno))
                if mapped_id and str(mapped_id) != str(assessment_id):
                    warnings.append("Caderno mapeado difere do agendamento vinculado ao cartão")
        elif ano and caderno:
            mk = mapping_key(ano, caderno)
            assessment_id = mapping.get(mk)
            if assessment_id is None:
                errors.append(f"Caderno não mapeado: {ano}º ano · {caderno}")
            else:
                summary.cadernos_identified += 1
        elif caderno and not cartao:
            errors.append("ano_detectado inválido ou ausente")
        elif not cartao:
            errors.append("Informe codigo_cartao ou mapeie o caderno manualmente")

        limit_ano = ano if ano in YEAR_QUESTION_LIMITS else None
        if not limit_ano and by_cartao:
            limit_ano = parse_grade_year(by_cartao.get("grade_name"))
        if not limit_ano and by_ra and ra:
            limit_ano = parse_grade_year(row.get("serie"))

        q_limit = question_count_for_year(limit_ano) if limit_ano else None
        if assessment_id and q_limit:
            if assessment_id not in question_cache:
                question_cache[assessment_id] = await _load_question_map(db, assessment_id)
                qids = list(question_cache[assessment_id].values())
                alt_cache[assessment_id] = await _load_alternative_map(db, qids)
            qmap = question_cache[assessment_id]
            amap = alt_cache[assessment_id]

            for qn in range(1, q_limit + 1):
                col = f"Q{qn:03d}"
                raw_ans = row.get(col, "")
                label, is_invalid = parse_answer(raw_ans)
                qid = qmap.get(qn)
                entry: dict[str, Any] = {
                    "raw": raw_ans,
                    "label": label,
                    "is_invalid": is_invalid,
                    "question_id": str(qid) if qid else None,
                    "response_id": None,
                }
                if qid is None:
                    summary.questions_unmapped += 1
                    if not is_invalid and label:
                        errors.append(f"Questão Q{qn:03d} sem mapeamento na avaliação")
                else:
                    summary.questions_mapped += 1
                    if is_invalid:
                        summary.invalid_answers += 1
                    elif label is None:
                        summary.blank_answers += 1
                    else:
                        alt_id = amap.get((qid, label))
                        if alt_id:
                            entry["response_id"] = str(alt_id)
                            summary.valid_answers += 1
                        else:
                            summary.alternatives_not_found += 1
                            errors.append(f"Alternativa {label} não encontrada em Q{qn:03d}")
                answers_summary[col] = entry

        lp_val, lp_err = parse_proficiency(row.get("prof_lp"))
        mt_val, mt_err = parse_proficiency(row.get("prof_mt"))
        if lp_err:
            warnings.append(lp_err)
        if mt_err:
            warnings.append(mt_err)
        if lp_val is not None:
            proficiency_summary[AREA_LP] = lp_val
            summary.prof_lp_valid += 1
        if mt_val is not None:
            proficiency_summary[AREA_MT] = mt_val
            summary.prof_mt_valid += 1

        if unprocessed_columns:
            warnings.append(
                "Colunas detectadas e não processadas: " + ", ".join(unprocessed_columns)
            )

        if student_id and assessment_id:
            cache_key = (str(student_id), str(assessment_id))
            if cache_key not in classroom_cache:
                if classroom_id:
                    classroom_cache[cache_key] = classroom_id
                else:
                    classroom_cache[cache_key] = await _resolve_classroom_for_student_assessment(
                        db,
                        student_id=student_id,
                        assessment_id=assessment_id,
                        schedule_classroom_id=None,
                    )
            classroom_id = classroom_cache[cache_key]
            if classroom_id is None and (lp_val is not None or mt_val is not None):
                warnings.append("Turma não identificada; proficiência pode falhar na importação")

        status_row = "blocked" if errors else "validated"
        if status_row == "validated":
            summary.rows_ready += 1
        else:
            summary.rows_blocked += 1

        pending_logs.append(
            {
                "rn": idx,
                "cartao": cartao or None,
                "ra": ra or None,
                "sname": student_name or None,
                "sid": str(student_id) if student_id else None,
                "aid": str(assessment_id) if assessment_id else None,
                "sch": str(schedule_id) if schedule_id else None,
                "cid": str(classroom_id) if classroom_id else None,
                "caderno": caderno or None,
                "ano": limit_ano,
                "status": status_row,
                "errors": json.dumps(errors, ensure_ascii=False),
                "warnings": json.dumps(warnings, ensure_ascii=False),
                "answers": json.dumps(answers_summary, ensure_ascii=False),
                "prof": json.dumps(proficiency_summary, ensure_ascii=False),
            }
        )

        if len(pending_logs) >= VALIDATION_CHUNK_SIZE:
            await _flush_logs()
        if on_progress and idx % VALIDATION_PROGRESS_EVERY == 0:
            await on_progress(idx, summary)

    await _flush_logs()
    if on_progress:
        await on_progress(len(rows), summary)

    summary.caderno_stats = caderno_counts
    summary.detected_years = sorted(years)
    summary.detected_cadernos = sorted(cadernos)
    return summary


async def _upsert_import_proficiency(
    db: AsyncSession,
    *,
    student_id: UUID,
    assessment_id: UUID,
    classroom_id: UUID,
    area_slug: str,
    score: float,
    grade_year: int | None,
    standard_set: str = "psp_2025",
    source: str = "imported",
) -> bool:
    """Grava proficiência TRI. Retorna True se já existia (update)."""
    existed = await fetch_one(
        db,
        """
        SELECT id FROM student_assessment_area_proficiency
        WHERE student_id = CAST(:sid AS uuid)
          AND assessment_id = CAST(:aid AS uuid)
          AND area_slug = CAST(:area AS text)
        LIMIT 1
        """,
        {"sid": str(student_id), "aid": str(assessment_id), "area": area_slug},
    )

    level_code: str | None = None
    if grade_year is not None:
        level_row = await fetch_one(
            db,
            """
            SELECT fn_classify_proficiency_level(
              CAST(:area AS text),
              CAST(:gy AS smallint),
              CAST(:score AS numeric),
              CAST(:std AS varchar)
            ) AS level_code
            """,
            {
                "area": area_slug,
                "gy": grade_year,
                "score": score,
                "std": standard_set,
            },
        )
        if level_row:
            level_code = level_row.get("level_code")

    await execute(
        db,
        """
        INSERT INTO student_assessment_area_proficiency (
          student_id, assessment_id, area_slug, proficiency, level_code,
          classroom_id, standard_set, source, computed_at, updated_at
        ) VALUES (
          CAST(:sid AS uuid), CAST(:aid AS uuid), CAST(:area AS text),
          CAST(:score AS numeric), CAST(:level AS varchar), CAST(:cid AS uuid),
          CAST(:std AS varchar), CAST(:source AS varchar), now(), now()
        )
        ON CONFLICT (student_id, assessment_id, area_slug) DO UPDATE SET
          proficiency = EXCLUDED.proficiency,
          level_code = EXCLUDED.level_code,
          classroom_id = EXCLUDED.classroom_id,
          standard_set = EXCLUDED.standard_set,
          source = EXCLUDED.source,
          computed_at = now(),
          updated_at = now()
        """,
        {
            "sid": str(student_id),
            "aid": str(assessment_id),
            "area": area_slug,
            "score": score,
            "level": level_code,
            "cid": str(classroom_id),
            "std": standard_set,
            "source": source,
        },
    )
    return existed is not None


async def _upsert_import_response(
    db: AsyncSession,
    *,
    student_id: UUID,
    assessment_id: UUID,
    question_id: UUID,
    response_id: UUID | None,
    order_index: int,
    schedule_id: UUID | None,
    raw_answer: str | None,
    is_invalid: bool,
    batch_id: UUID,
) -> str:
    existing = await fetch_one(
        db,
        """
        SELECT id FROM question_student_responsed
        WHERE student_id = CAST(:sid AS uuid)
          AND assessment_id = CAST(:aid AS uuid)
          AND question_id = CAST(:qid AS uuid)
        ORDER BY updated_at DESC NULLS LAST
        LIMIT 1
        """,
        {"sid": str(student_id), "aid": str(assessment_id), "qid": str(question_id)},
    )
    params = {
        "sid": str(student_id),
        "aid": str(assessment_id),
        "qid": str(question_id),
        "rid": str(response_id) if response_id else None,
        "oidx": order_index,
        "sch": str(schedule_id) if schedule_id else None,
        "raw": raw_answer,
        "inv": is_invalid,
        "bid": str(batch_id),
    }
    if existing:
        await execute(
            db,
            """
            UPDATE question_student_responsed
            SET response_id = CAST(:rid AS uuid),
                order_index = :oidx,
                schedule_id = COALESCE(CAST(:sch AS uuid), schedule_id),
                raw_answer = :raw,
                is_invalid_answer = :inv,
                import_batch_id = CAST(:bid AS uuid),
                updated_at = now()
            WHERE id = :pk
            """,
            {**params, "pk": _as_int_pk(existing["id"])},
        )
        return "updated"
    await fetch_one(
        db,
        """
        INSERT INTO question_student_responsed (
          student_id, assessment_id, question_id, response_id, order_index,
          schedule_id, raw_answer, is_invalid_answer, import_batch_id
        ) VALUES (
          CAST(:sid AS uuid), CAST(:aid AS uuid), CAST(:qid AS uuid),
          CAST(:rid AS uuid), :oidx, CAST(:sch AS uuid),
          :raw, :inv, CAST(:bid AS uuid)
        )
        RETURNING id
        """,
        params,
    )
    return "inserted"


async def _count_pending_import_rows(db: AsyncSession, batch_id: UUID) -> int:
    row = await fetch_one(
        db,
        """
        SELECT COUNT(*)::int AS cnt
        FROM assessment_response_import_row_log
        WHERE batch_id = CAST(:bid AS uuid) AND status = 'validated'
        """,
        {"bid": str(batch_id)},
    )
    return int((row or {}).get("cnt") or 0)


async def _fetch_pending_import_chunk(
    db: AsyncSession, batch_id: UUID, *, limit: int
) -> list[dict[str, Any]]:
    return await fetch_all(
        db,
        """
        SELECT * FROM assessment_response_import_row_log
        WHERE batch_id = CAST(:bid AS uuid) AND status = 'validated'
        ORDER BY row_number
        LIMIT :lim
        """,
        {"bid": str(batch_id), "lim": limit},
    )


async def _bulk_load_response_id_maps(
    db: AsyncSession,
    pairs: list[tuple[str, str]],
) -> dict[tuple[str, str], dict[str, int]]:
    result: dict[tuple[str, str], dict[str, int]] = {pair: {} for pair in pairs}
    if not pairs:
        return result
    sids = [pair[0] for pair in pairs]
    aids = [pair[1] for pair in pairs]
    try:
        rows = await fetch_all(
            db,
            """
            SELECT DISTINCT ON (q.student_id, q.assessment_id, q.question_id)
                   q.student_id, q.assessment_id, q.question_id, q.id
            FROM question_student_responsed q
            INNER JOIN unnest(CAST(:sids AS uuid[]), CAST(:aids AS uuid[]))
              AS p(student_id, assessment_id)
              ON q.student_id = p.student_id AND q.assessment_id = p.assessment_id
            ORDER BY q.student_id, q.assessment_id, q.question_id,
                     q.updated_at DESC NULLS LAST
            """,
            {"sids": sids, "aids": aids},
        )
        for row in rows:
            key = (str(row["student_id"]), str(row["assessment_id"]))
            qid = str(row["question_id"])
            result.setdefault(key, {})[qid] = _as_int_pk(row["id"])
        return result
    except Exception as exc:
        logger.warning("bulk response lookup failed, using per-pair fallback: %s", exc)
        for sid, aid in pairs:
            result[(sid, aid)] = await _load_response_id_map(db, UUID(sid), UUID(aid))
        return result


def _try_acquire_import_job(batch_id: UUID) -> bool:
    key = str(batch_id)
    now = time.monotonic()
    started = _IMPORT_JOBS.get(key)
    if started is not None and now - started < _IMPORT_JOB_STALE_SEC:
        return False
    _IMPORT_JOBS[key] = now
    return True


def _release_import_job(batch_id: UUID) -> None:
    _IMPORT_JOBS.pop(str(batch_id), None)


def _as_int_pk(value: Any) -> int:
    if isinstance(value, int):
        return value
    return int(str(value))


async def _load_response_id_map(
    db: AsyncSession,
    student_id: UUID,
    assessment_id: UUID,
) -> dict[str, int]:
    rows = await fetch_all(
        db,
        """
        SELECT DISTINCT ON (question_id) id, question_id
        FROM question_student_responsed
        WHERE student_id = CAST(:sid AS uuid)
          AND assessment_id = CAST(:aid AS uuid)
        ORDER BY question_id, updated_at DESC NULLS LAST
        """,
        {"sid": str(student_id), "aid": str(assessment_id)},
    )
    return {str(r["question_id"]): _as_int_pk(r["id"]) for r in rows}


async def _resolve_level_code(
    db: AsyncSession,
    *,
    area_slug: str,
    grade_year: int | None,
    score: float,
    cache: dict[tuple[str, int | None, float], str | None],
    standard_set: str = "psp_2025",
) -> str | None:
    key = (area_slug, grade_year, score)
    if key in cache:
        return cache[key]
    level_code: str | None = None
    if grade_year is not None:
        level_row = await fetch_one(
            db,
            """
            SELECT fn_classify_proficiency_level(
              CAST(:area AS text),
              CAST(:gy AS smallint),
              CAST(:score AS numeric),
              CAST(:std AS varchar)
            ) AS level_code
            """,
            {
                "area": area_slug,
                "gy": grade_year,
                "score": score,
                "std": standard_set,
            },
        )
        if level_row:
            level_code = level_row.get("level_code")
    cache[key] = level_code
    return level_code


async def update_import_progress(
    db: AsyncSession,
    batch_id: UUID,
    *,
    processed_rows: int,
    total_rows: int,
    stats: dict[str, Any],
) -> None:
    await execute(
        db,
        """
        UPDATE assessment_response_import_batch
        SET metadata = COALESCE(metadata, '{}'::jsonb) || CAST(:meta AS jsonb)
        WHERE id = CAST(:id AS uuid)
        """,
        {
            "id": str(batch_id),
            "meta": json.dumps(
                {
                    "importing": True,
                    "import_processed_rows": processed_rows,
                    "import_total_rows": total_rows,
                    "import_stats": stats,
                },
                ensure_ascii=False,
            ),
        },
    )


async def _import_rows_chunk(
    db: AsyncSession,
    *,
    batch_id: UUID,
    rows: list[dict[str, Any]],
    stats: dict[str, Any],
    response_cache: dict[tuple[str, str], dict[str, int]],
    level_cache: dict[tuple[str, int | None, float], str | None],
) -> None:
    pairs_needed: set[tuple[str, str]] = set()
    for row in rows:
        sid = row.get("student_id")
        aid = row.get("assessment_id")
        if sid and aid:
            pairs_needed.add((str(sid), str(aid)))

    missing_pairs = [pair for pair in pairs_needed if pair not in response_cache]
    if missing_pairs:
        loaded = await _bulk_load_response_id_maps(db, missing_pairs)
        response_cache.update(loaded)

    inserts: list[dict[str, Any]] = []
    updates: list[dict[str, Any]] = []
    prof_params: list[dict[str, Any]] = []
    imported_log_ids: list[Any] = []

    for row in rows:
        student_id = row.get("student_id")
        assessment_id = row.get("assessment_id")
        if not student_id or not assessment_id:
            continue

        sid = str(student_id)
        aid = str(assessment_id)
        qmap = response_cache.setdefault((sid, aid), {})
        schedule_id = row.get("schedule_id")
        answers = row.get("answers_summary") or {}
        if isinstance(answers, str):
            answers = json.loads(answers)

        for _col, entry in answers.items():
            if not isinstance(entry, dict):
                continue
            qid_raw = entry.get("question_id")
            if not qid_raw:
                continue
            qid = str(qid_raw)
            m = re.match(r"Q(\d+)", _col, re.I)
            order_index = int(m.group(1)) if m else 0
            rid_raw = entry.get("response_id")
            response_id = str(rid_raw) if rid_raw else None
            raw_answer = entry.get("raw")
            is_invalid = bool(entry.get("is_invalid"))
            params = {
                "sid": sid,
                "aid": aid,
                "qid": qid,
                "rid": response_id,
                "oidx": order_index,
                "sch": str(schedule_id) if schedule_id else None,
                "raw": str(raw_answer) if raw_answer is not None else None,
                "inv": is_invalid,
                "bid": str(batch_id),
            }
            existing_id = qmap.get(qid)
            if existing_id is not None:
                updates.append({**params, "pk": _as_int_pk(existing_id)})
                stats["updated_responses"] += 1
            else:
                inserts.append(params)
                stats["imported_responses"] += 1
            if response_id is None:
                stats["null_responses"] += 1

        prof = row.get("proficiency_summary") or {}
        if isinstance(prof, str):
            prof = json.loads(prof)
        classroom_id = row.get("classroom_id")
        if classroom_id and prof:
            grade_year = row.get("ano_detectado")
            if grade_year is not None:
                try:
                    grade_year = int(grade_year)
                except (TypeError, ValueError):
                    grade_year = None
            cid = str(classroom_id)
            for area_slug, score in prof.items():
                if score is None:
                    continue
                level_code = await _resolve_level_code(
                    db,
                    area_slug=str(area_slug),
                    grade_year=grade_year,
                    score=float(score),
                    cache=level_cache,
                )
                prof_params.append(
                    {
                        "sid": sid,
                        "aid": aid,
                        "area": str(area_slug),
                        "score": float(score),
                        "level": level_code,
                        "cid": cid,
                        "std": "psp_2025",
                        "source": "imported",
                    }
                )

        imported_log_ids.append(row["id"])

    if updates:
        await execute_many(
            db,
            """
            UPDATE question_student_responsed
            SET response_id = CAST(:rid AS uuid),
                order_index = :oidx,
                schedule_id = COALESCE(CAST(:sch AS uuid), schedule_id),
                raw_answer = :raw,
                is_invalid_answer = :inv,
                import_batch_id = CAST(:bid AS uuid),
                updated_at = now()
            WHERE id = :pk
            """,
            updates,
        )
    if inserts:
        await execute_many(
            db,
            """
            INSERT INTO question_student_responsed (
              student_id, assessment_id, question_id, response_id, order_index,
              schedule_id, raw_answer, is_invalid_answer, import_batch_id
            ) VALUES (
              CAST(:sid AS uuid), CAST(:aid AS uuid), CAST(:qid AS uuid),
              CAST(:rid AS uuid), :oidx, CAST(:sch AS uuid),
              :raw, :inv, CAST(:bid AS uuid)
            )
            """,
            inserts,
        )

    if prof_params:
        await execute_many(
            db,
            """
            INSERT INTO student_assessment_area_proficiency (
              student_id, assessment_id, area_slug, proficiency, level_code,
              classroom_id, standard_set, source, computed_at, updated_at
            ) VALUES (
              CAST(:sid AS uuid), CAST(:aid AS uuid), CAST(:area AS text),
              CAST(:score AS numeric), CAST(:level AS varchar), CAST(:cid AS uuid),
              CAST(:std AS varchar), CAST(:source AS varchar), now(), now()
            )
            ON CONFLICT (student_id, assessment_id, area_slug) DO UPDATE SET
              proficiency = EXCLUDED.proficiency,
              level_code = EXCLUDED.level_code,
              classroom_id = EXCLUDED.classroom_id,
              standard_set = EXCLUDED.standard_set,
              source = EXCLUDED.source,
              computed_at = now(),
              updated_at = now()
            """,
            prof_params,
        )
        stats["imported_proficiencies"] += len(prof_params)

    if imported_log_ids:
        await execute_many(
            db,
            """
            UPDATE assessment_response_import_row_log
            SET status = 'imported'
            WHERE id = :id
            """,
            [{"id": row_id} for row_id in imported_log_ids],
        )


async def commit_import_batch(
    db: AsyncSession,
    batch_id: UUID,
    *,
    on_progress: Callable[[int, int, dict[str, Any]], Awaitable[None]] | None = None,
) -> dict[str, Any]:
    batch = await fetch_one(
        db,
        "SELECT * FROM assessment_response_import_batch WHERE id = CAST(:id AS uuid)",
        {"id": str(batch_id)},
    )
    if not batch:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Lote não encontrado")
    if batch["status"] not in ("validated", "validation_failed", "import_failed", "importing"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Lote não está validado")
    if batch["status"] == "imported":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Lote já importado")

    total = await _count_pending_import_rows(db, batch_id)
    if total == 0:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Nenhuma linha apta para importação")

    stats: dict[str, Any] = {
        "imported_responses": 0,
        "updated_responses": 0,
        "null_responses": 0,
        "imported_proficiencies": 0,
        "updated_proficiencies": 0,
        "errors": [],
    }

    if batch["status"] != "importing":
        await execute(
            db,
            """
            UPDATE assessment_response_import_batch
            SET status = 'importing',
                metadata = COALESCE(metadata, '{}'::jsonb) || CAST(:meta AS jsonb)
            WHERE id = CAST(:id AS uuid)
            """,
            {
                "id": str(batch_id),
                "meta": json.dumps(
                    {
                        "importing": True,
                        "import_processed_rows": 0,
                        "import_total_rows": total,
                        "import_stats": stats,
                    },
                    ensure_ascii=False,
                ),
            },
        )

    response_cache: dict[tuple[str, str], dict[str, int]] = {}
    level_cache: dict[tuple[str, int | None, float], str | None] = {}
    initial_total = total
    skipped_rows = int(batch.get("invalid_rows") or 0)

    try:
        if on_progress:
            await on_progress(0, initial_total, stats)
            await db.commit()

        processed = 0
        while True:
            chunk = await _fetch_pending_import_chunk(
                db, batch_id, limit=IMPORT_CHUNK_SIZE
            )
            if not chunk:
                break

            await _import_rows_chunk(
                db,
                batch_id=batch_id,
                rows=chunk,
                stats=stats,
                response_cache=response_cache,
                level_cache=level_cache,
            )
            processed += len(chunk)
            if on_progress:
                await on_progress(processed, initial_total, stats)
            else:
                await db.commit()

        stats["imported_rows"] = processed
        stats["skipped_rows"] = skipped_rows

        await execute(
            db,
            """
            UPDATE assessment_response_import_batch
            SET status = 'imported',
                imported_responses = :ir,
                updated_responses = :ur,
                null_responses = :nr,
                imported_proficiencies = :ip,
                updated_proficiencies = :up,
                imported_at = now(),
                metadata = COALESCE(metadata, '{}'::jsonb) || CAST(:meta AS jsonb)
            WHERE id = CAST(:id AS uuid)
            """,
            {
                "id": str(batch_id),
                "ir": stats["imported_responses"],
                "ur": stats["updated_responses"],
                "nr": stats["null_responses"],
                "ip": stats["imported_proficiencies"],
                "up": stats["updated_proficiencies"],
                "meta": json.dumps(
                    {
                        "importing": False,
                        "import_processed_rows": processed,
                        "import_total_rows": initial_total,
                        "import_stats": stats,
                    },
                    ensure_ascii=False,
                ),
            },
        )
    except Exception as exc:
        await db.rollback()
        try:
            await execute(
                db,
                """
                UPDATE assessment_response_import_batch
                SET status = 'import_failed',
                    metadata = COALESCE(metadata, '{}'::jsonb) || CAST(:meta AS jsonb)
                WHERE id = CAST(:id AS uuid)
                """,
                {
                    "id": str(batch_id),
                    "meta": json.dumps(
                        {"importing": False, "error": str(exc), "import_stats": stats},
                        ensure_ascii=False,
                    ),
                },
            )
            await db.commit()
        except Exception:
            await db.rollback()
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            f"Falha na importação: {exc}",
        ) from exc

    return stats


async def run_import_background(batch_id: UUID) -> None:
    from app.db.session import AsyncSessionLocal

    if not _try_acquire_import_job(batch_id):
        logger.info("import job already running for batch %s", batch_id)
        return

    try:
        async with AsyncSessionLocal() as db:
            try:
                logger.info("starting import for batch %s", batch_id)

                async def on_progress(
                    processed: int, total: int, stats: dict[str, Any]
                ) -> None:
                    await update_import_progress(
                        db,
                        batch_id,
                        processed_rows=processed,
                        total_rows=total,
                        stats=stats,
                    )
                    await db.commit()

                await commit_import_batch(db, batch_id, on_progress=on_progress)
                await db.commit()
                logger.info("import finished for batch %s", batch_id)
            except HTTPException as exc:
                await db.rollback()
                logger.exception("import failed for batch %s: %s", batch_id, exc.detail)
                async with AsyncSessionLocal() as err_db:
                    await execute(
                        err_db,
                        """
                        UPDATE assessment_response_import_batch
                        SET status = 'import_failed',
                            metadata = COALESCE(metadata, '{}'::jsonb) || CAST(:meta AS jsonb)
                        WHERE id = CAST(:id AS uuid)
                        """,
                        {
                            "id": str(batch_id),
                            "meta": json.dumps(
                                {"importing": False, "error": exc.detail},
                                ensure_ascii=False,
                            ),
                        },
                    )
                    await err_db.commit()
            except Exception as exc:
                await db.rollback()
                logger.exception("import failed for batch %s", batch_id)
                async with AsyncSessionLocal() as err_db:
                    await execute(
                        err_db,
                        """
                        UPDATE assessment_response_import_batch
                        SET status = 'import_failed',
                            metadata = COALESCE(metadata, '{}'::jsonb) || CAST(:meta AS jsonb)
                        WHERE id = CAST(:id AS uuid)
                        """,
                        {
                            "id": str(batch_id),
                            "meta": json.dumps(
                                {"importing": False, "error": str(exc)},
                                ensure_ascii=False,
                            ),
                        },
                    )
                    await err_db.commit()
    finally:
        _release_import_job(batch_id)


async def start_import_batch(db: AsyncSession, batch_id: UUID) -> dict[str, Any]:
    """Valida o lote e prepara importação em background."""
    batch = await fetch_one(
        db,
        "SELECT * FROM assessment_response_import_batch WHERE id = CAST(:id AS uuid)",
        {"id": str(batch_id)},
    )
    if not batch:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Lote não encontrado")
    if batch["status"] == "imported":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Lote já importado")
    if batch["status"] not in (
        "validated",
        "validation_failed",
        "import_failed",
        "importing",
    ):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Lote não está validado")

    pending = await fetch_one(
        db,
        """
        SELECT COUNT(*)::int AS cnt
        FROM assessment_response_import_row_log
        WHERE batch_id = CAST(:bid AS uuid) AND status = 'validated'
        """,
        {"bid": str(batch_id)},
    )
    pending_count = int((pending or {}).get("cnt") or 0)
    if pending_count == 0:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Nenhuma linha apta para importação")

    if batch["status"] != "importing":
        await execute(
            db,
            """
            UPDATE assessment_response_import_batch
            SET status = 'importing',
                metadata = COALESCE(metadata, '{}'::jsonb) || CAST(:meta AS jsonb)
            WHERE id = CAST(:id AS uuid)
            """,
            {
                "id": str(batch_id),
                "meta": json.dumps(
                    {
                        "importing": True,
                        "import_processed_rows": 0,
                        "import_total_rows": pending_count,
                        "import_stats": {},
                    },
                    ensure_ascii=False,
                ),
            },
        )
        batch = await fetch_one(
            db,
            "SELECT * FROM assessment_response_import_batch WHERE id = CAST(:id AS uuid)",
            {"id": str(batch_id)},
        )

    return batch_status_payload(batch or {})


def build_log_csv(rows: list[dict[str, Any]]) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(
        [
            "row_number",
            "status",
            "codigo_cartao",
            "ra",
            "student_name",
            "student_id",
            "assessment_id",
            "caderno",
            "ano_detectado",
            "errors",
            "warnings",
            "valid_answers",
            "blank_answers",
            "invalid_answers",
            "prof_lp",
            "prof_mt",
        ]
    )
    for row in rows:
        answers = row.get("answers_summary") or {}
        if isinstance(answers, str):
            answers = json.loads(answers)
        prof = row.get("proficiency_summary") or {}
        if isinstance(prof, str):
            prof = json.loads(prof)
        valid_a = sum(1 for v in answers.values() if isinstance(v, dict) and v.get("response_id"))
        blank_a = sum(
            1
            for v in answers.values()
            if isinstance(v, dict) and not v.get("is_invalid") and not v.get("label")
        )
        invalid_a = sum(1 for v in answers.values() if isinstance(v, dict) and v.get("is_invalid"))
        errors = row.get("errors") or []
        warnings = row.get("warnings") or []
        if isinstance(errors, str):
            errors = json.loads(errors)
        if isinstance(warnings, str):
            warnings = json.loads(warnings)
        writer.writerow(
            [
                row.get("row_number"),
                row.get("status"),
                row.get("codigo_cartao"),
                row.get("ra"),
                row.get("student_name"),
                row.get("student_id"),
                row.get("assessment_id"),
                row.get("caderno"),
                row.get("ano_detectado"),
                "; ".join(errors) if isinstance(errors, list) else str(errors),
                "; ".join(warnings) if isinstance(warnings, list) else str(warnings),
                valid_a,
                blank_a,
                invalid_a,
                prof.get(AREA_LP),
                prof.get(AREA_MT),
            ]
        )
    return buf.getvalue().encode("utf-8-sig")
