"""Standardized invoice PDF (ReportLab). One template for every invoice."""
from __future__ import annotations

import io
import re
from datetime import datetime
from pathlib import Path
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas
from reportlab.platypus import (
    HRFlowable, KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
)

from .checks import validate
from .models import InvoiceData
from .numbers import format_amount, format_percent, to_decimal

FONT_DIR = Path(__file__).parent / "fonts"
ACCENT = colors.HexColor("#1F3A5F")
MUTED = colors.HexColor("#5B6573")
RULE = colors.HexColor("#C9D0DA")
HEAD_BG = colors.HexColor("#EEF1F5")
FLAG = "#B45309"

_FONTS_READY = False
FONT, FONT_B, FONT_I = "Helvetica", "Helvetica-Bold", "Helvetica-Oblique"


def _register_fonts() -> None:
    global _FONTS_READY, FONT, FONT_B, FONT_I
    if _FONTS_READY:
        return
    try:
        pdfmetrics.registerFont(TTFont("LibSans", str(FONT_DIR / "LiberationSans-Regular.ttf")))
        pdfmetrics.registerFont(TTFont("LibSans-Bold", str(FONT_DIR / "LiberationSans-Bold.ttf")))
        pdfmetrics.registerFont(TTFont("LibSans-Italic", str(FONT_DIR / "LiberationSans-Italic.ttf")))
        pdfmetrics.registerFont(TTFont("LibSans-BoldItalic", str(FONT_DIR / "LiberationSans-BoldItalic.ttf")))
        pdfmetrics.registerFontFamily("LibSans", normal="LibSans", bold="LibSans-Bold",
                                      italic="LibSans-Italic", boldItalic="LibSans-BoldItalic")
        FONT, FONT_B, FONT_I = "LibSans", "LibSans-Bold", "LibSans-Italic"
    except Exception:  # fall back to built-in Helvetica (Latin-1 only)
        pass
    _FONTS_READY = True


def _styles() -> dict[str, ParagraphStyle]:
    base = ParagraphStyle("base", fontName=FONT, fontSize=9, leading=12, textColor=colors.HexColor("#1B2430"))
    return {
        "base": base,
        "title": ParagraphStyle("title", parent=base, fontName=FONT_B, fontSize=21, leading=25, textColor=ACCENT),
        "note": ParagraphStyle("note", parent=base, fontName=FONT_I, fontSize=8, leading=10.5, textColor=MUTED),
        "label": ParagraphStyle("label", parent=base, fontName=FONT_B, fontSize=7.5, leading=10, textColor=MUTED),
        "name": ParagraphStyle("name", parent=base, fontName=FONT_B, fontSize=10.5, leading=13.5),
        "cell": ParagraphStyle("cell", parent=base, fontSize=9, leading=11.5),
        "cellr": ParagraphStyle("cellr", parent=base, fontSize=9, leading=11.5, alignment=TA_RIGHT),
        "head": ParagraphStyle("head", parent=base, fontName=FONT_B, fontSize=8.5, leading=10.5, textColor=ACCENT),
        "headr": ParagraphStyle("headr", parent=base, fontName=FONT_B, fontSize=8.5, leading=10.5,
                                textColor=ACCENT, alignment=TA_RIGHT),
        "h2": ParagraphStyle("h2", parent=base, fontName=FONT_B, fontSize=9, leading=12, textColor=ACCENT),
        "small": ParagraphStyle("small", parent=base, fontSize=8, leading=10.5, textColor=MUTED),
        "totl": ParagraphStyle("totl", parent=base, fontName=FONT_B, fontSize=10.5, leading=13),
        "totr": ParagraphStyle("totr", parent=base, fontName=FONT_B, fontSize=10.5, leading=13, alignment=TA_RIGHT),
    }


def esc(text: str) -> str:
    return escape(text or "").replace("\n", "<br/>")


def flagged(text: str = "Not identified") -> str:
    return f'<font color="{FLAG}"><i>{esc(text)}</i></font>'


def _value_or_flag(value: str) -> str:
    return esc(value.strip()) if value and value.strip() else flagged()


class _NumberedCanvas(canvas.Canvas):
    """Adds 'Page x of y' and the standing disclaimer to every page."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._saved: list[dict] = []

    def showPage(self):
        self._saved.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        total = len(self._saved)
        for state in self._saved:
            self.__dict__.update(state)
            self._footer(total)
            super().showPage()
        super().save()

    def _footer(self, total: int) -> None:
        w, _ = A4
        self.setStrokeColor(RULE)
        self.setLineWidth(0.5)
        self.line(18 * mm, 14 * mm, w - 18 * mm, 14 * mm)
        self.setFont(FONT, 7.5)
        self.setFillColor(MUTED)
        self.drawString(18 * mm, 10 * mm,
                        "Standardized representation of an original invoice - not an invoice issued by the supplier.")
        self.drawRightString(w - 18 * mm, 10 * mm, f"Page {self._pageNumber} of {total}")


def standardized_filename(data: InvoiceData, original_filename: str | None = None) -> str:
    number = re.sub(r"[^A-Za-z0-9._-]+", "-", (data.invoice_number or "").strip()).strip("-.")
    if number:
        return f"Standardized_Invoice_{number}.pdf"
    stem = Path(original_filename or "").stem
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", stem).strip("-.")
    return f"Standardized_Invoice_NO-NUMBER{('_' + stem) if stem else ''}.pdf"


def _fmt_qty(v: str) -> str:
    d = to_decimal(v)
    if d is not None and d == d.to_integral_value():
        return format_amount(str(d.to_integral_value()), min_decimals_if_fractional=False)  # "15.00" -> "15"
    return format_amount(v, min_decimals_if_fractional=False)


def _fmt_discount(v: str) -> str:
    v = v.strip()
    if not v:
        return ""
    if v.endswith("%"):
        return format_percent(v)
    return format_amount(v)


CALC_CODES = {"line_mismatch", "subtotal_mismatch", "tax_mismatch", "total_mismatch", "balance_mismatch", "bad_number"}


def build_pdf(
    data: InvoiceData,
    *,
    original_filename: str | None = None,
    original_pages: int | None = None,
    generated_at: datetime | None = None,
) -> bytes:
    _register_fonts()
    st = _styles()
    generated_at = generated_at or datetime.now()
    buf = io.BytesIO()
    page_w, _ = A4
    margin = 18 * mm
    width = page_w - 2 * margin - 12  # minus the frame's 6pt padding on both sides
    doc = SimpleDocTemplate(
        buf, pagesize=A4, leftMargin=margin, rightMargin=margin, topMargin=16 * mm, bottomMargin=20 * mm,
        title=f"Standardized Invoice {data.invoice_number}".strip(), author="Invoice Standardizer",
        subject="Standardized representation of an original invoice",
    )
    flow = []

    # 1. Title ----------------------------------------------------------------------
    flow.append(Paragraph("STANDARDIZED INVOICE", st["title"]))
    flow.append(Spacer(1, 2))
    flow.append(Paragraph(
        "This document is a standardized representation of the information found in the original invoice. "
        "It is not a new official invoice issued by the supplier.", st["note"]))
    flow.append(Spacer(1, 4))
    flow.append(HRFlowable(width="100%", thickness=1.2, color=ACCENT, spaceAfter=8))

    # 2. Identification ---------------------------------------------------------------
    meta_rows = [
        ("Original invoice no.", _value_or_flag(data.invoice_number)),
        ("Issue date", _value_or_flag(data.issue_date)),
    ]
    if data.due_date.strip():
        meta_rows.append(("Due date", esc(data.due_date.strip())))
    if data.po_number.strip():
        meta_rows.append(("Purchase order / reference", esc(data.po_number.strip())))
    if data.payment_terms.strip():
        meta_rows.append(("Payment terms", esc(data.payment_terms.strip())))
    meta_rows.append(("Currency", _value_or_flag(data.currency)))
    meta = Table(
        [[Paragraph(esc(k), st["label"]), Paragraph(v, st["base"])] for k, v in meta_rows],
        colWidths=[48 * mm, width - 48 * mm],
    )
    meta.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"), ("TOPPADDING", (0, 0), (-1, -1), 1.5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 1.5), ("LEFTPADDING", (0, 0), (-1, -1), 0),
    ]))
    flow.append(meta)
    flow.append(Spacer(1, 10))

    # 3. Parties ------------------------------------------------------------------------
    def party_block(title: str, party, essential: bool) -> list:
        out = [Paragraph(title, st["label"]), Spacer(1, 2)]
        if party.name.strip():
            out.append(Paragraph(esc(party.name.strip()), st["name"]))
        elif essential:
            out.append(Paragraph(flagged(), st["name"]))
        if party.address.strip():
            out.append(Paragraph(esc(party.address.strip()), st["base"]))
        if party.tax_id.strip():
            out.append(Paragraph(esc(party.tax_id.strip()), st["small"]))
        bank_rows = []
        if party.bank_name.strip():
            bank_rows.append(f"Bank: {party.bank_name.strip()}")
        if party.bank_account.strip():
            bank_rows.append(f"Account: {party.bank_account.strip()}")
        if party.iban.strip():
            bank_rows.append(f"IBAN: {party.iban.strip()}")
        if party.swift.strip():
            bank_rows.append(f"SWIFT/BIC: {party.swift.strip()}")
        if party.payment_method.strip():
            bank_rows.append(f"Payment method: {party.payment_method.strip()}")
        for row in bank_rows:
            out.append(Paragraph(esc(row), st["small"]))
        return out

    has_customer = any([
        data.customer.name.strip(), data.customer.address.strip(), data.customer.tax_id.strip(),
        data.customer.bank_name.strip(), data.customer.bank_account.strip(), data.customer.iban.strip(),
        data.customer.swift.strip(), data.customer.payment_method.strip(),
    ])
    supplier_cells = party_block("SUPPLIER", data.supplier, True)
    if has_customer:
        customer_cells = party_block("CUSTOMER", data.customer, False)
        parties = Table([[supplier_cells, customer_cells]], colWidths=[width / 2, width / 2])
    else:
        parties = Table([[supplier_cells]], colWidths=[width])
    parties.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8), ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
    ]))
    flow.append(parties)
    flow.append(Spacer(1, 12))

    # 4. Line items -----------------------------------------------------------------------
    cur = data.currency.strip()
    suffix = f" ({cur})" if cur else ""
    show_disc = any(i.discount.strip() for i in data.items)
    show_tax = any(i.tax_rate.strip() for i in data.items)
    show_qty = any(i.quantity.strip() for i in data.items) or not data.items
    show_unit = any(i.unit_price.strip() for i in data.items) or not data.items

    heads = [("Description", "head")]
    widths: list[float] = []
    if show_qty:
        heads.append(("Qty", "headr")); widths.append(18 * mm)
    if show_unit:
        heads.append((f"Unit price{suffix}", "headr")); widths.append(30 * mm)
    if show_disc:
        heads.append(("Discount", "headr")); widths.append(20 * mm)
    if show_tax:
        heads.append(("Tax", "headr")); widths.append(16 * mm)
    heads.append((f"Amount{suffix}", "headr")); widths.append(32 * mm)
    desc_w = width - sum(widths)
    col_widths = [desc_w] + widths

    flow.append(Paragraph("PRODUCTS AND SERVICES", st["h2"]))
    flow.append(Spacer(1, 3))
    if data.items:
        rows = [[Paragraph(esc(t), st[s]) for t, s in heads]]
        for it in data.items:
            row = [Paragraph(esc(it.description.strip()) or flagged("(no description)"), st["cell"])]
            if show_qty:
                row.append(Paragraph(esc(_fmt_qty(it.quantity)), st["cellr"]))
            if show_unit:
                row.append(Paragraph(esc(format_amount(it.unit_price)), st["cellr"]))
            if show_disc:
                row.append(Paragraph(esc(_fmt_discount(it.discount)), st["cellr"]))
            if show_tax:
                row.append(Paragraph(esc(format_percent(it.tax_rate)), st["cellr"]))
            row.append(Paragraph(esc(format_amount(it.amount)) or flagged("-"), st["cellr"]))
            rows.append(row)
        table = Table(rows, colWidths=col_widths, repeatRows=1)
        table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), HEAD_BG),
            ("LINEBELOW", (0, 0), (-1, 0), 0.8, ACCENT),
            ("LINEBELOW", (0, 1), (-1, -1), 0.4, RULE),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ("LEFTPADDING", (0, 0), (-1, -1), 4), ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ]))
        flow.append(table)
    else:
        flow.append(Paragraph(flagged("No line items were identified in the original invoice."), st["base"]))
    flow.append(Spacer(1, 12))

    # 5. Financial summary -------------------------------------------------------------------
    sum_rows: list[tuple[str, str, bool]] = []  # label, value, bold
    if data.subtotal.strip():
        sum_rows.append(("Subtotal", format_amount(data.subtotal), False))
    if data.discount_total.strip():
        sum_rows.append(("Discount", format_amount(data.discount_total), False))
    for c in data.additional_charges:
        if c.amount.strip():
            sum_rows.append((c.label.strip() or "Additional charge", format_amount(c.amount), False))
    for t in data.taxes:
        if not t.amount.strip():
            continue
        label = t.label.strip() or "Tax"
        if t.rate.strip():
            label += f" ({format_percent(t.rate)})"
        sum_rows.append((label, format_amount(t.amount), False))
    total_row_index = len(sum_rows)
    sum_rows.append(("Total", format_amount(data.total) if data.total.strip() else "", True))
    if data.amount_paid.strip():
        sum_rows.append(("Amount paid", format_amount(data.amount_paid), False))
    if data.balance_due.strip():
        sum_rows.append(("Balance due", format_amount(data.balance_due), False))

    sw = [58 * mm, 36 * mm]
    srows = []
    for label, value, bold in sum_rows:
        lab_style, val_style = (st["totl"], st["totr"]) if bold else (st["cell"], st["cellr"])
        val_html = esc(value) if value else flagged()
        if value and cur:
            val_html += f' <font size="7.5" color="#5B6573">{esc(cur)}</font>'
        srows.append([Paragraph(esc(label), lab_style), Paragraph(val_html, val_style)])
    summary = Table(srows, colWidths=sw, hAlign="RIGHT")
    summary.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 2.5), ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5),
        ("LINEABOVE", (0, total_row_index), (-1, total_row_index), 1.0, ACCENT),
        ("BACKGROUND", (0, total_row_index), (-1, total_row_index), HEAD_BG),
    ]))
    flow.append(KeepTogether([Paragraph("FINANCIAL SUMMARY", st["h2"]), Spacer(1, 3), summary]))
    if data.amount_in_words.strip():
        flow.append(Spacer(1, 6))
        flow.append(Paragraph(f"<b>Amount in words:</b> {esc(data.amount_in_words.strip())}", st["small"]))

    # 6. Verification notes (discrepancies are reported, never corrected) --------------------
    notes = [w for w in validate(data) if w.code in CALC_CODES and w.severity != "info"]
    if notes:
        flow.append(Spacer(1, 12))
        block = [
            Paragraph("VERIFICATION NOTES", st["h2"]), Spacer(1, 2),
            Paragraph("The checks below found differences in the source figures. The amounts above are reproduced "
                      "exactly as they appear in the original invoice and were not adjusted.", st["small"]),
            Spacer(1, 3),
        ]
        for n in notes:
            block.append(Paragraph("&bull; " + esc(n.message), st["small"]))
        flow.append(KeepTogether(block))

    # 7. Reference to the original ------------------------------------------------------------
    flow.append(Spacer(1, 14))
    flow.append(HRFlowable(width="100%", thickness=0.5, color=RULE, spaceAfter=5))
    ref = "Reference: standardized from the original invoice"
    if data.invoice_number.strip():
        ref += f" no. {esc(data.invoice_number.strip())}"
    if data.supplier.name.strip():
        ref += f" issued by {esc(data.supplier.name.strip())}"
    if original_filename:
        ref += f" (file: {esc(original_filename)}"
        ref += f", {original_pages} page{'s' if original_pages != 1 else ''})" if original_pages else ")"
    ref += f". Generated on {generated_at.strftime('%Y-%m-%d %H:%M')}."
    flow.append(Paragraph(ref, st["small"]))

    doc.build(flow, canvasmaker=_NumberedCanvas)
    return buf.getvalue()
