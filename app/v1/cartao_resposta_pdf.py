"""Cartão-resposta em PDF (ReportLab), alinhado à view `vw_student_assessment` e ao script
`pdf/generate_cartao_resposta_atualizado.py`.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from typing import Any, Mapping, Optional
from urllib.parse import urlparse
from urllib.request import Request, urlopen

import qrcode
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas


def _seg(v: Any) -> str:
    s = "" if v is None else str(v).strip()
    return s.replace("\r", " ").replace("\n", " ")


@dataclass
class CartaoRespostaData:
    caderno: str
    ano_serie: str
    turma: str
    estudante: str
    ra_codigo: str
    codigo_cartao: str
    escola: str
    qr_code_text: str
    titulo: str = "CARTÃO-RESPOSTA | Avaliação de Desempenho 5º ano"
    total_questoes: int = 45
    alternativas: Optional[list[str]] = None
    logo_url: Optional[str] = None
    logo_left_url: Optional[str] = None
    answer_grid_top_offset_px: float = 30
    bubble_offset_x_px: float = 5


def mm_to_pt(value: float) -> float:
    return value * mm


def is_http_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"}


def load_image_reader(source: str) -> ImageReader:
    if is_http_url(source):
        request = Request(
            source,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        with urlopen(request, timeout=20) as response:
            image_bytes = response.read()
        return ImageReader(io.BytesIO(image_bytes))
    return ImageReader(source)


def generate_qrcode_image(text: str) -> ImageReader:
    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=10,
        border=1,
    )
    qr.add_data(text)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    return ImageReader(img.convert("RGB"))


def draw_centered_text(c, text, x, y, font_name="Helvetica-Bold", font_size=12):
    c.setFont(font_name, font_size)
    text_width = c.stringWidth(text, font_name, font_size)
    c.drawString(x - text_width / 2, y, text)


def draw_logos(c, data: CartaoRespostaData, page_width: float, page_height: float):
    logo_source = data.logo_url or data.logo_left_url
    if not logo_source:
        return
    try:
        y = page_height - mm_to_pt(29)
        logo_w = mm_to_pt(48)
        logo_h = mm_to_pt(16)
        logo_x = (page_width - logo_w) / 2
        logo_y = y - mm_to_pt(9)
        logo = load_image_reader(logo_source)
        c.drawImage(
            logo,
            logo_x,
            logo_y,
            width=logo_w,
            height=logo_h,
            preserveAspectRatio=True,
            anchor="c",
            mask="auto",
        )
    except Exception:
        return
def draw_header(c, data: CartaoRespostaData, page_width: float, page_height: float):
    draw_logos(c, data, page_width, page_height)
    c.setFillColor(colors.black)
    draw_centered_text(
        c,
        data.titulo,
        page_width / 2,
        page_height - mm_to_pt(43),
        "Helvetica-Bold",
        12,
    )


def draw_student_box(c, data: CartaoRespostaData, page_width: float, page_height: float):
    box_x = mm_to_pt(24)
    box_y = page_height - mm_to_pt(97)
    box_w = mm_to_pt(129)
    box_h = mm_to_pt(30)
    c.setStrokeColor(colors.black)
    c.setLineWidth(1)
    c.rect(box_x, box_y, box_w, box_h, stroke=1, fill=0)
    lines = [
        f"CADERNO: {data.caderno}",
        f"ANO/SÉRIE: {data.ano_serie}",
        f"TURMA: {data.turma}",
        f"ESTUDANTE: {data.estudante}",
        f"RA/CÓDIGO: {data.ra_codigo}",
        f"CÓDIGO DO CARTÃO: {data.codigo_cartao}",
        f"ESCOLA: {data.escola}",
    ]
    c.setFont("Helvetica-Bold", 7.5)
    text_x = box_x + mm_to_pt(3)
    text_y = box_y + box_h - mm_to_pt(5)
    for line in lines:
        c.drawString(text_x, text_y, line)
        text_y -= mm_to_pt(3.8)
    qr_img = generate_qrcode_image(data.qr_code_text)
    qr_size = mm_to_pt(30)
    qr_x = page_width - mm_to_pt(55)
    qr_y = box_y - mm_to_pt(2)
    c.drawImage(qr_img, qr_x, qr_y, width=qr_size, height=qr_size)


def draw_half_filled_circle(c, x: float, y: float, radius: float):
    c.setStrokeColor(colors.black)
    c.setFillColor(colors.white)
    c.circle(x, y, radius, stroke=1, fill=1)
    c.setFillColor(colors.black)
    c.rect(x - radius * 0.85, y + radius * 0.05, radius * 1.7, radius * 0.42, stroke=0, fill=1)
    c.setFillColor(colors.black)


def draw_x_circle(c, x: float, y: float, radius: float):
    c.setStrokeColor(colors.black)
    c.setFillColor(colors.white)
    c.circle(x, y, radius, stroke=1, fill=0)
    line_offset = radius * 0.9
    c.setLineWidth(1.4)
    c.line(x - line_offset, y - line_offset, x + line_offset, y + line_offset)
    c.line(x - line_offset, y + line_offset, x + line_offset, y - line_offset)
    c.setLineWidth(1)


def draw_target_circle(c, x: float, y: float, radius: float):
    c.setStrokeColor(colors.black)
    c.setFillColor(colors.white)
    c.circle(x, y, radius, stroke=1, fill=0)
    c.circle(x, y, radius * 0.45, stroke=1, fill=0)


def draw_example_box(c, page_width: float, _page_height: float, y_reference: float):
    example_w = mm_to_pt(71.2)
    example_h = mm_to_pt(14)
    example_right_margin = mm_to_pt(25)
    example_x = page_width - example_right_margin - example_w
    example_y = y_reference - mm_to_pt(12)
    c.setStrokeColor(colors.gray)
    c.setLineWidth(1)
    c.rect(example_x, example_y, example_w, example_h, stroke=1, fill=0)
    c.setFillColor(colors.black)
    c.setFont("Helvetica-Bold", 6.7)
    c.drawString(example_x + mm_to_pt(6), example_y + example_h - mm_to_pt(4.5), "FAÇA ASSIM")
    c.drawString(example_x + mm_to_pt(36), example_y + example_h - mm_to_pt(4.5), "NÃO FAÇA ASSIM")
    cy = example_y + mm_to_pt(4.2)
    radius = mm_to_pt(2.8)
    c.setFillColor(colors.black)
    c.circle(example_x + mm_to_pt(16), cy, radius, stroke=1, fill=1)
    draw_half_filled_circle(c, example_x + mm_to_pt(38), cy, radius)
    draw_x_circle(c, example_x + mm_to_pt(51), cy, radius)
    draw_target_circle(c, example_x + mm_to_pt(64), cy, radius)
    c.setFillColor(colors.black)


def draw_instructions(c, page_width: float, page_height: float):
    x = mm_to_pt(26)
    y = page_height - mm_to_pt(115)
    c.setFont("Helvetica", 7.5)
    c.drawString(x, y, "Não amasse, não dobre, não suje e não rasure esta folha.")
    c.setFont("Helvetica-Bold", 7.5)
    c.drawString(x, y - mm_to_pt(4.5), "Utilize somente caneta esferográfica azul ou preta.")
    c.setFont("Helvetica", 7.5)
    c.drawString(x, y - mm_to_pt(9), "Preencha completamente o círculo da resposta como no exemplo:")
    draw_example_box(c, page_width, page_height, y)


def draw_corner_marker(c, x: float, y: float, size: float = 8 * mm):
    c.setFillColor(colors.black)
    c.rect(x, y, size, size, stroke=0, fill=1)
    c.setFillColor(colors.white)
    small = size * 0.28
    c.rect(x + size * 0.18, y + size * 0.55, small, small, stroke=0, fill=1)
    c.rect(x + size * 0.52, y + size * 0.18, small, small, stroke=0, fill=1)
    c.rect(x + size * 0.52, y + size * 0.52, small, small, stroke=0, fill=1)
    c.setFillColor(colors.black)


def draw_markers(c, page_width: float, page_height: float):
    left_x = mm_to_pt(26)
    right_x = page_width - mm_to_pt(34)
    top_y = page_height - mm_to_pt(139)
    bottom_y = mm_to_pt(33)
    draw_corner_marker(c, left_x, top_y)
    draw_corner_marker(c, right_x, top_y)
    draw_corner_marker(c, left_x, bottom_y)
    draw_corner_marker(c, right_x, bottom_y)


def draw_bubble(c, x: float, y: float, label: str):
    radius = mm_to_pt(2.6)
    c.setStrokeColor(colors.gray)
    c.setLineWidth(0.8)
    c.circle(x, y, radius, stroke=1, fill=0)
    c.setFillColor(colors.gray)
    c.setFont("Helvetica-Bold", 5.5)
    text_width = c.stringWidth(label, "Helvetica-Bold", 5.5)
    c.drawString(x - text_width / 2, y - mm_to_pt(0.9), label)
    c.setFillColor(colors.black)


def draw_question_row(
    c,
    number: int,
    x: float,
    y: float,
    alternativas: list[str],
    bubble_offset_x_px: float = 5,
):
    c.setFont("Helvetica-Bold", 10)
    c.drawRightString(x, y - mm_to_pt(1.5), f"{number:02d}")
    start_x = x + mm_to_pt(8) + bubble_offset_x_px
    gap = mm_to_pt(8.8)
    for index, alt in enumerate(alternativas):
        draw_bubble(c, start_x + index * gap, y, alt)


def draw_answer_grid(
    c,
    data: CartaoRespostaData,
    page_width: float,
    page_height: float,
    top_offset_px: Optional[float] = None,
    bubble_offset_x_px: Optional[float] = None,
):
    alternativas = data.alternativas or ["A", "B", "C", "D"]
    top_adjust = data.answer_grid_top_offset_px if top_offset_px is None else top_offset_px
    bubble_adjust = data.bubble_offset_x_px if bubble_offset_x_px is None else bubble_offset_x_px
    columns = [
        {"start": 1, "end": 15, "x": mm_to_pt(37), "y": page_height - mm_to_pt(157) + top_adjust},
        {"start": 16, "end": 30, "x": mm_to_pt(85), "y": page_height - mm_to_pt(157) + top_adjust},
        {"start": 31, "end": 45, "x": mm_to_pt(133), "y": page_height - mm_to_pt(157) + top_adjust},
    ]
    row_gap = mm_to_pt(7.2)
    table_width = mm_to_pt(35)
    table_height = row_gap * 15 + mm_to_pt(1.5)
    for col in columns:
        start = col["start"]
        end = min(col["end"], data.total_questoes)
        if start > data.total_questoes:
            continue
        x = col["x"]
        y = col["y"]
        rect_x = x + mm_to_pt(5)
        rect_y = y - row_gap * (end - start + 1) + mm_to_pt(2)
        c.setStrokeColor(colors.black)
        c.setLineWidth(0.7)
        c.rect(rect_x, rect_y, table_width, table_height, stroke=1, fill=0)
        for q in range(start, end + 1):
            row_index = q - start
            row_y = y - row_index * row_gap
            draw_question_row(
                c,
                q,
                x,
                row_y,
                alternativas,
                bubble_offset_x_px=bubble_adjust,
            )


def create_cartao_resposta_pdf_bytes(data: CartaoRespostaData) -> bytes:
    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=A4)
    page_width, page_height = A4
    c.setTitle("Cartão-resposta")
    c.setAuthor("Gerado pela API")
    draw_header(c, data, page_width, page_height)
    draw_student_box(c, data, page_width, page_height)
    draw_instructions(c, page_width, page_height)
    draw_markers(c, page_width, page_height)
    draw_answer_grid(c, data, page_width, page_height)
    c.showPage()
    c.save()
    return buffer.getvalue()


def view_row_to_cartao_resposta_data(row: Mapping[str, Any]) -> CartaoRespostaData:
    logo = row.get("logo_url")
    if logo is not None:
        logo = str(logo).strip() or None
    tq = row.get("total_questoes")
    try:
        total = int(tq) if tq is not None else 45
    except (TypeError, ValueError):
        total = 45
    titulo = _seg(row.get("titulo")) or "CARTÃO-RESPOSTA"
    return CartaoRespostaData(
        caderno=_seg(row.get("caderno")),
        ano_serie=_seg(row.get("ano_serie")),
        turma=_seg(row.get("turma")),
        estudante=_seg(row.get("estudante")),
        ra_codigo=_seg(row.get("ra_codigo")),
        codigo_cartao=_seg(row.get("codigo_cartao")),
        escola=_seg(row.get("escola")),
        qr_code_text=_seg(row.get("qr_code_text")),
        titulo=titulo,
        total_questoes=total,
        logo_url=logo,
    )


def build_cartao_resposta_pdf_bytes_from_view_row(row: Mapping[str, Any]) -> bytes:
    return create_cartao_resposta_pdf_bytes(view_row_to_cartao_resposta_data(row))


def suggested_download_filename(row: Mapping[str, Any], *, fallback_code: str) -> str:
    raw = row.get("output")
    name = _seg(raw) if raw is not None else ""
    if name and name.lower().endswith(".pdf"):
        safe = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in name)[:120]
        return safe or f"cartao-resposta-{fallback_code}.pdf"
    code = _seg(row.get("codigo_cartao")) or fallback_code
    safe_code = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in code)[:80] or "cartao"
    return f"cartao-resposta-{safe_code}.pdf"


def merge_pdf_bytes(parts: list[bytes]) -> bytes:
    if not parts:
        return b""
    import fitz

    merged = fitz.open()
    try:
        for b in parts:
            with fitz.open(stream=b, filetype="pdf") as src:
                merged.insert_pdf(src)
        return merged.tobytes(deflate=True, garbage=3, clean=True)
    finally:
        merged.close()
