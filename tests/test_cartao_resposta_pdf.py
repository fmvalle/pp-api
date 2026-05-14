from app.v1.cartao_resposta_pdf import (
    build_cartao_resposta_pdf_bytes_from_view_row,
    merge_pdf_bytes,
    suggested_download_filename,
)


def _sample_row():
    return {
        "agendamento": "00000000-0000-0000-0000-000000000001",
        "caderno": "Avaliação X",
        "ano_serie": "5º ano",
        "turma": "A",
        "estudante": "Maria Silva",
        "ra_codigo": "RA123",
        "codigo_cartao": "CARD01",
        "escola": "Escola Y",
        "qr_code_text": "RA123|CARD01|Avaliação X|Escola Y|5º ano|A",
        "titulo": "CARTÃO-RESPOSTA | Avaliação X",
        "logo_url": None,
        "output": "cartao_resposta_CARD01.pdf",
    }


def test_build_cartao_resposta_pdf_non_trivial():
    pdf = build_cartao_resposta_pdf_bytes_from_view_row(_sample_row())
    assert isinstance(pdf, (bytes, bytearray))
    assert len(pdf) > 2000
    assert pdf[:4] == b"%PDF"


def test_merge_two_pages():
    row1 = dict(_sample_row())
    row1["codigo_cartao"] = "C1"
    row1["estudante"] = "A"
    row2 = dict(_sample_row())
    row2["codigo_cartao"] = "C2"
    row2["estudante"] = "B"
    merged = merge_pdf_bytes(
        [
            build_cartao_resposta_pdf_bytes_from_view_row(row1),
            build_cartao_resposta_pdf_bytes_from_view_row(row2),
        ]
    )
    assert merged[:4] == b"%PDF"
    assert len(merged) > len(build_cartao_resposta_pdf_bytes_from_view_row(row1))


def test_suggested_download_filename_uses_output():
    row = _sample_row()
    assert "cartao_resposta" in suggested_download_filename(row, fallback_code="x")


def test_suggested_download_filename_fallback():
    row = {k: v for k, v in _sample_row().items() if k not in ("output", "codigo_cartao")}
    name = suggested_download_filename(row, fallback_code="ABC")
    assert name.endswith(".pdf")
    assert "ABC" in name
