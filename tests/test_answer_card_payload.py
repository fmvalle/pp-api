from app.v1.answer_card_pdf import build_answer_card_qr_payload


def test_answer_card_qr_payload_escapes_pipes():
    row = {
        "ra": "1|2",
        "code": "ABC",
        "school": "Escola X",
        "title": "Título",
        "grade": "5º",
        "classroom": "Turma A",
    }
    assert build_answer_card_qr_payload(row) == "1 2|ABC|Escola X|Título|5º|Turma A"
