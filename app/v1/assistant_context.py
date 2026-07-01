"""Contexto do professor e respostas stub (Avaliador)."""

from __future__ import annotations

import re
from typing import Any
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import AuthContext
from app.v1._academic_year import resolve_academic_year_id
from app.v1._scope import resolve_bot_audience
from app.v1._sql import fetch_one
from app.v1.bot_context_pack import load_teacher_data_pack

_TEACHER_SUGGESTIONS = (
    "Como interpretar o relatório pedagógico?",
    "Qual aluno teve a menor nota na avaliação?",
    "Qual a média de matemática da turma?",
    "Como acompanhar presença nas avaliações?",
)

_PLATFORM_ADMIN_SUGGESTIONS = (
    "Como cadastrar uma escola?",
    "Como configurar o Avaliador (LLM)?",
    "Como gerenciar agendamentos na plataforma?",
    "O que é o catálogo pedagógico?",
)

_DEFAULT_SUGGESTIONS = _TEACHER_SUGGESTIONS


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


async def assert_classroom_access(
    db: AsyncSession,
    ctx: AuthContext,
    classroom_id: UUID,
) -> dict[str, Any]:
    row = await fetch_one(
        db,
        """
        SELECT c.id, c.name, c.code, s.name AS school_name
        FROM classrooms c
        LEFT JOIN schools s ON s.id = c.school_id
        WHERE c.id = CAST(:cid AS uuid)
        LIMIT 1
        """,
        {"cid": str(classroom_id)},
    )
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Turma não encontrada")

    ok = await fetch_one(
        db,
        """
        SELECT 1 AS ok FROM my_classrooms mc
        WHERE mc.teacher_id = CAST(:pid AS uuid)
          AND mc.classroom_id = CAST(:cid AS uuid)
        LIMIT 1
        """,
        {"pid": str(ctx.active_profile_id), "cid": str(classroom_id)},
    )
    if not ok:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Turma fora do vínculo do professor")
    return row


async def load_teacher_context(
    db: AsyncSession,
    ctx: AuthContext,
    *,
    classroom_id: UUID | None,
    academic_year_id: UUID | None,
) -> dict[str, Any]:
    effective_ay = await resolve_academic_year_id(db, academic_year_id)
    context: dict[str, Any] = {"academic_year_id": str(effective_ay)}

    if classroom_id:
        classroom_row = await assert_classroom_access(db, ctx, classroom_id)
        context["classroom"] = {
            "id": str(classroom_row["id"]),
            "name": classroom_row.get("name"),
            "code": classroom_row.get("code"),
            "school_name": classroom_row.get("school_name"),
        }

    stats_sql = """
    SELECT
      COUNT(DISTINCT cs.student_id)::int AS total_students,
      COUNT(*) FILTER (WHERE vas.pending > 0)::int AS active_assessments,
      COUNT(*) FILTER (WHERE vas.completed > 0)::int AS completed_assessments,
      COUNT(*)::int AS total_assessments
    FROM vw_assessment_summary vas
    JOIN classrooms c ON c.id = vas.classroom_id
    JOIN my_classrooms mc ON mc.classroom_id = vas.classroom_id
    LEFT JOIN classroom_students cs ON cs.classroom_id = vas.classroom_id
    WHERE mc.teacher_id = CAST(:pid AS uuid)
      AND c.academic_year_id = CAST(:ay AS uuid)
    """
    params: dict[str, Any] = {
        "pid": str(ctx.active_profile_id),
        "ay": str(effective_ay),
    }
    if classroom_id:
        stats_sql += " AND vas.classroom_id = CAST(:cid AS uuid)"
        params["cid"] = str(classroom_id)

    stats = await fetch_one(db, stats_sql, params) or {}
    context["metrics"] = {
        "total_students": int(stats.get("total_students") or 0),
        "active_assessments": int(stats.get("active_assessments") or 0),
        "completed_assessments": int(stats.get("completed_assessments") or 0),
        "total_assessments": int(stats.get("total_assessments") or 0),
    }
    context["data_pack"] = await load_teacher_data_pack(
        db,
        pid=str(ctx.active_profile_id),
        academic_year_id=effective_ay,
        classroom_id=classroom_id,
    )
    context["audience"] = "teacher"
    return context


async def load_platform_admin_context(
    db: AsyncSession,
    ctx: AuthContext,
    *,
    academic_year_id: UUID | None,
) -> dict[str, Any]:
    effective_ay = await resolve_academic_year_id(db, academic_year_id)
    schools = await fetch_one(
        db,
        "SELECT COUNT(*)::int AS total FROM schools",
        {},
    ) or {}
    assessments = await fetch_one(
        db,
        "SELECT COUNT(*)::int AS total FROM assessments",
        {},
    ) or {}
    schedules = await fetch_one(
        db,
        """
        SELECT COUNT(*)::int AS total
        FROM assessment_schedules sch
        JOIN classrooms c ON c.id = sch.classroom_id
        WHERE c.academic_year_id = CAST(:ay AS uuid)
        """,
        {"ay": str(effective_ay)},
    ) or {}
    return {
        "audience": "platform_admin",
        "academic_year_id": str(effective_ay),
        "metrics": {
            "schools": int(schools.get("total") or 0),
            "assessments": int(assessments.get("total") or 0),
            "schedules": int(schedules.get("total") or 0),
        },
    }


async def load_assistant_context(
    db: AsyncSession,
    ctx: AuthContext,
    *,
    classroom_id: UUID | None,
    academic_year_id: UUID | None,
) -> dict[str, Any]:
    if resolve_bot_audience(ctx.role) == "platform_admin":
        return await load_platform_admin_context(
            db,
            ctx,
            academic_year_id=academic_year_id,
        )
    return await load_teacher_context(
        db,
        ctx,
        classroom_id=classroom_id,
        academic_year_id=academic_year_id,
    )


def suggestions_for_audience(role: str) -> list[str]:
    if resolve_bot_audience(role) == "platform_admin":
        return list(_PLATFORM_ADMIN_SUGGESTIONS)
    return list(_TEACHER_SUGGESTIONS)


def build_stub_reply(message: str, context: dict[str, Any]) -> tuple[str, list[str]]:
    text = normalize_text(message)
    audience = str(context.get("audience") or "teacher")
    metrics = context.get("metrics") or {}

    if audience == "platform_admin":
        if any(k in text for k in ("escola", "escolas", "unidade")):
            return (
                f"A rede tem **{metrics.get('schools', 0)}** escola(s) cadastrada(s). "
                "Gerencie em **Escolas** (`/admin/schools`).",
                list(_PLATFORM_ADMIN_SUGGESTIONS),
            )
        if any(k in text for k in ("avalia", "caderno", "macro", "agend")):
            return (
                f"Há **{metrics.get('assessments', 0)}** avaliação(ões) e "
                f"**{metrics.get('schedules', 0)}** agendamento(s) no ano letivo. "
                "Use `/admin/assessments` e `/admin/schedules`.",
                list(_PLATFORM_ADMIN_SUGGESTIONS),
            )
        if any(k in text for k in ("bot", "avaliador", "llm", "inten")):
            return (
                "Configure LLM e intenções locais em **Config. Avaliador** (`/admin/bot`).",
                list(_PLATFORM_ADMIN_SUGGESTIONS),
            )
        return (
            "Entendi sua pergunta sobre a plataforma. Tente uma sugestão abaixo ou "
            "pergunte sobre escolas, usuários, avaliações ou catálogo.",
            list(_PLATFORM_ADMIN_SUGGESTIONS),
        )

    classroom = context.get("classroom") or {}
    classroom_name = classroom.get("name") or "sua turma"

    if any(k in text for k in ("presença", "presenca", "falta", "faltou", "comparec")):
        return (
            "Para presença nas avaliações, use Presença no menu ou o relatório por "
            "agendamento. Registre comparecimento antes de liberar a prova.",
            list(_TEACHER_SUGGESTIONS),
        )

    if any(k in text for k in ("relatório", "relatorio", "pedagógico", "pedagogico")):
        return (
            "O relatório pedagógico mostra desempenho por habilidade e questão. "
            "Comece pelo resumo da turma e aprofunde questão a questão.",
            list(_TEACHER_SUGGESTIONS),
        )

    if any(k in text for k in ("turma", "aluno", "desempenho", "nota", "media", "média")):
        students = metrics.get("total_students", 0)
        active = metrics.get("active_assessments", 0)
        completed = metrics.get("completed_assessments", 0)
        return (
            f"Para {classroom_name}: {students} aluno(s), "
            f"{active} com pendências e {completed} concluídas.",
            list(_TEACHER_SUGGESTIONS),
        )

    return (
        "Entendi sua pergunta. Tente reformular ou escolha uma sugestão abaixo.",
        list(_TEACHER_SUGGESTIONS),
    )
