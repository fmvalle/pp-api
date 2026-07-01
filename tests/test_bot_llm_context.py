"""Testes de preparação de contexto LLM."""

from app.v1.bot_llm import prepare_llm_context


def test_prepare_llm_context_narrows_schedule_data_pack():
    context = {
        "page_context": {
            "report_type": "schedule",
            "schedule_id": "abc-123",
            "assessment_title": "TAI de Matemática",
            "statistics": {"total_students": 6, "completed": 5},
        },
        "data_pack": {
            "schedules": [
                {"schedule_id": "abc-123", "assessment_title": "TAI de Matemática"},
                {"schedule_id": "other", "assessment_title": "Português"},
            ],
            "assessments": [
                {"assessment_id": "1", "title": "TAI de Matemática"},
                {"assessment_id": "2", "title": "Português"},
            ],
            "pedagogical_reports": [{"assessment_title": "x"}],
        },
    }

    narrowed = prepare_llm_context(context)
    assert len(narrowed["data_pack"]["schedules"]) == 1
    assert narrowed["data_pack"]["schedules"][0]["schedule_id"] == "abc-123"
    assert len(narrowed["data_pack"]["assessments"]) == 1
    assert narrowed["data_pack"]["pedagogical_reports"] == []
    assert "llm_focus" in narrowed
