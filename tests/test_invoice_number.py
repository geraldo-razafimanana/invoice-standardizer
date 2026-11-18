"""Unit tests for invoice-number extraction — section 8 of the spec.

These tests work on synthetic Line/Cell/Word objects so they run without any PDF.
They verify the label-matching regexes and the _inv_no / fallback logic directly.
All 10 required scenarios are covered.
"""
from __future__ import annotations

import re
import pytest

# ── helpers ──────────────────────────────────────────────────────────────────
from app.numbers import fold
from app.parser import (
    INV_NO_LABEL,
    INV_NO_LABEL_ALT,
    INV_NO_LABEL_FR,
    INV_REF_LABEL,
    NUM_LABEL,
    REF_LABEL,
    PO_LABEL,
    DATE_RE,
    _inv_no,
    _inv_no_strict,
    fold_and_norm,
    find_value,
    is_section_label,
)
from app.layout import Word, Line, Cell, build_lines


def _word(text: str, x0: float = 0, y0: float = 0, w: float = 50, h: float = 12,
          page: int = 0, conf: float | None = None) -> Word:
    return Word(x0, y0, x0 + w, y0 + h, text, page, conf)


def _line(*texts: str, y0: float = 0, page: int = 0) -> Line:
    """Build a Line where each `text` is a separate Cell."""
    words: list[Word] = []
    x = 0.0
    for t in texts:
        w = _word(t, x0=x, y0=y0, w=float(8 * len(t)), h=12.0, page=page)
        words.append(w)
        x += w.x1 + 10
    return build_lines(words, page)[0]


def _lines(*rows: tuple[str, ...], base_y: float = 0) -> list[Line]:
    result: list[Line] = []
    for i, row in enumerate(rows):
        result.append(_line(*row, y0=base_y + i * 20))
    return result


# ══════════════════════════════════════════════════════════════════════════════
# 1 – Label-regex coverage
# ══════════════════════════════════════════════════════════════════════════════

class TestLabelRegexes:

    # ── INV_NO_LABEL (primary) ────────────────────────────────────────────────
    @pytest.mark.parametrize("text", [
        "FACTURE N°",
        "facture n°",
        "Facture N°:",
        "Invoice",
        "INVOICE NO.",
        "Invoice No",
        "Invoice No.",
        "invoice #",
        "INVOICE ID",
        "Bill",
    ])
    def test_inv_no_label_matches(self, text: str):
        assert INV_NO_LABEL.match(fold_and_norm(text)), f"Expected match: {text!r}"

    @pytest.mark.parametrize("text", [
        "Invoice date",
        "Facture du",
        "Invoice total",
        "Invoice address",
    ])
    def test_inv_no_label_no_false_positive(self, text: str):
        assert not INV_NO_LABEL.match(fold_and_norm(text)), f"Unexpected match: {text!r}"

    # ── INV_NO_LABEL_FR ───────────────────────────────────────────────────────
    @pytest.mark.parametrize("text", [
        # Original set
        "N° de facture",
        "N° facture",
        "No facture",
        "Numéro de facture",
        "Numéro facture",
        "Référence facture",
        "Réf. facture",
        "Référence de facturation",
        "Facture N°",
        "Facture numéro",
        "Facture no.",
        # New additions
        "Référence de la facture",
        "Référence facture fournisseur",
        "Identifiant facture",
        "Code facture",
        "Facture ID",
        "Facture code",
        "N° de pièce",
        "Numéro de pièce",
        "Référence pièce",
        "Pièce comptable N°",
        "N° document",
        "Numéro de document",
        "Référence document",
        # OCR variants
        "N0 facture",
        "Nº facture",
        "No de facture",
    ])
    def test_inv_no_label_fr_matches(self, text: str):
        assert INV_NO_LABEL_FR.match(fold_and_norm(text)), f"Expected match: {text!r}"

    @pytest.mark.parametrize("text", [
        "Référence commande",
        "Référence client",
        "No de compte",
        "Numéro de TVA",
        "Code postal",
    ])
    def test_inv_no_label_fr_no_false_positive(self, text: str):
        assert not INV_NO_LABEL_FR.match(fold_and_norm(text)), f"Unexpected match: {text!r}"

    # ── INV_REF_LABEL (English reference variants) ────────────────────────────
    @pytest.mark.parametrize("text", [
        # Original set
        "Invoice Reference",
        "Invoice Ref.",
        "Invoice Ref",
        "Inv. No.",
        "Inv No.",
        "Inv No",
        "Bill Number",
        "Bill No.",
        "Billing Reference",
        "Billing Ref.",
        # New additions
        "Invoice ID",
        "Invoice Code",
        "Inv #",
        "Inv. Ref.",
        "Bill #",
        "Bill ID",
        "Billing Number",
        "Billing ID",
        "Document Number",
        "Document No.",
        "Document Reference",
        "Document Ref.",
        "Reference Number",
        "Ref. No.",
        "Ref No",
        "Transaction Reference",
    ])
    def test_inv_ref_label_matches(self, text: str):
        assert INV_REF_LABEL.match(fold_and_norm(text)), f"Expected match: {text!r}"

    # ── PO_LABEL must NOT match invoice labels ────────────────────────────────
    @pytest.mark.parametrize("text", [
        "Purchase Order No.",
        "PO No.",
        "P.O. Number",
        "Bon de commande N°",
        "BC N°",
    ])
    def test_po_label_matches_po_only(self, text: str):
        assert PO_LABEL.match(fold_and_norm(text)), f"PO_LABEL should match: {text!r}"
        assert not INV_NO_LABEL_FR.match(fold_and_norm(text))
        assert not INV_REF_LABEL.match(fold_and_norm(text))


# ══════════════════════════════════════════════════════════════════════════════
# 2 – _inv_no value extractor
# ══════════════════════════════════════════════════════════════════════════════

class TestInvNo:

    @pytest.mark.parametrize("text, expected", [
        ("FT354992",         "FT354992"),
        ("F1354992",         "F1354992"),
        ("FAC-2026-00125",   "FAC-2026-00125"),
        ("INV-2026-00045",   "INV-2026-00045"),
        ("2026/00125",       "2026/00125"),
        ("001254",           "001254"),
        ("24-000158",        "24-000158"),
        ("ABC/2026/045",     "ABC/2026/045"),
        # With leading noise stripped by regex
        ("N° FAC-001",       "FAC-001"),
        ("No. INV2024001",   "INV2024001"),
        (": ABC/2026/045",   "ABC/2026/045"),
    ])
    def test_valid_numbers(self, text: str, expected: str):
        result = _inv_no(text)
        assert result == expected, f"_inv_no({text!r}) = {result!r}, expected {expected!r}"

    @pytest.mark.parametrize("text", [
        "2024-01-15",    # date
        "01/02/2024",    # date
        "",              # empty
        "TOTAL",         # label word
        "facture",       # label word
    ])
    def test_rejects_invalid(self, text: str):
        assert _inv_no(text) is None, f"Should have rejected: {text!r}"

    def test_length_cap_50(self):
        # 50 chars should be accepted
        long_ref = "FAC-" + "1" * 46
        assert _inv_no(long_ref) == long_ref
        # 51 chars should be rejected
        too_long = "FAC-" + "1" * 47
        assert _inv_no(too_long) is None


# ══════════════════════════════════════════════════════════════════════════════
# 3 – find_value integration (Scenarios 1–10 from the spec)
# ══════════════════════════════════════════════════════════════════════════════

def _extract_inv(rows: list[tuple[str, ...]]) -> str | None:
    """Run the full label-priority chain on synthetic lines."""
    ls = _lines(*rows)
    r = (
        find_value(ls, INV_NO_LABEL,    _inv_no, neighbor_extract=_inv_no_strict)
        or find_value(ls, INV_NO_LABEL_FR,  _inv_no, neighbor_extract=_inv_no_strict)
        or find_value(ls, INV_REF_LABEL,    _inv_no, neighbor_extract=_inv_no_strict)
        or find_value(ls, INV_NO_LABEL_ALT, _inv_no)
        or find_value(ls, NUM_LABEL,    _inv_no)
        or find_value(ls, REF_LABEL,    _inv_no, limit=len(ls), neighbor_extract=_inv_no_strict)
    )
    return r[0] if r else None


class TestScenarios:

    # Scenario 1 – French "FACTURE N°"
    def test_french_facture_no(self):
        result = _extract_inv([("FACTURE N°", "F-2026-0012")])
        assert result == "F-2026-0012"

    # Scenario 1 – French "N° de facture" on separate cells
    def test_french_no_de_facture(self):
        result = _extract_inv([("N° de facture", "FAC-2026-00125")])
        assert result == "FAC-2026-00125"

    # Scenario 1 – French "Numéro de facture"
    def test_french_numero_de_facture(self):
        result = _extract_inv([("Numéro de facture", "001254")])
        assert result == "001254"

    # Scenario 2 – English "Invoice Number"
    def test_english_invoice_number(self):
        result = _extract_inv([("Invoice Number", "INV-2026-00045")])
        assert result == "INV-2026-00045"

    # Scenario 2 – English "Invoice No."
    def test_english_invoice_no(self):
        result = _extract_inv([("Invoice No.", "FT354992")])
        assert result == "FT354992"

    # Scenario 2 – English "Invoice #"
    def test_english_invoice_hash(self):
        result = _extract_inv([("Invoice #", "24-000158")])
        assert result == "24-000158"

    # Scenario 3 – "Invoice Reference"
    def test_invoice_reference(self):
        result = _extract_inv([("Invoice Reference", "ABC/2026/045")])
        assert result == "ABC/2026/045"

    # Scenario 3 – "Invoice Ref."
    def test_invoice_ref_dot(self):
        result = _extract_inv([("Invoice Ref.", "INV-001")])
        assert result == "INV-001"

    # Scenario 3 – "Billing Reference"
    def test_billing_reference(self):
        result = _extract_inv([("Billing Reference", "BR-2024-9876")])
        assert result == "BR-2024-9876"

    # Scenario 3 – "Inv. No."
    def test_inv_no_dot(self):
        result = _extract_inv([("Inv. No.", "F1354992")])
        assert result == "F1354992"

    # Scenario 4 – number in header, value in next cell (right-aligned layout)
    def test_header_right_value_in_next_cell(self):
        # Label cell on the left, value in the adjacent cell
        result = _extract_inv([("Invoice No.", "INV-2025-001")])
        assert result == "INV-2025-001"

    # Scenario 5 – number in header left, value on next line below
    def test_header_left_value_below(self):
        ls = _lines(
            ("Invoice Number:",),   # label line
            ("INV-2025-001",),      # value line directly below
        )
        r = (
            find_value(ls, INV_NO_LABEL,    _inv_no, neighbor_extract=_inv_no_strict)
            or find_value(ls, INV_REF_LABEL,    _inv_no, neighbor_extract=_inv_no_strict)
        )
        assert r is not None and r[0] == "INV-2025-001"

    # Scenario 6 – value inline on the same label cell
    def test_value_inline_same_cell(self):
        # Label and value are in the same text: "FACTURE N° FAC-001"
        ls = _lines(("FACTURE N° FAC-001",))
        r = find_value(ls, INV_NO_LABEL, _inv_no, neighbor_extract=_inv_no_strict)
        assert r is not None and r[0] == "FAC-001"

    # Scenario 7 – invoice has invoice number, PO and BL — must pick the invoice number
    def test_not_confused_with_po_and_bl(self):
        # Three labels; the invoice label appears first
        result = _extract_inv([
            ("Invoice Number", "INV-001"),
            ("Purchase Order No.", "PO-9999"),
            ("Bon de livraison N°", "BL-5555"),
        ])
        assert result == "INV-001"

    # Scenario 7 – PO appears before invoice label: must NOT pick PO
    def test_invoice_wins_over_po_regardless_of_order(self):
        result = _extract_inv([
            ("PO No.", "PO-9999"),
            ("Facture N°", "FAC-0042"),
        ])
        # INV_NO_LABEL and INV_NO_LABEL_FR are tried before PO_LABEL in parse_document;
        # here find_value scans all lines, so the first-matched label wins.
        # The important thing: INV_NO_LABEL_FR matches "Facture N°", not PO_LABEL.
        assert result == "FAC-0042"

    # Scenario 8 – OCR error in label: "1nvoice" should be normalised to "invoice"
    def test_ocr_error_in_label_normalised(self):
        # fold_and_norm("1nvoice") -> normalise_ocr -> "lnvoice" -> "invoice" via _OCR_NORM
        normalised = fold_and_norm("1nvoice")
        assert "invoice" in normalised or "inv" in normalised

    # Scenario 9 – alphanumeric reference
    def test_alphanumeric_reference(self):
        result = _extract_inv([("Invoice No.", "ABC-2026-XYZ-001")])
        assert result == "ABC-2026-XYZ-001"

    # Scenario 9 – letters + digits no separator (FT354992)
    def test_compact_alphanumeric(self):
        result = _extract_inv([("N° facture", "FT354992")])
        assert result == "FT354992"

    # Scenario 10 – "Référence facture" (no explicit "N°")
    def test_reference_facture_label(self):
        result = _extract_inv([("Référence facture", "REF-2026-001")])
        assert result == "REF-2026-001"

    # Scenario 10 – "Réf. facture"
    def test_ref_dot_facture(self):
        result = _extract_inv([("Réf. facture", "RF-001")])
        assert result == "RF-001"

    # ── New terms from the extended list ────────────────────────────────────

    def test_reference_de_la_facture(self):
        result = _extract_inv([("Référence de la facture", "FAC-2026-0099")])
        assert result == "FAC-2026-0099"

    def test_reference_facture_fournisseur(self):
        result = _extract_inv([("Référence facture fournisseur", "FT354992")])
        assert result == "FT354992"

    def test_identifiant_facture(self):
        result = _extract_inv([("Identifiant facture", "ID-2026-001")])
        assert result == "ID-2026-001"

    def test_code_facture(self):
        result = _extract_inv([("Code facture", "CF-0042")])
        assert result == "CF-0042"

    def test_no_de_piece(self):
        result = _extract_inv([("N° de pièce", "PC-2026-0055")])
        assert result == "PC-2026-0055"

    def test_numero_de_piece(self):
        result = _extract_inv([("Numéro de pièce", "2026/00125")])
        assert result == "2026/00125"

    def test_reference_piece(self):
        result = _extract_inv([("Référence pièce", "RP-001")])
        assert result == "RP-001"

    def test_piece_comptable(self):
        result = _extract_inv([("Pièce comptable N°", "PCO-2026-001")])
        assert result == "PCO-2026-001"

    def test_no_document(self):
        result = _extract_inv([("N° document", "DOC-2026-001")])
        assert result == "DOC-2026-001"

    def test_numero_de_document(self):
        result = _extract_inv([("Numéro de document", "2026-00158")])
        assert result == "2026-00158"

    def test_reference_document(self):
        result = _extract_inv([("Référence document", "RD-2026-001")])
        assert result == "RD-2026-001"

    def test_invoice_id(self):
        result = _extract_inv([("Invoice ID", "INV-ID-001")])
        assert result == "INV-ID-001"

    def test_invoice_code(self):
        result = _extract_inv([("Invoice Code", "IC-2026-001")])
        assert result == "IC-2026-001"

    def test_inv_hash(self):
        result = _extract_inv([("Inv #", "FT354992")])
        assert result == "FT354992"

    def test_bill_hash(self):
        result = _extract_inv([("Bill #", "BL-2026-001")])
        assert result == "BL-2026-001"

    def test_billing_number(self):
        result = _extract_inv([("Billing Number", "BN-2026-001")])
        assert result == "BN-2026-001"

    def test_billing_id(self):
        result = _extract_inv([("Billing ID", "BI-001")])
        assert result == "BI-001"

    def test_document_number(self):
        result = _extract_inv([("Document Number", "DOC-001")])
        assert result == "DOC-001"

    def test_document_no(self):
        result = _extract_inv([("Document No.", "DN-2026-001")])
        assert result == "DN-2026-001"

    def test_document_reference(self):
        result = _extract_inv([("Document Reference", "DR-2026-001")])
        assert result == "DR-2026-001"

    def test_reference_number(self):
        result = _extract_inv([("Reference Number", "REF-2026-001")])
        assert result == "REF-2026-001"

    def test_ref_no(self):
        result = _extract_inv([("Ref. No.", "RN-2026-001")])
        assert result == "RN-2026-001"

    def test_transaction_reference(self):
        result = _extract_inv([("Transaction Reference", "TXN-2026-001")])
        assert result == "TXN-2026-001"

    # OCR variant: "N0 facture" (zero instead of degree sign)
    def test_ocr_n0_facture(self):
        result = _extract_inv([("N0 facture", "FAC-OCR-001")])
        assert result == "FAC-OCR-001"


# ══════════════════════════════════════════════════════════════════════════════
# 4 – Non-regression: other fields must not be captured as invoice number
# ══════════════════════════════════════════════════════════════════════════════

class TestNonRegression:

    @pytest.mark.parametrize("label,value", [
        ("Purchase Order No.", "PO-2024-001"),
        ("Bon de commande N°", "BC-001"),
        ("IBAN", "FR7630006000011234567890189"),
        ("NIF", "123456789"),
        ("STAT", "12345678901234"),
        ("N° client", "CLI-0042"),
        ("N° fournisseur", "FURN-001"),
        # Extended list — must also be blocked
        ("N° de TVA", "FR12345678901"),
        ("Code postal", "75001"),
        ("Référence commande", "CMD-001"),
        ("Référence client", "CLI-REF-001"),
        ("Numéro de TVA", "FR12345678901"),
        ("Document de livraison", "BL-001"),
    ])
    def test_false_positive_labels_not_matched_as_invoice(self, label: str, value: str):
        """These labels must NOT be caught by the invoice-number label patterns."""
        f = fold_and_norm(label)
        assert not INV_NO_LABEL.match(f),    f"INV_NO_LABEL should not match {label!r}"
        assert not INV_NO_LABEL_FR.match(f), f"INV_NO_LABEL_FR should not match {label!r}"
        assert not INV_REF_LABEL.match(f),   f"INV_REF_LABEL should not match {label!r}"

    def test_date_not_extracted_as_invoice_number(self):
        # A date must never be returned by _inv_no
        assert _inv_no("2024-01-15") is None
        assert _inv_no("15/01/2024") is None
        assert _inv_no("January 15, 2024") is None

    def test_is_section_label_includes_new_patterns(self):
        for label in (
            # Original
            "invoice reference", "ref. facture", "billing reference", "inv. no.", "n° de facture",
            # New terms
            "reference de la facture", "reference facture fournisseur",
            "identifiant facture", "code facture",
            "n° de piece", "numero de piece", "reference piece", "piece comptable n°",
            "n° document", "numero de document", "reference document",
            "invoice id", "invoice code", "inv #",
            "bill #", "billing number", "billing id",
            "document number", "document no.", "document reference",
            "reference number", "ref. no.", "transaction reference",
        ):
            f = fold_and_norm(label)
            assert is_section_label(f), f"is_section_label should return True for {label!r}"
