"""Utilitários legados do formato do payload do QR (testes / compatibilidade).

A geração do PDF passou a ser feita em `cartao_resposta_pdf.py` (ReportLab + dados da view).
"""

from __future__ import annotations

from typing import Any


def _seg(v: Any) -> str:
    s = "" if v is None else str(v).strip()
    return s.replace("|", " ").replace("\r", " ").replace("\n", " ")


def build_answer_card_qr_payload(row: dict[str, Any]) -> str:
    """Formato: ra|code|school|title|grade|classroom (linhas antigas da view)."""
    return "|".join(
        [
            _seg(row.get("ra")),
            _seg(row.get("code")),
            _seg(row.get("school")),
            _seg(row.get("title")),
            _seg(row.get("grade")),
            _seg(row.get("classroom")),
        ]
    )
