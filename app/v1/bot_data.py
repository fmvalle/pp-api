"""Consultas de dados da plataforma (camada local, sem LLM)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import AuthContext
from app.v1._academic_year import resolve_academic_year_id
from app.v1._sql import fetch_all, fetch_one
from app.v1.bot_context_pack import load_teacher_data_pack
from app.v1.bot_local import normalize_text

_DISCIPLINE_ALIASES: dict[str, tuple[str, ...]] = {
    "matematica": ("matematica", "mat", "math"),
    "portugues": ("portugues", "lp", "lingua portuguesa", "português"),
    "ciencias": ("ciencias", "ciência", "ciencias naturais"),
    "historia": ("historia", "história"),
    "geografia": ("geografia", "geo"),
}

_DATA_UNAVAILABLE_REPLY = (
    "Não encontrei dados suficientes no banco para responder com precisão. "
    "Selecione a **turma** no chat, informe o **caderno** ou **componente** "
    "(ex.: Matemática) e reformule — por exemplo: "
    "\"Qual aluno teve a menor média na turma?\" ou "
    "\"Qual aluno teve a menor nota na avaliação X?\"."
)


@dataclass(frozen=True)
class DataQueryResult:
    intent_key: str
    reply: str
    confidence: float


def _extract_assessment_hint(message: str) -> str | None:
    text = normalize_text(message)
    quoted = re.search(r"[\"']([^\"']+)[\"']", message)
    if quoted:
        return quoted.group(1).strip()
    match = re.search(
        r"(?:avaliacao|avaliação|caderno|prova|agendamento)\s+([a-z0-9\s\-_]{3,80})",
        text,
        re.IGNORECASE,
    )
    if match:
        return match.group(1).strip()
    return None


def _detect_discipline(message: str) -> str | None:
    text = normalize_text(message)
    for canonical, aliases in _DISCIPLINE_ALIASES.items():
        if any(alias in text for alias in aliases):
            return canonical
    return None


def _student_average_direction(text: str) -> str | None:
    """Detecta pergunta sobre aluno com menor/maior média de acertos."""
    has_media = "media" in text
    if not has_media:
        return None

    has_student = bool(
        re.search(r"\b(aluno|alunos|estudante|estudantes)\b", text)
        or re.search(r"\bqual\s+aluno\b", text)
    )
    wants_min = bool(re.search(r"\b(menor|pior|minim\w*|piores)\b", text))
    wants_max = bool(re.search(r"\b(maior|melhor|maxim\w*|melhores)\b", text))

    if not has_student:
        return None
    if wants_min and not wants_max:
        return "min"
    if wants_max and not wants_min:
        return "max"
    return None


def data_unavailable_result() -> DataQueryResult:
    return DataQueryResult(
        intent_key="data_unavailable",
        reply=_DATA_UNAVAILABLE_REPLY,
        confidence=88.0,
    )


async def _teacher_assessment_filter_sql(
    *,
    classroom_id: UUID | None,
    academic_year_id: UUID,
    assessment_hint: str | None,
) -> tuple[str, dict[str, Any]]:
    sql = """
    SELECT a.id, a.title
    FROM assessments a
    JOIN assessment_schedules sch ON sch.assessment_id = a.id
    JOIN classrooms c ON c.id = sch.classroom_id
    JOIN my_classrooms mc ON mc.classroom_id = c.id
    WHERE mc.teacher_id = CAST(:pid AS uuid)
      AND c.academic_year_id = CAST(:ay AS uuid)
    """
    params: dict[str, Any] = {
        "pid": None,  # filled by caller
        "ay": str(academic_year_id),
    }
    if classroom_id:
        sql += " AND c.id = CAST(:cid AS uuid)"
        params["cid"] = str(classroom_id)
    if assessment_hint:
        sql += " AND a.title ILIKE :hint"
        params["hint"] = f"%{assessment_hint}%"
    sql += " GROUP BY a.id, a.title ORDER BY MAX(sch.start_time) DESC NULLS LAST LIMIT 5"
    return sql, params


async def try_data_query(
    db: AsyncSession,
    ctx: AuthContext,
    *,
    message: str,
    classroom_id: UUID | None,
    academic_year_id: UUID | None,
    page_context: dict[str, Any] | None = None,
) -> DataQueryResult | None:
    text = normalize_text(message)
    effective_ay = await resolve_academic_year_id(db, academic_year_id)
    pid = str(ctx.active_profile_id)

    student_avg_dir = _student_average_direction(text)
    if student_avg_dir:
        return await _student_extreme_average(
            db,
            pid,
            classroom_id,
            effective_ay,
            message,
            direction=student_avg_dir,
        )

    if re.search(r"\b(menor|pior|minim\w*)\b.*\b(nota|score|desempenho)\b", text):
        return await _score_extreme(
            db, pid, classroom_id, effective_ay, message, direction="min"
        )

    if re.search(r"\b(maior|melhor|maxim\w*)\b.*\b(nota|score|desempenho)\b", text):
        return await _score_extreme(
            db, pid, classroom_id, effective_ay, message, direction="max"
        )

    if "media" in text:
        discipline = _detect_discipline(message)
        if discipline or "turma" in text or re.search(r"\bmedia\b.*\b(geral|componente)\b", text):
            return await _classroom_average(
                db, pid, classroom_id, effective_ay, discipline, message
            )
        if re.search(r"\b(aluno|estudante)\b", text) or re.search(r"\bqual\s+aluno\b", text):
            return data_unavailable_result()

    if re.search(r"\b(quantos|quantas)\b.*\b(alunos|pendentes|concluid)\b", text):
        from app.v1.bot_page_context import should_skip_classroom_wide_sql

        if not should_skip_classroom_wide_sql(page_context):
            return await _classroom_metrics(db, pid, classroom_id, effective_ay)

    schedule_result = await _try_schedule_query(
        db, pid, classroom_id, effective_ay, message, text
    )
    if schedule_result:
        return schedule_result

    return None


def _format_schedule_dt(row: dict[str, Any]) -> str:
    start = row.get("start_time")
    if start is None:
        return "data não informada"
    if hasattr(start, "strftime"):
        return start.strftime("%d/%m/%Y às %H:%M")
    return str(start)


async def _schedule_scope_sql(
    *,
    classroom_id: UUID | None,
    academic_year_id: UUID,
    assessment_hint: str | None,
) -> tuple[str, dict[str, Any]]:
    assessment_filter = ""
    params: dict[str, Any] = {"ay": str(academic_year_id)}
    if classroom_id:
        params["cid"] = str(classroom_id)
    if assessment_hint:
        assessment_filter = " AND a.title ILIKE :hint"
        params["hint"] = f"%{assessment_hint}%"
    classroom_filter = ""
    if classroom_id:
        classroom_filter = " AND sch.classroom_id = CAST(:cid AS uuid)"
    return assessment_filter, classroom_filter, params


def looks_like_schedule_question(message: str) -> bool:
    return _looks_like_schedule_question(normalize_text(message))


def looks_like_factual_question(message: str) -> bool:
    from app.v1.bot_local import looks_like_data_question

    text = normalize_text(message)
    return looks_like_data_question(message) or _looks_like_schedule_question(text)


async def _try_schedule_query(
    db: AsyncSession,
    pid: str,
    classroom_id: UUID | None,
    academic_year_id: UUID,
    message: str,
    text: str,
) -> DataQueryResult | None:
    if not _looks_like_schedule_question(text):
        return None

    hint = _extract_assessment_hint(message)
    assessment_filter, classroom_filter, params = await _schedule_scope_sql(
        classroom_id=classroom_id,
        academic_year_id=academic_year_id,
        assessment_hint=hint,
    )
    params["pid"] = pid

    wants_first = bool(
        re.search(r"\b(primeir\w*|1a|1ª)\b", text)
        and re.search(r"\b(avaliac\w*|prova|agend\w*|caderno|aplicad\w*|realizad\w*)\b", text)
    )
    wants_next = bool(re.search(r"\b(proxim\w*|seguinte)\b", text))
    wants_date = bool(
        re.search(r"\b(data|quando|dia|horario|horário)\b", text)
        or (hint and re.search(r"\b(data|quando)\b", text))
    )

    if wants_next:
        label = "Próxima avaliação"
        intent_key = "data_next_schedule"
        time_filter = " AND sch.start_time >= now()"
    elif wants_first:
        order = "ASC"
        label = "Primeira avaliação agendada"
        intent_key = "data_first_schedule"
        time_filter = ""
    elif wants_date and hint:
        order = "DESC"
        label = "Data da avaliação"
        intent_key = "data_schedule_date"
        time_filter = ""
    elif wants_date:
        order = "ASC"
        label = "Próxima avaliação"
        intent_key = "data_next_schedule"
        time_filter = " AND sch.start_time >= now()"
    else:
        order = "DESC"
        label = "Última avaliação agendada"
        intent_key = "data_last_schedule"
        time_filter = ""

    if wants_next or (wants_date and not hint):
        row = await fetch_one(
            db,
            f"""
            SELECT a.title AS assessment_title, c.name AS classroom_name, sch.start_time
            FROM assessment_schedules sch
            JOIN assessments a ON a.id = sch.assessment_id
            JOIN classrooms c ON c.id = sch.classroom_id
            JOIN my_classrooms mc ON mc.classroom_id = sch.classroom_id
            WHERE mc.teacher_id = CAST(:pid AS uuid)
              AND c.academic_year_id = CAST(:ay AS uuid)
              {classroom_filter}
              {assessment_filter}
              AND sch.start_time >= now()
            ORDER BY sch.start_time ASC NULLS LAST
            LIMIT 1
            """,
            params,
        )
        if not row:
            return DataQueryResult(
                intent_key=intent_key,
                reply="Não há próximas avaliações agendadas no escopo selecionado.",
                confidence=90.0,
            )
    else:
        row = await fetch_one(
            db,
            f"""
            SELECT a.title AS assessment_title, c.name AS classroom_name, sch.start_time
            FROM assessment_schedules sch
            JOIN assessments a ON a.id = sch.assessment_id
            JOIN classrooms c ON c.id = sch.classroom_id
            JOIN my_classrooms mc ON mc.classroom_id = sch.classroom_id
            WHERE mc.teacher_id = CAST(:pid AS uuid)
              AND c.academic_year_id = CAST(:ay AS uuid)
              {classroom_filter}
              {assessment_filter}
              {time_filter}
            ORDER BY sch.start_time {order} NULLS LAST
            LIMIT 1
            """,
            params,
        )
        if not row:
            scope = f" para \"{hint}\"" if hint else ""
            return DataQueryResult(
                intent_key=intent_key,
                reply=f"Não encontrei agendamentos{scope} no escopo da turma/professor.",
                confidence=90.0,
            )

    when = _format_schedule_dt(row)
    title = row.get("assessment_title") or "Avaliação"
    classroom = row.get("classroom_name") or "turma"
    return DataQueryResult(
        intent_key=intent_key,
        reply=(
            f"{label}: **{title}** ({classroom}) — **{when}**."
        ),
        confidence=94.0,
    )


def _looks_like_schedule_question(text: str) -> bool:
    _assessment = r"(avaliac\w*|prova|caderno|agend\w*)"
    _when = r"(data|quando|dia|horario|agendamento|agenda|cronograma)"
    _ordinal = r"(primeir\w*|proxim\w*|seguinte|ultim\w*)"
    return bool(
        re.search(rf"\b{_when}\b.*\b{_assessment}\b", text)
        or re.search(rf"\b{_assessment}\b.*\b{_when}\b", text)
        or re.search(rf"\b{_ordinal}\b.*\b{_assessment}\b", text)
        or re.search(r"\b(primeira|1a|1ª)\b.*\b(aplicad\w*|realizad\w*|agendad\w*)\b", text)
    )


async def _resolve_assessment_id(
    db: AsyncSession,
    pid: str,
    classroom_id: UUID | None,
    academic_year_id: UUID,
    message: str,
) -> dict[str, Any] | None:
    hint = _extract_assessment_hint(message)
    if not hint:
        return None
    sql, params = await _teacher_assessment_filter_sql(
        classroom_id=classroom_id,
        academic_year_id=academic_year_id,
        assessment_hint=hint,
    )
    params["pid"] = pid
    rows = await fetch_all(db, sql, params)
    if not rows:
        return None
    return rows[0]


async def _fetch_score_extreme(
    db: AsyncSession,
    *,
    pid: str,
    classroom_id: UUID | None,
    academic_year_id: UUID,
    assessment_id: UUID | None,
    direction: str,
) -> dict[str, Any] | None:
    order = "ASC" if direction == "min" else "DESC"
    params: dict[str, Any] = {
        "pid": pid,
        "ay": str(academic_year_id),
    }
    assessment_filter = ""
    if assessment_id:
        assessment_filter = " AND ar.assessment_id = CAST(:aid AS uuid)"
        params["aid"] = str(assessment_id)
    classroom_filter = ""
    if classroom_id:
        classroom_filter = " AND ar.classroom_id = CAST(:cid AS uuid)"
        params["cid"] = str(classroom_id)

    return await fetch_one(
        db,
        f"""
        SELECT
          pe.full_name AS student_name,
          ar.score,
          a.title AS assessment_title,
          c.name AS classroom_name
        FROM assessment_results ar
        JOIN assessments a ON a.id = ar.assessment_id
        JOIN profiles pr ON pr.id = ar.student_id
        JOIN people pe ON pe.id = pr.person_id
        JOIN classrooms c ON c.id = ar.classroom_id
        JOIN my_classrooms mc ON mc.classroom_id = ar.classroom_id
        WHERE mc.teacher_id = CAST(:pid AS uuid)
          AND c.academic_year_id = CAST(:ay AS uuid)
          AND ar.score IS NOT NULL
          {assessment_filter}
          {classroom_filter}
        ORDER BY ar.score {order} NULLS LAST, pe.full_name ASC
        LIMIT 1
        """,
        params,
    )


async def _score_extreme(
    db: AsyncSession,
    pid: str,
    classroom_id: UUID | None,
    academic_year_id: UUID,
    message: str,
    *,
    direction: str,
) -> DataQueryResult:
    hint = _extract_assessment_hint(message)
    assessment_title_filter: str | None = None
    assessment_id: UUID | None = None

    if hint:
        assessment = await _resolve_assessment_id(
            db, pid, classroom_id, academic_year_id, message
        )
        if not assessment:
            label = "menor" if direction == "min" else "maior"
            intent_key = "data_lowest_score" if direction == "min" else "data_highest_score"
            return DataQueryResult(
                intent_key=intent_key,
                reply=(
                    f"Não encontrei o caderno \"{hint}\". Informe o nome exato da avaliação "
                    f"(ex.: \"Qual aluno teve a {label} nota na avaliação Matemática 1?\") "
                    "ou selecione a turma."
                ),
                confidence=90.0,
            )
        assessment_id = assessment["id"]
        assessment_title_filter = str(assessment["title"])

    row = await _fetch_score_extreme(
        db,
        pid=pid,
        classroom_id=classroom_id,
        academic_year_id=academic_year_id,
        assessment_id=assessment_id,
        direction=direction,
    )

    intent_key = "data_lowest_score" if direction == "min" else "data_highest_score"
    label = "Menor" if direction == "min" else "Maior"

    if not row:
        if assessment_title_filter:
            reply = (
                f"Não há notas registradas para **{assessment_title_filter}** "
                "no escopo da sua turma."
            )
        else:
            reply = (
                "Não há notas registradas no escopo da sua turma. "
                "Confirme se as provas já foram corrigidas ou cite o caderno desejado."
            )
        return DataQueryResult(intent_key=intent_key, reply=reply, confidence=92.0)

    score = float(row["score"])
    title = row.get("assessment_title") or assessment_title_filter or "avaliação"
    classroom = row.get("classroom_name") or "turma"
    return DataQueryResult(
        intent_key=intent_key,
        reply=(
            f"{label} nota em **{title}** ({classroom}): "
            f"**{row['student_name']}** com **{score:.1f}**."
        ),
        confidence=95.0,
    )


async def _classroom_average(
    db: AsyncSession,
    pid: str,
    classroom_id: UUID | None,
    academic_year_id: UUID,
    discipline: str | None,
    message: str,
) -> DataQueryResult | None:
    discipline_filter = ""
    params: dict[str, Any] = {
        "pid": pid,
        "ay": str(academic_year_id),
    }
    if classroom_id:
        params["cid"] = str(classroom_id)

    if discipline:
        aliases = _DISCIPLINE_ALIASES.get(discipline, (discipline,))
        patterns = [f"%{alias}%" for alias in aliases]
        discipline_filter = """
          AND (
            LOWER(COALESCE(v.discipline_name, '')) LIKE ANY(:patterns)
            OR LOWER(COALESCE(v.discipline_slug, '')) LIKE ANY(:patterns)
          )
        """
        params["patterns"] = patterns

    sql = f"""
    SELECT
      c.name AS classroom_name,
      COALESCE(v.discipline_name, 'Geral') AS discipline_name,
      ROUND(
        (SUM(v.correct_answers)::numeric / NULLIF(SUM(v.total_questions), 0)) * 100,
        1
      ) AS avg_accuracy,
      COUNT(DISTINCT v.student_id)::int AS students
    FROM vw_assessment_component_results v
    JOIN classrooms c ON c.id = v.classroom_id
    JOIN my_classrooms mc ON mc.classroom_id = v.classroom_id
    WHERE mc.teacher_id = CAST(:pid AS uuid)
      AND c.academic_year_id = CAST(:ay AS uuid)
      {discipline_filter}
    """
    if classroom_id:
        sql += " AND v.classroom_id = CAST(:cid AS uuid)"
    sql += """
    GROUP BY c.name, COALESCE(v.discipline_name, 'Geral')
    ORDER BY avg_accuracy DESC NULLS LAST
    LIMIT 1
    """

    row = await fetch_one(db, sql, params)
    if not row:
        return DataQueryResult(
            intent_key="data_classroom_average",
            reply=(
                "Não encontrei médias de componente para o filtro informado. "
                "Confirme a turma selecionada e se já há avaliações corrigidas."
            ),
            confidence=88.0,
        )

    label = discipline or row.get("discipline_name") or "componente"
    return DataQueryResult(
        intent_key="data_classroom_average",
        reply=(
            f"Média de **{label}** na turma **{row['classroom_name']}**: "
            f"**{row['avg_accuracy']}%** de acerto "
            f"({row['students']} aluno(s) com dados)."
        ),
        confidence=93.0,
    )


async def _student_extreme_average(
    db: AsyncSession,
    pid: str,
    classroom_id: UUID | None,
    academic_year_id: UUID,
    message: str,
    *,
    direction: str,
) -> DataQueryResult:
    discipline = _detect_discipline(message)
    discipline_filter = ""
    params: dict[str, Any] = {
        "pid": pid,
        "ay": str(academic_year_id),
    }
    if classroom_id:
        params["cid"] = str(classroom_id)

    if discipline:
        aliases = _DISCIPLINE_ALIASES.get(discipline, (discipline,))
        params["patterns"] = [f"%{alias}%" for alias in aliases]
        discipline_filter = """
          AND (
            LOWER(COALESCE(v.discipline_name, '')) LIKE ANY(:patterns)
            OR LOWER(COALESCE(v.discipline_slug, '')) LIKE ANY(:patterns)
          )
        """

    order = "ASC" if direction == "min" else "DESC"
    label = "Menor" if direction == "min" else "Maior"
    intent_key = "data_lowest_student_average" if direction == "min" else "data_highest_student_average"

    sql = f"""
    SELECT
      pe.full_name AS student_name,
      c.name AS classroom_name,
      ROUND(
        (SUM(v.correct_answers)::numeric / NULLIF(SUM(v.total_questions), 0)) * 100,
        1
      ) AS avg_accuracy,
      COUNT(DISTINCT v.assessment_id)::int AS assessments
    FROM vw_assessment_component_results v
    JOIN profiles pr ON pr.id = v.student_id
    JOIN people pe ON pe.id = pr.person_id
    JOIN classrooms c ON c.id = v.classroom_id
    JOIN my_classrooms mc ON mc.classroom_id = v.classroom_id
    WHERE mc.teacher_id = CAST(:pid AS uuid)
      AND c.academic_year_id = CAST(:ay AS uuid)
      {discipline_filter}
    """
    if classroom_id:
        sql += " AND v.classroom_id = CAST(:cid AS uuid)"
    sql += f"""
    GROUP BY pe.full_name, c.name, v.student_id
    HAVING SUM(v.total_questions) > 0
    ORDER BY avg_accuracy {order} NULLS LAST, pe.full_name ASC
    LIMIT 1
    """

    row = await fetch_one(db, sql, params)
    if not row:
        scope = discipline or "geral"
        return DataQueryResult(
            intent_key=intent_key,
            reply=(
                f"Não há médias de acerto ({scope}) registradas para os alunos "
                "no escopo selecionado. Confirme a turma e se já existem avaliações corrigidas."
            ),
            confidence=90.0,
        )

    component_label = f" em **{discipline}**" if discipline else ""
    return DataQueryResult(
        intent_key=intent_key,
        reply=(
            f"{label} média de acerto{component_label} na turma **{row['classroom_name']}**: "
            f"**{row['student_name']}** com **{row['avg_accuracy']}%** "
            f"({row['assessments']} avaliação(ões) consideradas)."
        ),
        confidence=95.0,
    )


async def _classroom_metrics(
    db: AsyncSession,
    pid: str,
    classroom_id: UUID | None,
    academic_year_id: UUID,
) -> DataQueryResult:
    sql = """
    SELECT
      COUNT(DISTINCT cs.student_id)::int AS total_students,
      COUNT(*) FILTER (WHERE vas.pending > 0)::int AS pending,
      COUNT(*) FILTER (WHERE vas.completed > 0)::int AS completed
    FROM vw_assessment_summary vas
    JOIN classrooms c ON c.id = vas.classroom_id
    JOIN my_classrooms mc ON mc.classroom_id = vas.classroom_id
    LEFT JOIN classroom_students cs ON cs.classroom_id = vas.classroom_id
    WHERE mc.teacher_id = CAST(:pid AS uuid)
      AND c.academic_year_id = CAST(:ay AS uuid)
    """
    params: dict[str, Any] = {"pid": pid, "ay": str(academic_year_id)}
    if classroom_id:
        sql += " AND vas.classroom_id = CAST(:cid AS uuid)"
        params["cid"] = str(classroom_id)

    row = await fetch_one(db, sql, params) or {}
    return DataQueryResult(
        intent_key="data_classroom_metrics",
        reply=(
            f"Resumo: **{int(row.get('total_students') or 0)}** aluno(s), "
            f"**{int(row.get('pending') or 0)}** avaliação(ões) com pendências e "
            f"**{int(row.get('completed') or 0)}** com entregas concluídas."
        ),
        confidence=90.0,
    )
