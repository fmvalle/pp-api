"""Testes do pacote de contexto do Avaliador."""

from app.v1.report_bundle import slim_pedagogical_snapshot


def test_slim_pedagogical_snapshot_shape():
    result = slim_pedagogical_snapshot(
        assessment_id="aid-1",
        schedule_id="sch-1",
        assessment_title="Matemática 1",
        start_date="2026-03-01",
        summary={
            "accuracyPercentage": 72.5,
            "classroomAverage": 72.5,
            "schoolAverage": 68.0,
            "systemAverage": 65.0,
            "totalQuestions": 20,
        },
        component_performance=[
            {
                "componentName": "Matemática",
                "areaName": "Matemática",
                "studentAccuracy": 60.0,
                "comparisonAverage": 72.0,
                "variationPercentagePoints": -12.0,
                "pedagogicalAction": "intervir",
            }
        ],
        pedagogical_reading={
            "text": "Priorize intervenção em Matemática.",
            "priorityComponents": [
                {
                    "componentName": "Matemática",
                    "pedagogicalAction": "intervir",
                    "variationPercentagePoints": -12.0,
                }
            ],
        },
        critical_questions=[
            {
                "order": 3,
                "component": "Matemática",
                "skill_code": "EF06MA01",
                "skill_description": "Resolver problemas",
                "classroom_accuracy_pct": 35.0,
                "responses": 28,
            }
        ],
    )

    assert result["assessment_title"] == "Matemática 1"
    assert result["summary"]["accuracy_percentage"] == 72.5
    assert result["components"][0]["action"] == "intervir"
    assert result["pedagogical_reading"]["priority_components"][0]["name"] == "Matemática"
    assert result["critical_questions"][0]["skill_code"] == "EF06MA01"
