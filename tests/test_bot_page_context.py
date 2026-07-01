"""Testes de respostas factuais a partir do page_context."""

from app.v1.bot_page_context import (
    should_skip_classroom_wide_sql,
    try_page_context_data_query,
)

SCHEDULE_CONTEXT = {
    "report_type": "schedule",
    "assessment_title": "TAI de Matemática",
    "classroom_name": "Turma A - Colégio Horizonte",
    "schedule_id": "abc-123",
    "statistics": {
        "total_students": 6,
        "completed": 5,
        "average_score": 68.33,
    },
    "students": [
        {"name": "Juliano Silva", "status": "graded", "score": 100},
        {"name": "Olivia Sousa", "status": "graded", "score": 83.33},
        {"name": "Paula Soares", "status": "graded", "score": 26.67},
        {"name": "Pedro Souza", "status": "graded", "score": 100},
        {"name": "Rafaela Cardoso", "status": "pending", "score": 0},
        {"name": "Thalita Coutinho", "status": "graded", "score": 100},
    ],
}


def test_schedule_completed_count_from_page_context():
    result = try_page_context_data_query(
        "Quantos alunos concluíram a avaliação?",
        SCHEDULE_CONTEXT,
    )
    assert result is not None
    assert result.intent_key == "data_page_schedule_completed"
    assert "**5** de **6**" in result.reply
    assert "TAI de Matemática" in result.reply


def test_schedule_average_from_page_context():
    result = try_page_context_data_query(
        "Qual a média da turma nesta prova?",
        SCHEDULE_CONTEXT,
    )
    assert result is not None
    assert result.intent_key == "data_page_schedule_average"
    assert "**68.33**" in result.reply


def test_schedule_pending_from_page_context():
    result = try_page_context_data_query(
        "Quantos alunos ainda não entregaram?",
        SCHEDULE_CONTEXT,
    )
    assert result is not None
    assert result.intent_key == "data_page_schedule_pending"
    assert "**1**" in result.reply


def test_should_skip_classroom_wide_sql_for_schedule():
    assert should_skip_classroom_wide_sql(SCHEDULE_CONTEXT) is True
    assert should_skip_classroom_wide_sql(None) is False
    assert should_skip_classroom_wide_sql({"report_type": "other"}) is False


def test_schedule_who_pending_from_page_context():
    result = try_page_context_data_query(
        "Quem ainda não entregou?",
        SCHEDULE_CONTEXT,
    )
    assert result is not None
    assert result.intent_key == "data_page_schedule_who_pending"
    assert "Rafaela Cardoso" in result.reply


def test_schedule_who_pending_variant():
    result = try_page_context_data_query(
        "Quem ainda está pendente de entregar?",
        SCHEDULE_CONTEXT,
    )
    assert result is not None
    assert result.intent_key == "data_page_schedule_who_pending"
    assert "Rafaela Cardoso" in result.reply


def test_schedule_summary_from_page_context():
    result = try_page_context_data_query(
        "Faça um resumo do resultado da avaliação para essa turma",
        SCHEDULE_CONTEXT,
    )
    assert result is not None
    assert result.intent_key == "data_page_schedule_summary"
    assert "**5**" in result.reply
    assert "**6**" in result.reply


def test_no_match_without_report_type():
    assert try_page_context_data_query("Quantos alunos?", {"page": "home"}) is None
