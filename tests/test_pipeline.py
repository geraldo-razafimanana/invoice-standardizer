"""End-to-end tests for the scenarios listed in the specification."""
import io
import sys
from pathlib import Path

import pymupdf
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import extraction  # noqa: E402
from app.checks import validate  # noqa: E402
from app.main import app  # noqa: E402
from app.models import InvoiceData, LineItem  # noqa: E402
from app.numbers import format_amount, parse_number  # noqa: E402
from app.parser import parse_document  # noqa: E402
from app.pdfgen import build_pdf, standardized_filename  # noqa: E402
from tests import sample_invoices as S  # noqa: E402

client = TestClient(app)


def run(pdf: bytes):
    doc = extraction.open_pdf(pdf)
    text = extraction.extract_words(doc)
    return text, parse_document(text)


def amounts(d):
    return [i.amount for i in d.items]


# ---------------------------------------------------------------- numbers
@pytest.mark.parametrize("raw,exp", [
    ("1,234.56", "1234.56"), ("1.234,56", "1234.56"), ("1 234,56", "1234.56"), ("1 250,00", "1250.00"),
    ("3 075 000", "3075000"), ("(12.50)", "-12.50"), ("-12", "-12"), ("48,50", "48.50"),
    ("$1,000", "1000"), ("12,34 €", "12.34"), ("1'234.50", "1234.50"), ("abc", None), ("", None),
])
def test_parse_number(raw, exp):
    assert parse_number(raw) == exp


def test_format_never_rounds():
    assert format_amount("1234.5678") == "1,234.5678"
    assert format_amount("0.1") == "0.10"


# ---------------------------------------------------------------- layouts
def test_digital_classic():
    text, out = run(S.classic_en()[0])
    d = out.data
    assert text.method == "text"
    assert (d.invoice_number, d.issue_date, d.due_date) == ("INV-2026-0042", "2026-03-12", "2026-04-11")
    assert d.supplier.name == "Northwind Traders LLC" and d.customer.name == "Contoso Retail Inc."
    assert amounts(d) == ["518.00", "620.00", "449.95"]
    assert [i.quantity for i in d.items] == ["4", "2", "5"]
    assert (d.subtotal, d.total, d.taxes[0].amount) == ("1587.95", "1714.99", "127.04")


def test_letter_only_invoice_number():
    _, out = run(S.letters_only_invoice_number()[0])
    assert out.data.invoice_number == "ABS"


def test_ocr_restores_letter_from_embedded_text_layer():
    text_word = extraction.Word(124.2, 91.5, 182.7, 109.4, "T0024965", 0)
    ocr_word = extraction.Word(123.6, 95.3, 183.6, 105.5, "10024965", 0, 0.0)
    extraction._restore_text_tokens([ocr_word], [text_word])
    assert (ocr_word.text, ocr_word.conf) == ("T0024965", 80.0)


def test_supplier_after_customer_reference_header():
    _, out = run(S.supplier_after_customer_block()[0])
    d = out.data
    assert (d.invoice_number, d.supplier.name, d.customer.name) == (
        "T0024965", "DHL GLOBAL FORWARDING MADAGASCAR SAU", "DYNATEC MADAGASCAR SA")


def test_french_formats_and_multiple_tax_rates():
    _, out = run(S.french_ttc()[0])
    d = out.data
    assert d.invoice_number == "F-2026-118" and d.currency == "€"
    assert amounts(d) == ["291.00", "1250.00", "66.00"]
    assert [i.tax_rate for i in d.items] == ["20", "20", "5.5"]
    assert [(t.rate, t.amount) for t in d.taxes] == [("20", "308.20"), ("5.5", "3.63")]
    assert d.total == "1918.83"


def test_madagascar_ariary():
    _, out = run(S.mga_madagascar()[0])
    d = out.data
    assert d.invoice_number == "2026/0315" and d.total == "3075000" and d.currency == "Ar"
    assert "NIF" in d.supplier.tax_id and len(d.items) == 3


def test_discounts_and_two_tax_rates():
    _, out = run(S.discounts_multi_tax()[0])
    d = out.data
    assert d.po_number == "PO-88121"
    assert d.items[0].discount == "10%" and d.items[1].discount == ""
    assert (d.balance_due, d.amount_paid) == ("170.24", "100.00")
    assert d.additional_charges[0].label == "Shipping"
    assert not [w for w in validate(d) if w.severity == "warning"]


def test_foreign_currency_code():
    _, out = run(S.different_layout()[0])
    assert out.data.currency == "AUD" and out.data.total == "3514.50"


def test_different_layout():
    _, out = run(S.different_layout()[0])
    d = out.data
    assert d.invoice_number == "AE-20931" and d.supplier.name == "Atlas Engineering Pty Ltd"
    assert d.customer.name == "Zenith Mining Corp" and d.due_date == "30 July 2026"
    assert [i.quantity for i in d.items] == ["12", "6.5"]


def test_label_variants_fr_en():
    _, out = run(S.label_variants_fr_en()[0])
    d = out.data
    assert d.invoice_number == "F-2026-500" and d.issue_date == "12/03/2026"
    assert d.po_number == "PO-12345"          # "P0 Number" OCR confusion
    assert d.customer.name == "Client Example SA"
    assert d.subtotal == "1000.00"            # "Net Amount"
    assert [(t.rate, t.amount) for t in d.taxes] == [("20", "200.00")]  # "Taux de TVA"
    assert d.total == "1200.00"               # "Total Amount"


def test_bc_label_is_purchase_order_not_invoice_number():
    _, out = run(S.bc_label()[0])
    assert out.data.invoice_number == "F-2026-501"
    assert out.data.po_number == "BC-2026-001"


def test_bank_payment_terms_and_amount_in_words():
    _, out = run(S.bank_payment_words()[0])
    d = out.data
    assert d.payment_terms == "Net 30"
    assert (d.supplier.bank_name, d.supplier.bank_account) == ("BNP Paribas", "00123456789")
    assert (d.supplier.iban, d.supplier.swift) == ("FR7630004000030000001234567", "BNPAFRPPXXX")
    assert d.amount_in_words == "One thousand two hundred euros"
    assert d.total == "1200.00"


def test_bank_details_are_printed_on_generated_pdf():
    _, out = run(S.bank_payment_words()[0])
    text = pdf_text(build_pdf(out.data))
    for expected in ("Payment terms", "Net 30", "BNP Paribas", "FR7630004000030000001234567",
                     "BNPAFRPPXXX", "Amount in words", "One thousand two hundred euros"):
        assert expected in text


def test_same_invoice_and_po_number_warns():
    d = InvoiceData(invoice_number="INV-9", po_number="inv-9")
    assert "same_invoice_po" in {w.code for w in validate(d)}


def test_missing_information_is_flagged_not_invented():
    _, out = run(S.missing_info()[0])
    d = out.data
    assert d.invoice_number == "" and out.flags["invoice_number"] == "missing"
    assert d.customer.name == "" and d.due_date == "" and d.taxes == []
    assert out.flags["currency"] == "missing"


def test_inconsistent_amounts_warn_and_are_not_modified():
    _, out = run(S.inconsistent()[0])
    codes = {w.code for w in validate(out.data)}
    assert {"line_mismatch", "subtotal_mismatch", "total_mismatch"} <= codes
    assert out.data.total == "300.00" and out.data.subtotal == "250.00"  # untouched


def test_multipage():
    pdf, exp = S.multipage(55)
    text, out = run(pdf)
    assert len(text.pages) >= 2
    assert len(out.data.items) == 55 and amounts(out.data) == exp["amounts"]
    assert out.data.total == exp["total"] and out.data.invoice_number == "BS-7781"


# ---------------------------------------------------------------- scans / OCR
@pytest.mark.skipif(not extraction.tesseract_languages(), reason="tesseract missing")
def test_scanned_invoice_uses_ocr():
    pdf, exp = S.classic_en()
    text, out = run(S.to_scanned(pdf, noise=True))
    assert text.method == "ocr"
    d = out.data
    assert d.invoice_number == "INV-2026-0042" and amounts(d) == exp["amounts"] and d.total == "1714.99"


@pytest.mark.skipif(not extraction.tesseract_languages(), reason="tesseract missing")
def test_scanned_multipage():
    pdf, exp = S.multipage(30)
    text, out = run(S.to_scanned(pdf))
    assert text.method == "ocr" and len(text.pages) >= 2
    assert len(out.data.items) == 30 and out.data.total == exp["total"]


# ---------------------------------------------------------------- bad files
def test_bad_files():
    for payload, code in [(b"hello", "not_a_pdf"), (b"%PDF-1.4 junk", "corrupted"), (b"", "empty_file")]:
        r = client.post("/api/documents", files={"file": ("x.pdf", payload, "application/pdf")})
        assert r.status_code == 422 and r.json()["code"] == code


def test_encrypted_pdf_rejected():
    src = pymupdf.open(stream=S.classic_en()[0], filetype="pdf")
    enc = src.tobytes(encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="secret", owner_pw="secret")
    r = client.post("/api/documents", files={"file": ("e.pdf", enc, "application/pdf")})
    assert r.status_code == 422 and r.json()["code"] == "encrypted"


# ---------------------------------------------------------------- generated PDF
def pdf_text(b: bytes) -> str:
    return "\n".join(p.get_text() for p in pymupdf.open(stream=b, filetype="pdf"))


def test_generated_pdf_reflects_user_corrections():
    pdf, _ = S.classic_en()
    up = client.post("/api/documents", files={"file": ("inv.pdf", pdf, "application/pdf")}).json()
    ex = client.post(f"/api/documents/{up['doc_id']}/extract").json()
    data = ex["data"]
    data["supplier"]["name"] = "Corrected Supplier SA"          # user correction
    data["items"][0]["description"] = "Chaise édition spéciale"  # accents
    data["items"].pop(1)                                          # removed line
    data["items"].append({"description": "Added line", "quantity": "1", "unit_price": "10.00",
                          "discount": "", "tax_rate": "", "amount": "10.00"})
    r = client.post("/api/generate", json={"data": data, "doc_id": up["doc_id"]})
    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf"
    assert "Standardized_Invoice_INV-2026-0042.pdf" in r.headers["content-disposition"]
    text = pdf_text(r.content)
    assert "STANDARDIZED INVOICE" in text and "Corrected Supplier SA" in text
    assert "Chaise édition spéciale" in text and "Added line" in text
    assert "Standing desk frame" not in text
    assert "INV-2026-0042" in text and "inv.pdf" in text  # reference to the original
    assert "1,714.99" in text  # amounts preserved, not recomputed


def test_missing_fields_flagged_and_no_invented_content():
    data = InvoiceData(items=[LineItem(description="Thing", amount="5")], total="5")
    text = pdf_text(build_pdf(data))
    assert "Not identified" in text
    for forbidden in ("Due date", "CUSTOMER", "Payment terms", "Logo"):
        assert forbidden not in text
    assert standardized_filename(data, "my scan.pdf") == "Standardized_Invoice_NO-NUMBER_my-scan.pdf"


def test_pdf_paginates_long_invoice():
    data = InvoiceData(invoice_number="LONG-1", total="1")
    data.items = [LineItem(description=f"Item {i}", quantity="1", unit_price="1", amount="1") for i in range(150)]
    out = build_pdf(data)
    doc = pymupdf.open(stream=out, filetype="pdf")
    assert doc.page_count >= 3 and "Page 3 of" in pdf_text(out)


def test_discrepancies_printed_on_pdf_without_changing_amounts():
    _, out = run(S.inconsistent()[0])
    text = pdf_text(build_pdf(out.data))
    assert "VERIFICATION NOTES" in text and "300.00" in text and "250.00" in text
