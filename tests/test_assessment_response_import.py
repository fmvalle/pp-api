from app.v1.assessment_response_import_parser import parse_tabular_bytes
from app.v1.assessment_response_import_service import (
    parse_answer,
    normalize_codigo_cartao,
    normalize_ra,
    parse_grade_year,
    ras_equivalent,
    suggest_caderno_assessments,
)


def test_normalize_codigo_cartao():
    assert normalize_codigo_cartao(" ab12 ") == "AB12"


def test_normalize_ra_preserves_leading_zeros():
    assert normalize_ra(" 00123 ") == "00123"


def test_ras_equivalent_numeric_leading_zeros():
    assert ras_equivalent("18015610", "018015610") is True
    assert ras_equivalent("00123", "00123") is True
    assert ras_equivalent("ABC1", "ABC01") is False
    assert ras_equivalent("", "123") is False
    assert ras_equivalent(None, "123") is False


def test_parse_answer_valid():
    assert parse_answer("a") == ("A", False)
    assert parse_answer(" B ") == ("B", False)


def test_parse_answer_blank():
    assert parse_answer("") == (None, False)
    assert parse_answer(None) == (None, False)


def test_parse_answer_invalid():
    assert parse_answer("AB") == (None, True)
    assert parse_answer("A/B") == (None, True)


def test_parse_answer_na_as_blank():
    assert parse_answer("NA") == (None, False)
    assert parse_answer("n/a") == (None, False)


def test_parse_grade_year_from_name():
    assert parse_grade_year("2: ANO") == 2
    assert parse_grade_year("5º ano") == 5
    assert parse_grade_year(9) == 9
    assert parse_grade_year("EM") is None


def test_suggest_caderno_assessments():
    rows = [
        {"codigo_cartao": "9BFFCA3F", "ano_detectado": "2", "caderno": "Caderno 1"},
        {"codigo_cartao": "AA220D17", "ano_detectado": "2", "caderno": "Caderno 1"},
    ]
    index = {
        "9BFFCA3F": {"assessment_id": "6412fabd-41ce-4190-8e9d-f2e49cc1d4a7"},
        "AA220D17": {"assessment_id": "6412fabd-41ce-4190-8e9d-f2e49cc1d4a7"},
    }
    out = suggest_caderno_assessments(rows, index)
    assert out["2|caderno 1"] == "6412fabd-41ce-4190-8e9d-f2e49cc1d4a7"


def test_parse_csv_semicolon_delimiter():
    csv_text = (
        "codigo_cartao;RA;ESCOLA;Q001;Q002\n"
        "9BFFCA3F;18015610;ACESSO;B;C\n"
    ).encode("utf-8")
    headers, rows, _extras = parse_tabular_bytes(csv_text, "import.csv")
    assert "codigo_cartao" in headers
    assert rows[0]["codigo_cartao"] == "9BFFCA3F"
    assert rows[0]["ra"] == "18015610"
    assert rows[0]["Q001"] == "B"
