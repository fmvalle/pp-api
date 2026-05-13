"""Cartão de resposta: modelo PDF + substituição do QR (canto superior direito) e texto do cabeçalho.

Coordenadas calibradas para `app/v1/assets/Cartao_Resposta-modelo.pdf` (página 596×842 pt).
Imports lazy para não atrasar o arranque do processo.
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

# Regiões no PDF modelo (pt, origem topo-esquerda)
_QR_CLEAR = (462, 202, 538, 278)
_QR_INSERT = (465, 205, 535, 275)
_HEADER_CLEAR = (86, 44, 448, 198)
_TEXT_X = 92.0
_TEXT_Y0 = 56.0
_FONT = "helv"
_FONT_SIZE = 8.5
_LINE = 11.0


def _seg(v: Any) -> str:
    s = "" if v is None else str(v).strip()
    return s.replace("|", " ").replace("\r", " ").replace("\n", " ")


def build_answer_card_qr_payload(row: dict[str, Any]) -> str:
    """Formato pedido: ra|code|school|title|grade|classroom."""
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


def _header_lines(row: dict[str, Any]) -> list[str]:
    return [
        f"RA: {_seg(row.get('ra')) or '—'}",
        f"Código: {_seg(row.get('code')) or '—'}",
        f"Escola: {_seg(row.get('school')) or '—'}",
        f"Avaliação: {_seg(row.get('title')) or '—'}",
        f"Série: {_seg(row.get('grade')) or '—'}",
        f"Turma: {_seg(row.get('classroom')) or '—'}",
    ]


def _default_template_path() -> Path:
    return Path(__file__).resolve().parent / "assets" / "Cartao_Resposta-modelo.pdf"


def resolve_answer_card_template_path(configured: str | None) -> Path | None:
    if configured and str(configured).strip():
        p = Path(configured).expanduser()
        if not p.is_absolute():
            p = Path.cwd() / p
        return p if p.is_file() else None
    d = _default_template_path()
    return d if d.is_file() else None


def build_answer_card_pdf_bytes(
    *,
    template_path: str | Path,
    row: dict[str, Any],
) -> bytes:
    import fitz
    import qrcode
    from PIL import Image

    tpl = Path(template_path)
    if not tpl.is_file():
        raise FileNotFoundError(f"Modelo PDF não encontrado: {tpl}")

    payload = build_answer_card_qr_payload(row)
    img = qrcode.make(
        payload,
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
    ).convert("RGB")
    bio = io.BytesIO()
    img.save(bio, format="PNG", optimize=True)
    png = bio.getvalue()

    doc = fitz.open(tpl)
    try:
        page = doc[0]
        # Limpa faixa do cabeçalho (texto impresso no modelo) e área do QR antigo
        hx0, hy0, hx1, hy1 = _HEADER_CLEAR
        page.draw_rect(
            fitz.Rect(hx0, hy0, hx1, hy1),
            color=(1, 1, 1),
            fill=(1, 1, 1),
            width=0,
        )
        qx0, qy0, qx1, qy1 = _QR_CLEAR
        page.draw_rect(
            fitz.Rect(qx0, qy0, qx1, qy1),
            color=(1, 1, 1),
            fill=(1, 1, 1),
            width=0,
        )
        ix0, iy0, ix1, iy1 = _QR_INSERT
        page.insert_image(fitz.Rect(ix0, iy0, ix1, iy1), stream=png, keep_proportion=True)

        y = _TEXT_Y0
        for line in _header_lines(row):
            page.insert_text(
                fitz.Point(_TEXT_X, y),
                line,
                fontname=_FONT,
                fontsize=_FONT_SIZE,
                color=(0, 0, 0),
            )
            y += _LINE

        return doc.tobytes(deflate=True, garbage=3, clean=True)
    finally:
        doc.close()
