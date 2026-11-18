"""Regression tests on real supplier documents (Madagascar proformas / quotes addressed to HITA)."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import extraction  # noqa: E402
from app.checks import validate  # noqa: E402
from app.parser import parse_document  # noqa: E402
from app.pdfgen import build_pdf  # noqa: E402

REAL = Path(__file__).parent / "real"
OWN = Path(__file__).resolve().parents[1] / "own_company.txt"


@pytest.fixture(autouse=True)
def own_company(monkeypatch):
    monkeypatch.setenv("INVOICE_OWN_COMPANY_FILE", str(OWN))


def run(name):
    doc = extraction.open_pdf((REAL / name).read_bytes())
    text = extraction.extract_words(doc)
    return text, parse_document(text)


def test_soa_vy_proforma_text_pdf():
    text, out = run("soa_vy_proforma_26112209.pdf")
    d = out.data
    assert d.invoice_number == "26112209" and d.issue_date == "16 Juillet 2026"
    assert d.items[-1].description == "MANCHON MICROFIBRE 10MM"   # picture captions are not appended
    assert d.supplier.name == "SOA VY" and "1000030057" in d.supplier.tax_id
    assert "HUILERIE INDUSTRIELLE DE TAMATAVE" in d.customer.name and "3000063634" in d.customer.tax_id
    assert [i.amount for i in d.items] == ["75000.00", "240000.00", "300000.00", "1500000.00", "300000.00", "53333.32", "170000.00"]
    assert [i.quantity for i in d.items] == ["15.00", "40.00", "50.00", "10.00", "10.00", "4.00", "4.00"]
    assert (d.subtotal, d.total, d.currency) == ("2638333.32", "3165999.98", "Ar")
    assert d.taxes[0].amount == "527666.66"
    assert d.supplier.bank_name == "BMOI" and "BFV:" in d.supplier.bank_account and "BNI:" in d.supplier.bank_account
    assert d.amount_in_words.startswith("TROIS MILLION") and "somme" not in d.amount_in_words.lower()
    assert not [w for w in validate(d) if w.severity == "warning"]


def test_mci_quote_header_table_and_totals_box():
    _, out = run("mci_devis_DV26A8975.pdf")
    d = out.data
    assert d.invoice_number == "DV26A8975" and d.issue_date == "29/09/26"
    assert d.supplier.name == "MADAGASCAR CHIMIE INDUSTRIES"      # name only present in the logo image
    assert d.customer.name == "HITA SARL"
    assert [i.amount for i in d.items] == ["42000.00", "2000.00"]
    assert all(i.discount == "" for i in d.items)                  # "0,00%" is not a discount
    assert (d.subtotal, d.total, d.currency) == ("44000.00", "52800.00", "Ar")
    assert (d.taxes[0].rate, d.taxes[0].amount) == ("20.00", "8800.00")
    assert d.additional_charges == []                              # "Livraison gratuite..." is not a charge
    assert d.supplier.bank_name == "BOA" and "463013" in d.supplier.bank_account
    assert "livraison" in d.payment_terms.lower()
    assert d.amount_in_words == "Cinquante deux mille huit cents Ariary"
    assert not [w for w in validate(d) if w.severity == "warning"]


@pytest.mark.skipif(not extraction.tesseract_languages(), reason="tesseract missing")
def test_lami_stamped_scan_with_bad_text_layer():
    text, out = run("lami_proforma_8947.pdf")
    d = out.data
    assert text.method == "ocr"                                   # the scanner's hidden text layer is rejected
    assert d.invoice_number == "8947/Siege/PRF/2026"
    assert "Assistance" in d.supplier.name and "HITA" in d.customer.name
    assert [i.amount for i in d.items] == ["270000.00", "4000.00", "10000.00"]
    assert [i.quantity for i in d.items] == ["1", "10", "2"]
    assert (d.subtotal, d.total) == ("284000.00", "340800.00")
    assert d.taxes[0].amount == "56800.00"
    assert not [w for w in validate(d) if w.severity == "warning"]


def test_standardized_pdf_of_real_invoice():
    import pymupdf

    _, out = run("mci_devis_DV26A8975.pdf")
    pdf = build_pdf(out.data, original_filename="mci.pdf", original_pages=1)
    text = "\n".join(p.get_text() for p in pymupdf.open(stream=pdf, filetype="pdf"))
    assert "DV26A8975" in text and "52,800.00" in text and "MADAGASCAR CHIMIE INDUSTRIES" in text


# ------------------------------------------------------------------ second batch (varied layouts)
BATCH2 = {
    # file: (number, supplier, total, [amounts], customer address fragment)
    "new_f1.pdf": ("PF-26-101", "SOA VY", "738000.00", ["75000.00", "240000.00", "300000.00"], "Port fluvial"),
    "new_f2.pdf": ("DEV-2026-224", "TECHNO BUREAUTIQUE", "412800.00", ["270000.00", "4000.00", "10000.00", "60000.00"], "Terminus"),
    "new_f3.pdf": ("FV-2026-003", "MADA FOURNITURES", "1815600.00", ["700000.00", "108000.00", "150000.00", "555000.00"], "Madagascar"),
    "new_f4.pdf": ("PF-2026-004", "ÉLECTRO SERVICE TANA", "1410000.00", ["360000.00", "380000.00", "255000.00", "180000.00"], "Port fluvial"),
    "new_f5.pdf": ("F-2026-005", "QUINCAILLERIE DU PORT", "1334400.00", ["144000.00", "108000.00", "87500.00", "660000.00", "112500.00"], "Port fluvial"),
    "new_f6.pdf": ("OFF-2026-006", "NET & BUREAU SERVICES", "1974000.00", ["175000.00", "90000.00", "960000.00", "420000.00"], "BP 44"),
}


@pytest.mark.parametrize("name", sorted(BATCH2))
def test_varied_layouts(name):
    number, supplier, total, amounts, cust_addr = BATCH2[name]
    _, out = run(name)
    d = out.data
    assert d.invoice_number == number
    assert d.supplier.name == supplier
    assert d.total == total and d.currency == "Ar"
    assert [i.amount for i in d.items] == amounts
    assert all(i.unit_price for i in d.items)
    assert cust_addr in d.customer.address
    assert not [w for w in validate(d) if w.severity == "warning"]
