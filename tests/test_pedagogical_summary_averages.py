"""Garante que médias de escola/sistema não reutilizam o SQL recortado à turma."""

from app.v1.report_bundle import _COMPONENT_RESULTS_SQL, _SUMMARY_AVERAGES_SQL


def test_summary_averages_sql_aggregates_all_classrooms():
    assert "JOIN assessment_schedules ass" in _SUMMARY_AVERAGES_SQL
    assert "JOIN classrooms c ON c.id = ass.classroom_id" in _SUMMARY_AVERAGES_SQL
    assert "FILTER (WHERE school_id" in _SUMMARY_AVERAGES_SQL
    assert "AVG(accuracy) AS system_avg" in _SUMMARY_AVERAGES_SQL
    # O SQL de componentes continua limitado à turma do relatório (referência local).
    assert "COALESCE(ass.classroom_id, CAST(:cid AS uuid)) = CAST(:cid AS uuid)" in _COMPONENT_RESULTS_SQL
    assert "COALESCE(ass.classroom_id, CAST(:cid AS uuid)) = CAST(:cid AS uuid)" not in _SUMMARY_AVERAGES_SQL
