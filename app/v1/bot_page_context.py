"""Respostas factuais a partir do page_context (relatório aberto na tela)."""

from __future__ import annotations

import re
from typing import Any

from app.v1.bot_data import DataQueryResult
from app.v1.bot_local import normalize_text


def _title(page_context: dict[str, Any]) -> str:
    return str(page_context.get("assessment_title") or "esta avaliação")


def _classroom(page_context: dict[str, Any]) -> str:
    return str(page_context.get("classroom_name") or "a turma")


_COMPLETED_STATUSES = frozenset({"submitted", "graded", "completed", "concluido", "concluida"})


def _norm_status(status: str) -> str:
    return normalize_text(status or "pending")


def _is_completed_status(status: str) -> bool:
    return _norm_status(status) in _COMPLETED_STATUSES


def _is_not_delivered_status(status: str) -> bool:
    return not _is_completed_status(status)


def _schedule_students(page_context: dict[str, Any]) -> list[dict[str, Any]]:
    students = page_context.get("students") or []
    if not isinstance(students, list):
        return []
    return [s for s in students if isinstance(s, dict)]


def _student_name(student: dict[str, Any]) -> str:
    return str(student.get("name") or "Aluno")


def _pending_student_names(students: list[dict[str, Any]]) -> list[str]:
    return [
        _student_name(s)
        for s in students
        if _is_not_delivered_status(str(s.get("status") or ""))
    ]


def _looks_like_who_pending(text: str) -> bool:
    if not re.search(r"\b(quem|quais alunos|qual aluno)\b", text):
        return False
    return bool(
        re.search(
            r"\b(pendent\w*|falt\w*|nao entreg\w*|nao conclu\w*|ainda nao|esta pendente)\b",
            text,
        )
    )


def _format_name_list(names: list[str]) -> str:
    if not names:
        return ""
    if len(names) == 1:
        return f"**{names[0]}**"
    return ", ".join(f"**{name}**" for name in names[:-1]) + f" e **{names[-1]}**"


def try_page_context_data_query(
    message: str,
    page_context: dict[str, Any] | None,
) -> DataQueryResult | None:
    if not page_context:
        return None

    text = normalize_text(message)
    report_type = str(page_context.get("report_type") or "")

    if report_type == "schedule":
        return _schedule_report_query(text, page_context)
    if report_type == "pedagogical":
        return _pedagogical_report_query(text, page_context)
    if report_type == "macro":
        return _macro_report_query(text, page_context)
    if report_type == "student":
        return _student_report_query(text, page_context)

    return None


def _schedule_report_query(text: str, page_context: dict[str, Any]) -> DataQueryResult | None:
    stats = page_context.get("statistics") or {}
    if not isinstance(stats, dict):
        return None

    total = int(stats.get("total_students") or 0)
    completed = int(stats.get("completed") or 0)
    pending = max(0, total - completed)
    average = stats.get("average_score")
    title = _title(page_context)
    classroom = _classroom(page_context)
    students = _schedule_students(page_context)

    if _looks_like_who_pending(text):
        pending_names = _pending_student_names(students)
        if pending_names:
            subject = "Ainda não entregou" if len(pending_names) == 1 else "Ainda não entregaram"
            return DataQueryResult(
                intent_key="data_page_schedule_who_pending",
                reply=(
                    f"Nesta avaliação (**{title}**), {subject}: {_format_name_list(pending_names)}."
                ),
                confidence=98.0,
            )
        if students:
            return DataQueryResult(
                intent_key="data_page_schedule_who_pending_none",
                reply=(
                    f"Todos os alunos desta avaliação (**{title}**, {classroom}) "
                    f"já concluíram a prova."
                ),
                confidence=98.0,
            )
        if pending > 0:
            return DataQueryResult(
                intent_key="data_page_schedule_who_pending_count",
                reply=(
                    f"Nesta avaliação (**{title}**), **{pending}** aluno(s) ainda não concluíram, "
                    f"mas os nomes não estão disponíveis no contexto."
                ),
                confidence=90.0,
            )

    if re.search(
        r"\b(quantos|quantas)\b.*\b(pendent\w*|falt\w*|nao entreg\w*|pend\w*)",
        text,
    ):
        return DataQueryResult(
            intent_key="data_page_schedule_pending",
            reply=(
                f"Nesta avaliação (**{title}**), **{pending}** aluno(s) ainda não concluíram "
                f"(de **{total}** no total)."
            ),
            confidence=98.0,
        )

    if re.search(r"\b(quantos|quantas)\b.*\b(conclu\w*|entreg\w*|finaliz\w*|fech\w*)", text):
        return DataQueryResult(
            intent_key="data_page_schedule_completed",
            reply=(
                f"Nesta avaliação (**{title}**, {classroom}), **{completed}** de **{total}** "
                f"aluno(s) concluíram a prova."
            ),
            confidence=98.0,
        )

    if re.search(r"\b(quantos|quantas)\b.*\b(alunos|estudantes)\b", text):
        return DataQueryResult(
            intent_key="data_page_schedule_students",
            reply=(
                f"Nesta avaliação (**{title}**, {classroom}), há **{total}** aluno(s) — "
                f"**{completed}** concluíram e **{pending}** pendente(s)."
            ),
            confidence=98.0,
        )

    if re.search(r"\b(media|média|nota)\b", text):
        if average is not None:
            avg = round(float(average), 2)
            return DataQueryResult(
                intent_key="data_page_schedule_average",
                reply=f"A média nesta prova (**{title}**) é **{avg}**.",
                confidence=98.0,
            )

    if re.search(r"\b(resumo|sumari|sintetiz|resultado da avaliacao|resultado desta)\b", text):
        avg_text = f" A média é **{round(float(average), 2)}**." if average is not None else ""
        pending_names = _pending_student_names(students)
        pending_text = ""
        if pending_names:
            pending_text = f" Pendente(s): {_format_name_list(pending_names)}."
        elif pending == 0 and total > 0:
            pending_text = " Todos concluíram."
        return DataQueryResult(
            intent_key="data_page_schedule_summary",
            reply=(
                f"**{title}** ({classroom}): **{total}** aluno(s), "
                f"**{completed}** concluíram e **{pending}** pendente(s).{avg_text}{pending_text}"
            ),
            confidence=97.0,
        )

    return None


def _pedagogical_report_query(text: str, page_context: dict[str, Any]) -> DataQueryResult | None:
    summary = page_context.get("summary") or {}
    if not isinstance(summary, dict):
        summary = {}

    title = _title(page_context)
    classroom = _classroom(page_context)
    accuracy = summary.get("accuracy_percentage")
    classroom_avg = summary.get("classroom_average")

    if re.search(r"\b(quantos|quantas)\b.*\b(alunos|estudantes|quest)\b", text):
        total_q = summary.get("total_questions")
        if total_q is not None:
            return DataQueryResult(
                intent_key="data_page_pedagogical_scope",
                reply=(
                    f"No relatório pedagógico de **{title}** ({classroom}), "
                    f"a análise considera **{int(total_q)}** questão(ões) "
                    f"com acurácia da turma de **{accuracy}%**."
                    if accuracy is not None
                    else f"No relatório pedagógico de **{title}** ({classroom})."
                ),
                confidence=95.0,
            )

    if re.search(r"\b(media|média|acuracia|acurácia)\b", text):
        if accuracy is not None:
            return DataQueryResult(
                intent_key="data_page_pedagogical_accuracy",
                reply=(
                    f"A acurácia da turma **{classroom}** em **{title}** é **{accuracy}%** "
                    f"(média da turma: **{classroom_avg}%**)."
                    if classroom_avg is not None
                    else f"A acurácia da turma em **{title}** é **{accuracy}%**."
                ),
                confidence=98.0,
            )

    reading = page_context.get("pedagogical_reading") or {}
    if isinstance(reading, dict) and re.search(
        r"\b(intervir|orientar|desafiar|priorid|componente|leitura pedag)\b", text
    ):
        reading_text = str(reading.get("text") or "").strip()
        if reading_text:
            return DataQueryResult(
                intent_key="data_page_pedagogical_reading",
                reply=f"Leitura pedagógica de **{title}**: {reading_text}",
                confidence=96.0,
            )

    components = page_context.get("components") or []
    if isinstance(components, list) and re.search(r"\b(intervir|componente)\b", text):
        intervir = [
            c for c in components
            if isinstance(c, dict) and str(c.get("action") or "") == "intervir"
        ]
        if intervir:
            names = ", ".join(str(c.get("name") or "") for c in intervir)
            return DataQueryResult(
                intent_key="data_page_pedagogical_components",
                reply=f"Componentes para **intervir** em **{title}**: {names}.",
                confidence=96.0,
            )

    return None


def _macro_title(page_context: dict[str, Any]) -> str:
    return str(page_context.get("macro_title") or page_context.get("assessment_title") or "relatório macro")


def _macro_report_query(text: str, page_context: dict[str, Any]) -> DataQueryResult | None:
    stats = page_context.get("statistics") or {}
    if not isinstance(stats, dict):
        stats = {}

    total = int(stats.get("total_students") or 0)
    completed = int(stats.get("completed") or 0)
    pending = max(0, total - completed)
    avg_accuracy = stats.get("avg_accuracy")
    completion_rate = stats.get("completion_rate")
    title = _macro_title(page_context)
    classroom = _classroom(page_context)
    students = _schedule_students(page_context)
    assessments = page_context.get("assessments") or []
    caderno_count = len(assessments) if isinstance(assessments, list) else 0

    if _looks_like_who_pending(text):
        pending_names = _pending_student_names(students)
        if pending_names:
            unique = list(dict.fromkeys(pending_names))
            subject = "Ainda não concluíram" if len(unique) == 1 else "Ainda não concluíram"
            return DataQueryResult(
                intent_key="data_page_macro_who_pending",
                reply=f"No **{title}** ({classroom}), {subject}: {_format_name_list(unique)}.",
                confidence=98.0,
            )

    if re.search(r"\b(quantos|quantas)\b.*\b(conclu\w*|entreg\w*)\b", text):
        return DataQueryResult(
            intent_key="data_page_macro_completed",
            reply=(
                f"No **{title}** ({classroom}), **{completed}** de **{total}** "
                f"registro(s) de aluno/caderno concluíram."
            ),
            confidence=98.0,
        )

    if re.search(r"\b(quantos|quantas)\b.*\b(alunos|estudantes)\b", text):
        return DataQueryResult(
            intent_key="data_page_macro_students",
            reply=(
                f"No **{title}** ({classroom}), há **{total}** aluno(s) "
                f"em **{caderno_count}** caderno(s)."
            ),
            confidence=96.0,
        )

    if re.search(r"\b(media|média|acuracia|acurácia)\b", text) and avg_accuracy is not None:
        return DataQueryResult(
            intent_key="data_page_macro_average",
            reply=f"A média de acurácia no **{title}** ({classroom}) é **{round(float(avg_accuracy), 1)}%**.",
            confidence=98.0,
        )

    if re.search(r"\b(resumo|sumari|sintetiz|resultado)\b", text):
        rate_text = (
            f" Taxa de conclusão: **{round(float(completion_rate), 0)}%**."
            if completion_rate is not None
            else ""
        )
        avg_text = (
            f" Média de acurácia: **{round(float(avg_accuracy), 1)}%**."
            if avg_accuracy is not None
            else ""
        )
        return DataQueryResult(
            intent_key="data_page_macro_summary",
            reply=(
                f"**{title}** — **{classroom}**: **{total}** aluno(s), "
                f"**{completed}** concluíram, **{pending}** pendente(s), "
                f"**{caderno_count}** caderno(s).{rate_text}{avg_text}"
            ),
            confidence=97.0,
        )

    return None


def _student_report_query(text: str, page_context: dict[str, Any]) -> DataQueryResult | None:
    student_name = str(page_context.get("student_name") or "o aluno")
    title = _title(page_context)
    classroom = _classroom(page_context)
    summary = page_context.get("summary") or {}
    if not isinstance(summary, dict):
        summary = {}
    accuracy = summary.get("accuracy_percentage")

    if re.search(r"\b(resumo|sumari|desempenho|resultado)\b", text):
        acc_text = f" Acurácia: **{accuracy}%**." if accuracy is not None else ""
        return DataQueryResult(
            intent_key="data_page_student_summary",
            reply=f"Relatório individual de **{student_name}** em **{title}** ({classroom}).{acc_text}",
            confidence=96.0,
        )

    return _pedagogical_report_query(text, page_context)


def should_skip_classroom_wide_sql(page_context: dict[str, Any] | None) -> bool:
    """Evita métricas agregadas da turma quando há relatório específico aberto."""
    if not page_context:
        return False
    return str(page_context.get("report_type") or "") in (
        "schedule",
        "pedagogical",
        "macro",
        "student",
    )
