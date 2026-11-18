"""Generates test invoices with very different layouts (digital, scanned, multipage, ...).

Each builder returns (pdf_bytes, expected) where `expected` is the ground truth we assert on.
"""
from __future__ import annotations

import io
from pathlib import Path

import pymupdf
from reportlab.lib.pagesizes import A4
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

FONT_DIR = Path(__file__).resolve().parents[1] / "app" / "fonts"
pdfmetrics.registerFont(TTFont("S", str(FONT_DIR / "LiberationSans-Regular.ttf")))
pdfmetrics.registerFont(TTFont("SB", str(FONT_DIR / "LiberationSans-Bold.ttf")))

W, H = A4


def _pdf(draw) -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    draw(c)
    c.save()
    return buf.getvalue()


def _t(c, x, y, text, size=10, bold=False, align="l"):
    c.setFont("SB" if bold else "S", size)
    {"l": c.drawString, "r": c.drawRightString, "c": c.drawCentredString}[align](x, y, text)


# ----------------------------------------------------------------------------------------
def classic_en() -> tuple[bytes, dict]:
    """English, USD, supplier top-left, 'INVOICE' top-right, classic 4-column table."""
    def draw(c):
        _t(c, 50, H - 60, "Northwind Traders LLC", 18, True)
        _t(c, 50, H - 76, "42 Harbor Street", 9)
        _t(c, 50, H - 88, "Portland, OR 97201, USA", 9)
        _t(c, 50, H - 100, "Tax ID: 12-3456789", 9)
        _t(c, 50, H - 112, "Email: billing@northwind.example", 9)
        _t(c, W - 50, H - 60, "INVOICE", 24, True, "r")
        _t(c, W - 50, H - 85, "Invoice No: INV-2026-0042", 10, False, "r")
        _t(c, W - 50, H - 99, "Invoice Date: 2026-03-12", 10, False, "r")
        _t(c, W - 50, H - 113, "Due Date: 2026-04-11", 10, False, "r")
        _t(c, 50, H - 160, "Bill To", 9, True)
        _t(c, 50, H - 174, "Contoso Retail Inc.", 11, True)
        _t(c, 50, H - 187, "9 Market Square", 9)
        _t(c, 50, H - 199, "Seattle, WA 98101", 9)
        y = H - 250
        c.line(50, y + 14, W - 50, y + 14)
        _t(c, 55, y, "Description", 9, True)
        _t(c, 330, y, "Qty", 9, True, "r")
        _t(c, 420, y, "Unit Price", 9, True, "r")
        _t(c, W - 55, y, "Amount", 9, True, "r")
        c.line(50, y - 6, W - 50, y - 6)
        rows = [("Ergonomic office chair", "4", "129.50", "518.00"),
                ("Standing desk frame", "2", "310.00", "620.00"),
                ("USB-C docking station", "5", "89.99", "449.95")]
        for d, q, u, a in rows:
            y -= 20
            _t(c, 55, y, d, 9)
            _t(c, 330, y, q, 9, False, "r")
            _t(c, 420, y, u, 9, False, "r")
            _t(c, W - 55, y, a, 9, False, "r")
        y -= 30
        for lab, val in [("Subtotal", "1,587.95"), ("Tax (8%)", "127.04"), ("Total", "1,714.99")]:
            _t(c, 400, y, lab, 10, lab == "Total", "r")
            _t(c, W - 55, y, "$" + val if lab == "Total" else val, 10, lab == "Total", "r")
            y -= 16
        _t(c, 50, 80, "Payment terms: Net 30. Thank you for your business.", 8)
    exp = dict(invoice_number="INV-2026-0042", issue_date="2026-03-12", due_date="2026-04-11",
               supplier="Northwind Traders LLC", customer="Contoso Retail Inc.", n_items=3,
               amounts=["518.00", "620.00", "449.95"], qtys=["4", "2", "5"], subtotal="1587.95",
               total="1714.99", tax="127.04", currency="$", tax_id="12-3456789")
    return _pdf(draw), exp


def french_ttc() -> tuple[bytes, dict]:
    """French, EUR with comma decimals and space thousands, per-rate VAT lines."""
    def draw(c):
        _t(c, 50, H - 60, "Ets Dubois & Fils SARL", 16, True)
        _t(c, 50, H - 75, "12 rue des Lilas", 9)
        _t(c, 50, H - 87, "69003 Lyon", 9)
        _t(c, 50, H - 99, "SIRET : 552 100 554 00025", 9)
        _t(c, W - 50, H - 60, "FACTURE N° F-2026-118", 14, True, "r")
        _t(c, W - 50, H - 78, "Date : 12 mars 2026", 10, False, "r")
        _t(c, W - 50, H - 92, "Date d'échéance : 11 avril 2026", 10, False, "r")
        _t(c, 330, H - 150, "Client :", 9, True)
        _t(c, 330, H - 164, "Société Lambert SAS", 11, True)
        _t(c, 330, H - 177, "5 avenue Foch", 9)
        _t(c, 330, H - 189, "75016 Paris", 9)
        y = H - 250
        _t(c, 55, y, "Désignation", 9, True)
        _t(c, 300, y, "Qté", 9, True, "r")
        _t(c, 370, y, "PU HT", 9, True, "r")
        _t(c, 425, y, "TVA", 9, True, "r")
        _t(c, W - 55, y, "Montant HT", 9, True, "r")
        c.line(50, y - 6, W - 50, y - 6)
        rows = [("Peinture murale blanche 10 L", "6", "48,50", "20%", "291,00"),
                ("Pose et préparation des murs", "1", "1 250,00", "20%", "1 250,00"),
                ("Livre technique sur l'isolation", "3", "22,00", "5,5%", "66,00")]
        for d, q, u, t, a in rows:
            y -= 20
            _t(c, 55, y, d, 9)
            _t(c, 300, y, q, 9, False, "r")
            _t(c, 370, y, u, 9, False, "r")
            _t(c, 425, y, t, 9, False, "r")
            _t(c, W - 55, y, a, 9, False, "r")
        y -= 34
        for lab, val in [("Total HT", "1 607,00 €"), ("TVA 20%", "308,20 €"), ("TVA 5,5%", "3,63 €"),
                         ("Total TTC", "1 918,83 €")]:
            _t(c, 440, y, lab, 10, lab == "Total TTC", "r")
            _t(c, W - 55, y, val, 10, lab == "Total TTC", "r")
            y -= 16
    exp = dict(invoice_number="F-2026-118", supplier="Ets Dubois & Fils SARL", customer="Société Lambert SAS",
               n_items=3, amounts=["291.00", "1250.00", "66.00"], subtotal="1607.00", total="1918.83",
               taxes=[("20", "308.20"), ("5.5", "3.63")], currency="€", tax_rate_items=["20", "20", "5.5"])
    return _pdf(draw), exp


def mga_madagascar() -> tuple[bytes, dict]:
    """Madagascar: NIF/STAT, Ariary with space thousands and no decimals, no tax."""
    def draw(c):
        _t(c, 50, H - 60, "HUILERIE INDUSTRIELLE DE TOAMASINA", 14, True)
        _t(c, 50, H - 74, "Zone Industrielle Ivoloina, Toamasina", 9)
        _t(c, 50, H - 86, "NIF: 1000234567  STAT: 10101 11 1998 0 00123", 9)
        _t(c, 50, H - 130, "FACTURE", 20, True)
        _t(c, 50, H - 150, "N° 2026/0315", 11)
        _t(c, 50, H - 164, "Date : 05/10/2026", 10)
        _t(c, 320, H - 130, "Doit :", 9, True)
        _t(c, 320, H - 144, "Hôtel Le Soleil", 11, True)
        _t(c, 320, H - 157, "Boulevard Joffre, Toamasina", 9)
        y = H - 230
        _t(c, 55, y, "Désignation", 9, True)
        _t(c, 330, y, "Quantité", 9, True, "r")
        _t(c, 430, y, "Prix unitaire", 9, True, "r")
        _t(c, W - 55, y, "Montant", 9, True, "r")
        c.line(50, y - 6, W - 50, y - 6)
        rows = [("Huile de coco 20 L", "10", "180 000", "1 800 000"),
                ("Savon artisanal (carton)", "25", "45 000", "1 125 000"),
                ("Transport", "1", "150 000", "150 000")]
        for d, q, u, a in rows:
            y -= 20
            _t(c, 55, y, d, 9)
            _t(c, 330, y, q, 9, False, "r")
            _t(c, 430, y, u, 9, False, "r")
            _t(c, W - 55, y, a, 9, False, "r")
        y -= 34
        _t(c, 430, y, "Total", 11, True, "r")
        _t(c, W - 55, y, "3 075 000 Ar", 11, True, "r")
    exp = dict(invoice_number="2026/0315", issue_date="05/10/2026", supplier="HUILERIE INDUSTRIELLE DE TOAMASINA",
               customer="Hôtel Le Soleil", n_items=3, amounts=["1800000", "1125000", "150000"], total="3075000",
               currency="Ar")
    return _pdf(draw), exp


def multipage(n_items: int = 55) -> tuple[bytes, dict]:
    """Many lines across several pages, header repeated, totals on the last page."""
    rows = [(f"Component part #{i:03d}", str(1 + i % 5), f"{10 + i}.00") for i in range(1, n_items + 1)]
    amounts = [f"{(1 + i % 5) * (10 + i)}.00" for i in range(1, n_items + 1)]
    total = sum((1 + i % 5) * (10 + i) for i in range(1, n_items + 1))

    def draw(c):
        def header(y):
            _t(c, 55, y, "Description", 9, True)
            _t(c, 340, y, "Qty", 9, True, "r")
            _t(c, 430, y, "Unit price", 9, True, "r")
            _t(c, W - 55, y, "Amount", 9, True, "r")
            c.line(50, y - 6, W - 50, y - 6)

        _t(c, 50, H - 60, "Bulk Supplies GmbH", 16, True)
        _t(c, W - 50, H - 60, "INVOICE", 20, True, "r")
        _t(c, W - 50, H - 80, "Invoice #: BS-7781", 10, False, "r")
        _t(c, W - 50, H - 94, "Date: 2026-05-02", 10, False, "r")
        _t(c, 50, H - 130, "Bill To:", 9, True)
        _t(c, 50, H - 144, "Fabrikam Assembly Ltd", 11, True)
        y = H - 200
        header(y)
        per_page = 0
        for (d, q, u), a in zip(rows, amounts):
            y -= 18
            if y < 90:
                _t(c, W / 2, 40, "Page continues", 8, False, "c")
                c.showPage()
                y = H - 80
                _t(c, 50, H - 50, "Invoice BS-7781 - page 2", 8)
                header(y)
                y -= 0
            _t(c, 55, y, d, 9)
            _t(c, 340, y, q, 9, False, "r")
            _t(c, 430, y, u, 9, False, "r")
            _t(c, W - 55, y, a, 9, False, "r")
        y -= 30
        if y < 90:
            c.showPage()
            y = H - 100
        _t(c, 430, y, "Total", 11, True, "r")
        _t(c, W - 55, y, f"{total:,.2f}", 11, True, "r")
    exp = dict(invoice_number="BS-7781", supplier="Bulk Supplies GmbH", n_items=n_items, amounts=amounts,
               total=f"{total}.00")
    return _pdf(draw), exp


def discounts_multi_tax() -> tuple[bytes, dict]:
    """Per-line discount and tax columns, two tax rates, a shipping charge, GBP."""
    def draw(c):
        _t(c, 50, H - 60, "Highland Foods Ltd", 16, True)
        _t(c, 50, H - 75, "VAT No: GB123456789", 9)
        _t(c, W - 50, H - 60, "Invoice", 22, True, "r")
        _t(c, 50, H - 120, "Invoice Number", 9, True)
        _t(c, 200, H - 120, "Invoice Date", 9, True)
        _t(c, 350, H - 120, "PO Number", 9, True)
        _t(c, 50, H - 134, "HF-5531", 10)
        _t(c, 200, H - 134, "14 Feb 2026", 10)
        _t(c, 350, H - 134, "PO-88121", 10)
        _t(c, 50, H - 175, "Invoice To", 9, True)
        _t(c, 50, H - 189, "The Corner Cafe", 11, True)
        y = H - 250
        _t(c, 55, y, "Item", 9, True)
        _t(c, 250, y, "Qty", 9, True, "r")
        _t(c, 320, y, "Price", 9, True, "r")
        _t(c, 380, y, "Disc.", 9, True, "r")
        _t(c, 430, y, "VAT", 9, True, "r")
        _t(c, W - 55, y, "Total", 9, True, "r")
        c.line(50, y - 6, W - 50, y - 6)
        rows = [("Organic oats 25kg", "4", "32.00", "10%", "20%", "115.20"),
                ("Sparkling water (case)", "10", "6.50", "", "20%", "65.00"),
                ("Children's books", "6", "7.00", "", "0%", "42.00")]
        for d, q, u, ds, t, a in rows:
            y -= 20
            _t(c, 55, y, d, 9)
            _t(c, 250, y, q, 9, False, "r")
            _t(c, 320, y, u, 9, False, "r")
            if ds:
                _t(c, 380, y, ds, 9, False, "r")
            _t(c, 430, y, t, 9, False, "r")
            _t(c, W - 55, y, a, 9, False, "r")
        y -= 34
        for lab, val in [("Subtotal", "222.20"), ("Shipping", "12.00"), ("VAT 20%", "36.04"),
                         ("VAT 0%", "0.00"), ("Total", "270.24")]:
            _t(c, 440, y, lab, 10, lab == "Total", "r")
            _t(c, W - 55, y, ("£" + val) if lab == "Total" else val, 10, lab == "Total", "r")
            y -= 16
        _t(c, 440, y - 6, "Amount paid", 10, False, "r")
        _t(c, W - 55, y - 6, "100.00", 10, False, "r")
        _t(c, 440, y - 22, "Balance due", 10, True, "r")
        _t(c, W - 55, y - 22, "170.24", 10, True, "r")
    exp = dict(invoice_number="HF-5531", issue_date="14 Feb 2026", po_number="PO-88121",
               supplier="Highland Foods Ltd", customer="The Corner Cafe", n_items=3,
               amounts=["115.20", "65.00", "42.00"], discounts=["10%", "", ""], tax_rate_items=["20", "20", "0"],
               subtotal="222.20", total="270.24", paid="100.00", balance="170.24", currency="£",
               taxes=[("20", "36.04"), ("0", "0.00")], charges=[("Shipping", "12.00")])
    return _pdf(draw), exp


def missing_info() -> tuple[bytes, dict]:
    """No invoice number, no customer, no tax, no due date: only a few fields exist."""
    def draw(c):
        _t(c, 50, H - 60, "Marché Local Coopérative", 16, True)
        _t(c, 50, H - 80, "Date: 03/02/2026", 10)
        y = H - 150
        _t(c, 55, y, "Description", 9, True)
        _t(c, 400, y, "Amount", 9, True, "r")
        c.line(50, y - 6, W - 50, y - 6)
        for d, a in [("Fresh vegetables basket", "45.00"), ("Delivery to the market", "5.00")]:
            y -= 20
            _t(c, 55, y, d, 9)
            _t(c, 400, y, a, 9, False, "r")
        y -= 30
        _t(c, 300, y, "Total", 11, True, "r")
        _t(c, 400, y, "50.00", 11, True, "r")
    exp = dict(invoice_number="", supplier="Marché Local Coopérative", customer="", n_items=2,
               amounts=["45.00", "5.00"], total="50.00", due_date="")
    return _pdf(draw), exp


def letters_only_invoice_number() -> tuple[bytes, dict]:
    """Invoice number made only of letters (no digit)."""
    def draw(c):
        _t(c, 50, H - 60, "Alpha Beta Services", 16, True)
        _t(c, W - 50, H - 60, "INVOICE", 20, True, "r")
        _t(c, W - 50, H - 80, "Invoice No: ABS", 10, False, "r")
        _t(c, W - 50, H - 94, "Date: 2026-04-01", 10, False, "r")
        y = H - 170
        _t(c, 55, y, "Description", 9, True)
        _t(c, W - 55, y, "Amount", 9, True, "r")
        c.line(50, y - 6, W - 50, y - 6)
        y -= 20
        _t(c, 55, y, "Consulting service", 9)
        _t(c, W - 55, y, "100.00", 9, False, "r")
        y -= 30
        _t(c, 400, y, "Total", 11, True, "r")
        _t(c, W - 55, y, "100.00", 11, True, "r")
    exp = dict(invoice_number="ABS", supplier="Alpha Beta Services", n_items=1,
               amounts=["100.00"], total="100.00")
    return _pdf(draw), exp


def supplier_after_customer_block() -> tuple[bytes, dict]:
    """Noisy header, 'CLIENT | REFERENCE' column headers, supplier printed at the bottom."""
    def draw(c):
        _t(c, 280, H - 40, "me", 12)
        _t(c, 50, H - 80, "INVOICE T0024965", 18, True)
        _t(c, 50, H - 102, "DYNATEC MADAGASCAR SA", 11, True)
        _t(c, 400, H - 102, "DATE DE FACTURE 30-sept.-2026", 8)
        _t(c, 50, H - 114, "IMMEUBLE TRANOFITARATRA", 8)
        _t(c, 50, H - 160, "CLIENT", 8, True)
        _t(c, 320, H - 160, "REFERENCE", 8, True)
        _t(c, 50, H - 174, "DYNATEC MADAGASCAR SA", 10, True)
        _t(c, 320, H - 174, "W100932550", 8)
        _t(c, 50, H - 186, "ANTANANARIVO", 8)
        _t(c, 50, H - 400, "DHL GLOBAL FORWARDING MADAGASCAR SAU", 10, True)
        _t(c, 50, H - 414, "RTE DES HYDROCARBURES ANKORONDRANO", 8)
        _t(c, 50, H - 426, "NIF: 5005604555", 8)
        _t(c, 50, H - 438, "STAT: 68101 11 2021 0 11062", 8)
    exp = dict(invoice_number="T0024965", supplier="DHL GLOBAL FORWARDING MADAGASCAR SAU",
               customer="DYNATEC MADAGASCAR SA")
    return _pdf(draw), exp


def inconsistent() -> tuple[bytes, dict]:
    """Line amount, subtotal and total do not add up."""
    def draw(c):
        _t(c, 50, H - 60, "Pixel Print Studio", 16, True)
        _t(c, W - 50, H - 60, "INVOICE", 20, True, "r")
        _t(c, W - 50, H - 80, "Invoice No: PP-1009", 10, False, "r")
        _t(c, W - 50, H - 94, "Date: 2026-01-20", 10, False, "r")
        y = H - 180
        _t(c, 55, y, "Description", 9, True)
        _t(c, 300, y, "Qty", 9, True, "r")
        _t(c, 380, y, "Unit price", 9, True, "r")
        _t(c, 470, y, "Amount", 9, True, "r")
        c.line(50, y - 6, W - 50, y - 6)
        for d, q, u, a in [("Poster A2", "10", "12.00", "150.00"), ("Flyers (500)", "2", "40.00", "80.00")]:
            y -= 20
            _t(c, 55, y, d, 9)
            _t(c, 300, y, q, 9, False, "r")
            _t(c, 380, y, u, 9, False, "r")
            _t(c, 470, y, a, 9, False, "r")
        y -= 30
        for lab, val in [("Subtotal", "250.00"), ("Tax (10%)", "25.00"), ("Total", "300.00")]:
            _t(c, 400, y, lab, 10, lab == "Total", "r")
            _t(c, 470, y, val, 10, lab == "Total", "r")
            y -= 16
    exp = dict(invoice_number="PP-1009", supplier="Pixel Print Studio", n_items=2, amounts=["150.00", "80.00"],
               subtotal="250.00", total="300.00",
               expect_codes={"line_mismatch", "subtotal_mismatch", "total_mismatch"})
    return _pdf(draw), exp


def different_layout() -> tuple[bytes, dict]:
    """Labelled From/To columns, totals box on the LEFT, labels above values, unit-less table."""
    def draw(c):
        _t(c, W / 2, H - 50, "TAX INVOICE", 16, True, "c")
        _t(c, 50, H - 90, "From", 9, True)
        _t(c, 320, H - 90, "To", 9, True)
        _t(c, 50, H - 104, "Atlas Engineering Pty Ltd", 11, True)
        _t(c, 50, H - 117, "1 Collins Street, Melbourne", 9)
        _t(c, 50, H - 129, "ABN: 51 824 753 556", 9)
        _t(c, 320, H - 104, "Zenith Mining Corp", 11, True)
        _t(c, 320, H - 117, "88 Hay Street, Perth", 9)
        _t(c, 50, H - 170, "Invoice number", 9, True)
        _t(c, 200, H - 170, "Issued", 9, True)
        _t(c, 340, H - 170, "Due", 9, True)
        _t(c, 50, H - 184, "AE-20931", 10)
        _t(c, 200, H - 184, "30 June 2026", 10)
        _t(c, 340, H - 184, "30 July 2026", 10)
        y = H - 240
        _t(c, 55, y, "Services", 9, True)
        _t(c, 360, y, "Hours", 9, True, "r")
        _t(c, 440, y, "Rate", 9, True, "r")
        _t(c, W - 55, y, "Amount", 9, True, "r")
        c.line(50, y - 6, W - 50, y - 6)
        for d, q, u, a in [("Structural survey - site A", "12", "185.00", "2,220.00"),
                           ("Report preparation", "6.5", "150.00", "975.00")]:
            y -= 20
            _t(c, 55, y, d, 9)
            _t(c, 360, y, q, 9, False, "r")
            _t(c, 440, y, u, 9, False, "r")
            _t(c, W - 55, y, a, 9, False, "r")
        # totals box on the left
        y -= 60
        c.rect(50, y - 50, 190, 70)
        _t(c, 58, y + 6, "Sub-total", 9)
        _t(c, 232, y + 6, "3,195.00", 9, False, "r")
        _t(c, 58, y - 10, "GST", 9)
        _t(c, 232, y - 10, "319.50", 9, False, "r")
        _t(c, 58, y - 28, "Total due", 10, True)
        _t(c, 232, y - 28, "AUD 3,514.50", 10, True, "r")
    exp = dict(invoice_number="AE-20931", issue_date="30 June 2026", due_date="30 July 2026",
               supplier="Atlas Engineering Pty Ltd", customer="Zenith Mining Corp", n_items=2,
               amounts=["2220.00", "975.00"], qtys=["12", "6.5"], subtotal="3195.00", total="3514.50",
               tax="319.50", currency="AUD")
    return _pdf(draw), exp


def label_variants_fr_en() -> tuple[bytes, dict]:
    """OCR-confusable labels, Net Amount/Taux de TVA, and a customer name starting with "Client"."""
    def draw(c):
        _t(c, 50, H - 60, "Label Test SARL", 14, True)
        _t(c, W - 50, H - 60, "FACTURE", 16, True, "r")
        y = H - 110
        for lab, val in [("N° facture :", "F-2026-500"), ("Date d'émission :", "12/03/2026"),
                         ("P0 Number :", "PO-12345"), ("Net Amount", "1 000,00"),
                         ("Taux de TVA", "20%"), ("Montant TVA", "200,00"), ("Total Amount", "1 200,00")]:
            _t(c, 50, y, lab, 9, True)
            _t(c, 220, y, val, 9)
            y -= 16
        _t(c, 50, H - 260, "Bill To", 9, True)
        _t(c, 50, H - 276, "Client Example SA", 11, True)
        y2 = H - 330
        _t(c, 55, y2, "Description", 9, True)
        _t(c, 420, y2, "Qty", 9, True)
        _t(c, 470, y2, "Unit Price", 9, True)
        _t(c, W - 55, y2, "Amount", 9, True)
        y2 -= 20
        _t(c, 55, y2, "Service informatique", 9)
        _t(c, 420, y2, "1", 9)
        _t(c, 470, y2, "1 000,00", 9)
        _t(c, W - 55, y2, "1 000,00", 9)
        y2 -= 30
        _t(c, 430, y2, "Total", 10, True)
        _t(c, W - 55, y2, "1 200,00", 10, True)
    exp = dict(invoice_number="F-2026-500", issue_date="12/03/2026", po_number="PO-12345",
               customer="Client Example SA", subtotal="1000.00", taxes=[("20", "200.00")], total="1200.00")
    return _pdf(draw), exp


def bc_label() -> tuple[bytes, dict]:
    """French 'N° BC' purchase-order label must not replace the invoice number."""
    def draw(c):
        _t(c, 50, H - 60, "BC Test SARL", 14, True)
        _t(c, W - 50, H - 60, "FACTURE", 16, True, "r")
        _t(c, 50, H - 110, "Facture N°", 9, True)
        _t(c, 220, H - 110, "F-2026-501", 9)
        _t(c, 50, H - 126, "N° BC :", 9, True)
        _t(c, 220, H - 126, "BC-2026-001", 9)
        _t(c, 50, H - 180, "Bill To", 9, True)
        _t(c, 50, H - 196, "Buyer SA", 11, True)
        y = H - 250
        _t(c, 55, y, "Description", 9, True)
        _t(c, W - 55, y, "Amount", 9, True)
        y -= 20
        _t(c, 55, y, "Service", 9)
        _t(c, W - 55, y, "500.00", 9)
        y -= 30
        _t(c, 430, y, "Total", 10, True)
        _t(c, W - 55, y, "500.00", 10, True)
    exp = dict(invoice_number="F-2026-501", po_number="BC-2026-001", total="500.00")
    return _pdf(draw), exp


def bank_payment_words() -> tuple[bytes, dict]:
    """Supplier bank details, payment terms and the amount written in words."""
    def draw(c):
        _t(c, 50, H - 60, "Atlas Trading SARL", 16, True)
        _t(c, 50, H - 76, "12 rue des Comptoirs", 9)
        _t(c, 50, H - 88, "NIF: 1234567890", 9)
        _t(c, W - 50, H - 60, "INVOICE", 20, True, "r")
        _t(c, W - 50, H - 82, "Invoice No: AT-2026-77", 10, False, "r")
        _t(c, W - 50, H - 96, "Invoice Date: 2026-06-15", 10, False, "r")
        _t(c, 50, H - 120, "Payment terms:", 9, True)
        _t(c, 220, H - 120, "Net 30", 9)
        _t(c, 50, H - 140, "Bank name:", 9, True)
        _t(c, 220, H - 140, "BNP Paribas", 9)
        _t(c, 50, H - 154, "Account number:", 9, True)
        _t(c, 220, H - 154, "00123456789", 9)
        _t(c, 50, H - 168, "IBAN:", 9, True)
        _t(c, 220, H - 168, "FR76 3000 4000 0300 0000 1234 567", 9)
        _t(c, 50, H - 182, "SWIFT/BIC:", 9, True)
        _t(c, 220, H - 182, "BNPAFRPPXXX", 9)
        _t(c, 320, H - 120, "Bill To", 9, True)
        _t(c, 320, H - 136, "Horizon Consulting SA", 11, True)
        y = H - 250
        _t(c, 55, y, "Description", 9, True)
        _t(c, 420, y, "Qty", 9, True, "r")
        _t(c, 470, y, "Unit Price", 9, True, "r")
        _t(c, W - 55, y, "Amount", 9, True, "r")
        c.line(50, y - 6, W - 50, y - 6)
        y -= 20
        _t(c, 55, y, "Consulting service", 9)
        _t(c, 420, y, "1", 9, False, "r")
        _t(c, 470, y, "1 000,00", 9, False, "r")
        _t(c, W - 55, y, "1 000,00", 9, False, "r")
        y -= 34
        for lab, val in [("Subtotal", "1 000,00"), ("TVA 20%", "200,00"), ("Total", "1 200,00")]:
            _t(c, 430, y, lab, 10, lab == "Total", "r")
            _t(c, W - 55, y, val, 10, lab == "Total", "r")
            y -= 16
        _t(c, 50, 80, "Amount in words: One thousand two hundred euros", 9)
    exp = dict(invoice_number="AT-2026-77", supplier="Atlas Trading SARL", customer="Horizon Consulting SA",
               payment_terms="Net 30", bank_name="BNP Paribas", bank_account="00123456789",
               iban="FR7630004000030000001234567", swift="BNPAFRPPXXX",
               amount_in_words="One thousand two hundred euros", total="1200.00")
    return _pdf(draw), exp


def to_scanned(pdf_bytes: bytes, dpi: int = 200, noise: bool = False) -> bytes:
    """Rasterise a digital PDF into an image-only PDF (simulates a scan)."""
    from PIL import Image, ImageFilter

    src = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    out = pymupdf.open()
    for page in src:
        pix = page.get_pixmap(matrix=pymupdf.Matrix(dpi / 72, dpi / 72), alpha=False)
        img = Image.open(io.BytesIO(pix.tobytes("png"))).convert("L")
        if noise:
            img = img.filter(ImageFilter.GaussianBlur(0.8))
        b = io.BytesIO()
        img.save(b, format="JPEG", quality=70)
        new = out.new_page(width=page.rect.width, height=page.rect.height)
        new.insert_image(new.rect, stream=b.getvalue())
    return out.tobytes()


ALL = {
    "classic_en": classic_en,
    "french_ttc": french_ttc,
    "mga_madagascar": mga_madagascar,
    "discounts_multi_tax": discounts_multi_tax,
    "missing_info": missing_info,
    "inconsistent": inconsistent,
    "different_layout": different_layout,
    "label_variants_fr_en": label_variants_fr_en,
    "bc_label": bc_label,
    "bank_payment_words": bank_payment_words,
    "supplier_after_customer_block": supplier_after_customer_block,
}
