"""Testes da camada de dados do Avaliador."""

from app.v1.bot_data import _student_average_direction, data_unavailable_result
from app.v1.bot_local import looks_like_data_question, normalize_text


def test_looks_like_data_question_menor_media_aluno():
    assert looks_like_data_question("Qual aluno tem a menor média?")
    assert looks_like_data_question("qual aluno tem a menor media")


def test_student_average_direction_min():
    text = normalize_text("Qual aluno tem a menor média?")
    assert _student_average_direction(text) == "min"


def test_student_average_direction_max():
    text = normalize_text("Qual estudante tem a maior média na turma?")
    assert _student_average_direction(text) == "max"


def test_student_average_direction_none_for_turma_media():
    text = normalize_text("Qual a média da turma em matemática?")
    assert _student_average_direction(text) is None


def test_data_unavailable_result_has_intent_key():
    result = data_unavailable_result()
    assert result.intent_key == "data_unavailable"
    assert "Não encontrei dados" in result.reply


def test_extract_assessment_hint_none_without_caderno():
    from app.v1.bot_data import _extract_assessment_hint

    assert _extract_assessment_hint("Qual o nome do aluno com a maior nota?") is None


def test_extract_assessment_hint_with_caderno():
    from app.v1.bot_data import _extract_assessment_hint

    hint = _extract_assessment_hint('Maior nota na avaliação "Matemática 1"')
    assert hint == "Matemática 1"


def test_looks_like_schedule_question():
    from app.v1.bot_data import looks_like_schedule_question

    assert looks_like_schedule_question("Qual a data da avaliação de matemática?")
    assert looks_like_schedule_question("Qual foi a primeira avaliação aplicada?")
    assert looks_like_schedule_question("Quando é a próxima prova?")
    assert not looks_like_schedule_question("Como interpretar o relatório pedagógico?")
