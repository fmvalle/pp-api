"""Geração de PDF/Excel do relatório pedagógico individual."""

from app.v1.pedagogical_report_export import (
    build_pedagogical_report_pdf_bytes,
    build_pedagogical_report_xlsx_bytes,
    pedagogical_export_filename,
)


def _sample_bundle() -> dict:
    return {
        "assessment": {"title": "Prova 1", "date": "2025-06-01T12:00:00"},
        "classroom": {"name": "7º A", "school": "Escola Teste"},
        "student": {"name": "Maria Silva"},
        "summary": {
            "totalQuestions": 10,
            "correctAnswers": 7,
            "accuracyPercentage": 70.0,
            "classroomAverage": 65.0,
            "schoolAverage": 60.0,
            "systemAverage": 58.0,
        },
        "pedagogicalReading": {"text": "Priorize intervenção em Matemática."},
        "componentPerformance": [
            {
                "componentName": "Matemática",
                "areaName": "Matemática",
                "totalQuestions": 5,
                "correctAnswers": 3,
                "studentAccuracy": 60.0,
                "comparisonAverage": 55.0,
                "variationPercentagePoints": 5.0,
                "pedagogicalAction": "orientar",
            }
        ],
        "questionGroups": [
            {
                "areaName": "Matemática",
                "componentName": "Matemática",
                "accuracyPercentage": 60.0,
                "questions": [
                    {
                        "questionNumber": 1,
                        "questionType": "multiple_choice",
                        "skillCode": "EF07MA01",
                        "skillDescription": "Números racionais",
                        "correctAnswer": "A",
                        "studentAnswer": "B",
                        "isCorrect": False,
                        "schoolAccuracyPercentage": 45.0,
                        "systemAccuracyPercentage": 40.0,
                        "totalResponses": 28,
                        "description": "<p>Questão exemplo</p>",
                    }
                ],
            }
        ],
    }


def test_pedagogical_export_filename():
    bundle = _sample_bundle()
    assert pedagogical_export_filename(bundle, "pdf").endswith(".pdf")
    assert "maria-silva" in pedagogical_export_filename(bundle, "pdf")


def test_build_pedagogical_report_pdf_bytes():
    pdf = build_pedagogical_report_pdf_bytes(_sample_bundle())
    assert pdf[:4] == b"%PDF"
    assert len(pdf) > 500


def test_build_pedagogical_report_xlsx_bytes():
    xlsx = build_pedagogical_report_xlsx_bytes(_sample_bundle())
    assert xlsx[:2] == b"PK"
    assert len(xlsx) > 200
