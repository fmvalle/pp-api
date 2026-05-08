"""Geração de PDF da lista de presença (cabeçalho + tabela com assinatura).

Imports do ReportLab são lazy dentro de `build_attendance_sheet_pdf_bytes` para não
atrasar o arranque do contentor (ex.: Cloud Run / health na PORT).
"""

from __future__ import annotations

import json
from datetime import datetime
from io import BytesIO
from typing import Any


def _fmt_dt(iso_val: Any) -> str:
    if not iso_val:
        return "—"
    try:
        d = datetime.fromisoformat(str(iso_val).replace("Z", "+00:00"))
        return d.strftime("%d/%m/%Y %H:%M")
    except Exception:
        return str(iso_val)


def _student_doc(row: dict[str, Any]) -> str:
    code = row.get("code")
    if code:
        return str(code)
    meta = row.get("metadata")
    if isinstance(meta, str) and meta.strip():
        try:
            meta = json.loads(meta)
        except json.JSONDecodeError:
            meta = {}
    if isinstance(meta, dict):
        for k in ("document", "ra", "doc", "cpf"):
            v = meta.get(k)
            if v:
                return str(v)
    return "—"


def _student_name(row: dict[str, Any]) -> str:
    fn = row.get("full_name")
    if fn:
        return str(fn)
    return "—"


def build_attendance_sheet_pdf_bytes(
    *,
    school_name: str,
    classroom_name: str,
    classroom_code: str | None,
    assessment_title: str,
    schedule_start: Any,
    schedule_end: Any,
    academic_year_label: str | None,
    school_id: str | None,
    classroom_id: str | None,
    assessment_id: str | None,
    students: list[dict[str, Any]],
) -> bytes:
    """Monta PDF A4 em memória."""
    from reportlab.graphics.barcode import qr
    from reportlab.graphics.shapes import Drawing
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import cm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=1.5 * cm,
        rightMargin=1.5 * cm,
        topMargin=1.2 * cm,
        bottomMargin=1.2 * cm,
        title="Lista de presença",
    )
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        name="HeadTitle",
        parent=styles["Title"],
        fontSize=16,
        spaceAfter=8,
        textColor=colors.HexColor("#1a237e"),
    )
    sub_style = ParagraphStyle(
        name="HeadSub",
        parent=styles["Normal"],
        fontSize=9,
        textColor=colors.HexColor("#424242"),
        leading=12,
    )

    story: list[Any] = []
    story.append(Paragraph("Lista de presença — Avaliação", title_style))
    qr_payload = f"{school_id or '—'}|{classroom_id or '—'}|{assessment_id or '—'}"
    qr_widget = qr.QrCodeWidget(qr_payload)
    q_bounds = qr_widget.getBounds()
    q_size = 2.6 * cm
    q_width = max(q_bounds[2] - q_bounds[0], 1)
    q_height = max(q_bounds[3] - q_bounds[1], 1)
    qr_drawing = Drawing(q_size, q_size, transform=[q_size / q_width, 0, 0, q_size / q_height, 0, 0])
    qr_drawing.add(qr_widget)
    meta_lines = [
        f"<b>Escola:</b> {school_name}",
        f"<b>Turma:</b> {classroom_name}"
        + (f" &nbsp;|&nbsp; <b>Código:</b> {classroom_code}" if classroom_code else ""),
        f"<b>Avaliação:</b> {assessment_title}",
        f"<b>Início:</b> {_fmt_dt(schedule_start)} &nbsp;|&nbsp; <b>Fim:</b> {_fmt_dt(schedule_end)}",
    ]
    if academic_year_label:
        meta_lines.append(f"<b>Ano letivo:</b> {academic_year_label}")
    header_table = Table(
        [[Paragraph("<br/>".join(meta_lines), sub_style), qr_drawing]],
        colWidths=[doc.width - (q_size + 0.25 * cm), q_size],
    )
    header_table.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("ALIGN", (1, 0), (1, 0), "RIGHT"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
            ]
        )
    )
    story.append(header_table)
    story.append(Spacer(1, 0.4 * cm))

    # Cabeçalho da tabela
    header = ["RA / Documento", "Nome do aluno", "Assinatura"]
    data: list[list[str]] = [header]
    for row in sorted(students, key=lambda r: _student_name(r).lower()):
        data.append([_student_doc(row), _student_name(row), ""])

    col_widths = [3.2 * cm, 8.5 * cm, 5.3 * cm]
    t = Table(data, colWidths=col_widths, repeatRows=1)
    t.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e8eaf6")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#1a237e")),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, -1), 9),
                ("ALIGN", (0, 0), (-1, -1), "LEFT"),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#bdbdbd")),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#fafafa")]),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                ("LEFTPADDING", (0, 0), (-1, -1), 6),
                ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                ("MINIMUMHEIGHT", (0, 1), (-1, -1), 28),
            ]
        )
    )
    story.append(t)
    doc.build(story)
    out = buf.getvalue()
    buf.close()
    return out
