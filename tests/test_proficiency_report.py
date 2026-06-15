"""Testes do módulo de proficiência."""

from app.v1.proficiency_report import proficiency_table_missing


def test_proficiency_table_missing_detects_absent_table():
    exc = Exception('relation "student_assessment_area_proficiency" does not exist')
    assert proficiency_table_missing(exc) is True


def test_proficiency_table_missing_other_errors():
    assert proficiency_table_missing(Exception("connection refused")) is False
