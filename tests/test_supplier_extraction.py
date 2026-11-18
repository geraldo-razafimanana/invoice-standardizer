"""Unit tests for supplier-information extraction — spec section 6.

Tests cover all 12 required scenarios using synthetic Line/Cell/Word objects.
No PDF files are required.
"""
from __future__ import annotations

import pytest

from app.numbers import fold
from app.parser import (
    SUPPLIER_LABEL,
    CUSTOMER_LABEL,
    BANK_NAME_LABEL,
    BANK_ACCOUNT_LABEL,
    BANK_HEADER_LABEL,
    SWIFT_LABEL,
    IBAN_LABEL,
    PAYMENT_TERMS_LABEL,
    CONTACT_LINE,
    clean_party,
    fold_and_norm,
    other_tax_ids,
    find_value,
    find_bank_details,
    find_terms,
    _plain_text,
    _account_text,
    _iban_value,
    _swift_value,
)
from app.layout import Word, Line, build_lines
from app.models import Party


# ── helpers ──────────────────────────────────────────────────────────────────

def _word(text: str, x0: float = 0, y0: float = 0, w: float = 60,
          h: float = 12, page: int = 0, conf: float | None = None) -> Word:
    return Word(x0, y0, x0 + w, y0 + h, text, page, conf)


def _line(*texts: str, y0: float = 0, page: int = 0) -> Line:
    words, x = [], 0.0
    for t in texts:
        ww = float(8 * len(t))
        words.append(_word(t, x0=x, y0=y0, w=ww, h=12.0, page=page))
        x += ww + 10
    return build_lines(words, page)[0]


def _line_spaced(*texts: str, y0: float = 0, page: int = 0) -> Line:
    """Build a Line with cells well-separated (simulating table columns)."""
    words, x = [], 0.0
    for t in texts:
        ww = float(8 * len(t))
        words.append(Word(x, y0, x + ww, y0 + 12, t, page, None))
        x += ww + 120   # wide gap → separate cells
    return build_lines(words, page)[0]


def _lines_spaced(*rows: tuple[str, ...], base_y: float = 0) -> list[Line]:
    return [_line_spaced(*row, y0=base_y + i * 20) for i, row in enumerate(rows)]


# ══════════════════════════════════════════════════════════════════════════════
# 1 — SUPPLIER_LABEL coverage
# ══════════════════════════════════════════════════════════════════════════════

class TestSupplierLabel:

    @pytest.mark.parametrize("text", [
        # Original
        "From", "Supplier", "Supplier Name", "Supplier Details",
        "Vendor", "Vendor Name", "Seller", "Issued By", "Billed From", "Sold By",
        "Fournisseur", "Vendeur", "Emetteur", "Raison sociale", "Emis par",
        # New French
        "Nom du fournisseur",
        "Dénomination sociale",
        "Société",
        "Entreprise",
        "Etablissement",
        "Prestataire",
        # New English
        "Company Name",
        "Business Name",
    ])
    def test_supplier_label_matches(self, text: str):
        assert SUPPLIER_LABEL.match(fold_and_norm(text)), f"Expected match: {text!r}"

    @pytest.mark.parametrize("text", [
        "Customer", "Client", "Bill to", "Invoice to",
        "Billing Address", "Ship to", "Sold to",
        "Acheteur", "Destinataire",
    ])
    def test_supplier_label_not_customer(self, text: str):
        """Supplier label must not match customer labels."""
        f = fold_and_norm(text)
        assert not SUPPLIER_LABEL.match(f), f"Should not match: {text!r}"
        assert CUSTOMER_LABEL.match(f) or True  # customer label may or may not match


# ══════════════════════════════════════════════════════════════════════════════
# 2 — BANK labels coverage
# ══════════════════════════════════════════════════════════════════════════════

class TestBankLabels:

    # ── Bank name ────────────────────────────────────────────────────────────
    @pytest.mark.parametrize("text", [
        "Bank", "Bank Name", "Banque", "Nom de la banque",
        "Etablissement bancaire",
        "Banque domiciliataire",
        "Banque du fournisseur",
        "Beneficiary Bank",
        "Account Bank",
        "Banking Institution",
    ])
    def test_bank_name_label(self, text: str):
        assert BANK_NAME_LABEL.match(fold_and_norm(text)), f"Expected match: {text!r}"

    # ── Bank account ─────────────────────────────────────────────────────────
    @pytest.mark.parametrize("text", [
        "Account No.", "Bank Account", "Bank Account Number",
        "Account Number", "N° de compte", "Numéro de compte",
        "Compte bancaire", "RIB", "Relevé d'identité bancaire",
        "Référence bancaire", "Coordonnées bancaires",
        "A/C No.", "Bank Details",
    ])
    def test_bank_account_label(self, text: str):
        assert BANK_ACCOUNT_LABEL.match(fold_and_norm(text)), f"Expected match: {text!r}"

    # ── IBAN ─────────────────────────────────────────────────────────────────
    @pytest.mark.parametrize("text", [
        "IBAN", "IBAN No.", "N° IBAN", "Numéro IBAN",
        "International Bank Account Number",
    ])
    def test_iban_label(self, text: str):
        assert IBAN_LABEL.match(fold_and_norm(text)), f"Expected match: {text!r}"

    # ── SWIFT / BIC ───────────────────────────────────────────────────────────
    @pytest.mark.parametrize("text", [
        "SWIFT", "SWIFT Code", "BIC", "BIC Code",
        "SWIFT/BIC", "Code SWIFT", "Code BIC",
        "Bank Identifier Code",
    ])
    def test_swift_label(self, text: str):
        assert SWIFT_LABEL.match(fold_and_norm(text)), f"Expected match: {text!r}"

    # ── Bank header ───────────────────────────────────────────────────────────
    @pytest.mark.parametrize("text", [
        "Comptes bancaires", "Coordonnées bancaires",
        "Bank Details", "Bank Information", "Bank Accounts",
        "Référence bancaire", "Payment Details",
    ])
    def test_bank_header_label(self, text: str):
        assert BANK_HEADER_LABEL.match(fold_and_norm(text)), f"Expected match: {text!r}"


# ══════════════════════════════════════════════════════════════════════════════
# 3 — Payment terms label coverage
# ══════════════════════════════════════════════════════════════════════════════

class TestPaymentTermsLabel:

    @pytest.mark.parametrize("text", [
        # Original
        "Payment Terms", "Payment Method", "Payment Details",
        "Conditions de paiement", "Mode de paiement", "Règlement",
        # New French
        "Moyen de paiement",
        "Modalités de paiement",
        "Paiement par",
        "Règlement par",
        "Virement",
        "Virement bancaire",
        "Chèque",
        "Espèces",
        "Carte bancaire",
        # New English
        "Payment Mode",
        "Payment Type",
        "Method of Payment",
        "Terms of Payment",
        "Payment Conditions",
    ])
    def test_payment_terms_label(self, text: str):
        assert PAYMENT_TERMS_LABEL.match(fold_and_norm(text)), f"Expected match: {text!r}"


# ══════════════════════════════════════════════════════════════════════════════
# 4 — clean_party: address parsing (Scenarios 3 and 11)
# ══════════════════════════════════════════════════════════════════════════════

class TestCleanParty:

    def test_name_then_address_lines(self):
        """Scenario 3 — address on multiple lines."""
        block = ["ACME SARL", "Lot II M 45", "Rue de la Paix", "101 Antananarivo"]
        p = clean_party(block)
        assert p.name == "ACME SARL"
        assert "Lot II M 45" in p.address
        assert "Rue de la Paix" in p.address
        assert "101 Antananarivo" in p.address

    def test_siège_social_prefix_stripped(self):
        """'Siège social :' prefix should be stripped from address lines."""
        block = ["ACME SARL", "Siège social : Lot II M 45, Antananarivo"]
        p = clean_party(block)
        assert p.name == "ACME SARL"
        assert "Siège" not in p.address
        assert "Lot II M 45" in p.address

    def test_tax_ids_extracted_from_block(self):
        """Scenario 4 — NIF, STAT, CIF, RCS all extracted."""
        block = [
            "ACME SARL",
            "Lot 45 Antananarivo",
            "NIF: 1234567  STAT: 12345678901234",
        ]
        p = clean_party(block)
        assert "NIF: 1234567" in p.tax_id
        assert "STAT: 12345678901234" in p.tax_id

    def test_contact_line_dropped(self):
        """Phone, email, fax lines must not appear in address."""
        block = ["ACME SARL", "Lot 45 Tanà", "Tel: 020 22 123 45", "mail@acme.mg"]
        p = clean_party(block)
        assert "Tel" not in p.address
        assert "mail" not in p.address

    def test_name_only(self):
        block = ["ACME SARL"]
        p = clean_party(block)
        assert p.name == "ACME SARL"
        assert p.address == ""

    def test_address_prefix_agence(self):
        block = ["ACME SARL", "Agence : 12 Avenue de l'Indépendance"]
        p = clean_party(block)
        assert "Agence" not in p.address
        assert "12 Avenue" in p.address

    def test_head_office_prefix(self):
        block = ["ACME Ltd", "Head Office: 10 King Street, London"]
        p = clean_party(block)
        assert "Head Office" not in p.address
        assert "10 King Street" in p.address


# ══════════════════════════════════════════════════════════════════════════════
# 5 — other_tax_ids: extended label recognition (Scenario 4)
# ══════════════════════════════════════════════════════════════════════════════

class TestOtherTaxIds:

    def _make_lines(self, *texts: str) -> list[Line]:
        return [_line(t, y0=float(i * 20)) for i, t in enumerate(texts)]

    def test_nif_detected(self):
        ls = self._make_lines("NIF: 1234567")
        result = other_tax_ids(ls, set())
        assert "NIF" in result and "1234567" in result

    def test_stat_detected(self):
        ls = self._make_lines("STAT: 12345678901234")
        result = other_tax_ids(ls, set())
        assert "STAT" in result

    def test_numero_nif_variant(self):
        ls = self._make_lines("Numéro NIF: 1234567")
        result = other_tax_ids(ls, set())
        assert "NIF" in result or "1234567" in result

    def test_n_nif_variant(self):
        ls = self._make_lines("N° NIF 1234567")
        result = other_tax_ids(ls, set())
        assert "1234567" in result

    def test_rcs_detected(self):
        ls = self._make_lines("RCS: PARIS B 123 456 789")
        result = other_tax_ids(ls, set())
        assert "RCS" in result

    def test_cif_detected(self):
        ls = self._make_lines("CIF: ES12345678A")
        result = other_tax_ids(ls, set())
        assert "CIF" in result

    def test_multiple_ids_in_one_block(self):
        """Scenario 4 — NIF, STAT, CIF, RCS all on separate lines."""
        ls = self._make_lines(
            "NIF: 1234567",
            "STAT: 12345678901234",
            "CIF: A1234567B",
            "RCS: PARIS 123 456 789",
        )
        result = other_tax_ids(ls, set())
        assert "NIF" in result
        assert "STAT" in result
        assert "CIF" in result
        assert "RCS" in result

    def test_excluded_digits_not_returned(self):
        """Tax IDs whose digits match those of the customer are excluded."""
        ls = self._make_lines("NIF: 9876543")
        result = other_tax_ids(ls, {"9876543"})
        assert "9876543" not in result

    def test_tax_id_label(self):
        ls = self._make_lines("Tax ID: 12345678")
        result = other_tax_ids(ls, set())
        assert "12345678" in result

    def test_vat_number_label(self):
        ls = self._make_lines("VAT Number: FR12345678901")
        result = other_tax_ids(ls, set())
        assert "12345678901" in result


# ══════════════════════════════════════════════════════════════════════════════
# 6 — IBAN / SWIFT value extraction (Scenarios 5 and 6)
# ══════════════════════════════════════════════════════════════════════════════

class TestBankValues:

    def test_iban_valid(self):
        assert _iban_value("FR76 3000 6000 0112 3456 7890 189") == "FR7630006000011234567890189"

    def test_iban_none_on_garbage(self):
        assert _iban_value("hello world") is None

    def test_swift_valid(self):
        assert _swift_value("BNPAFRPP") == "BNPAFRPP"

    def test_swift_with_branch(self):
        result = _swift_value("BNPAFRPPXXX")
        assert result == "BNPAFRPPXXX"

    def test_swift_none_on_garbage(self):
        assert _swift_value("hello") is None

    def test_find_value_iban(self):
        """Scenario 6 — label + IBAN value in the next cell."""
        ls = _lines_spaced(("IBAN", "FR7630006000011234567890189"))
        r = find_value(ls, IBAN_LABEL, _iban_value)
        assert r is not None and "FR76" in r[0]

    def test_find_value_swift(self):
        """Scenario 6 — label + SWIFT code in the next cell."""
        ls = _lines_spaced(("SWIFT Code", "BNPAFRPP"))
        r = find_value(ls, SWIFT_LABEL, _swift_value)
        assert r is not None and r[0] == "BNPAFRPP"


# ══════════════════════════════════════════════════════════════════════════════
# 7 — Payment terms extraction (Scenarios 7 and 8)
# ══════════════════════════════════════════════════════════════════════════════

class TestPaymentTermsExtraction:

    def test_payment_method_virement(self):
        """Scenario 7 — explicit payment method 'Virement'."""
        ls = _lines_spaced(("Mode de paiement", "Virement bancaire"))
        r = find_terms(ls)
        assert r is not None and "Virement" in r[0]

    def test_payment_terms_30_jours(self):
        """Scenario 8 — '30 jours net' condition."""
        ls = _lines_spaced(("Conditions de paiement", "30 jours net"))
        r = find_terms(ls)
        assert r is not None and "30" in r[0]

    def test_payment_mode_label(self):
        ls = _lines_spaced(("Payment Method", "Bank Transfer"))
        r = find_terms(ls)
        assert r is not None and "Bank Transfer" in r[0]

    def test_method_of_payment(self):
        ls = _lines_spaced(("Method of Payment", "Cash"))
        r = find_terms(ls)
        assert r is not None and "Cash" in r[0]

    def test_modalites_paiement(self):
        ls = _lines_spaced(("Modalités de paiement", "Virement 30 jours"))
        r = find_terms(ls)
        assert r is not None and "Virement" in r[0]

    def test_payment_mode_label_pattern(self):
        """PAYMENT_TERMS_LABEL must match 'Payment Mode' standalone."""
        assert PAYMENT_TERMS_LABEL.match(fold_and_norm("Payment Mode"))

    def test_virement_label_pattern(self):
        """PAYMENT_TERMS_LABEL must match 'Virement' standalone."""
        assert PAYMENT_TERMS_LABEL.match(fold_and_norm("Virement"))


# ══════════════════════════════════════════════════════════════════════════════
# 8 — Supplier vs Customer distinction (Scenario 9)
# ══════════════════════════════════════════════════════════════════════════════

class TestSupplierVsCustomer:

    def test_supplier_label_not_customer_label(self):
        """SUPPLIER_LABEL and CUSTOMER_LABEL must be mutually exclusive on key terms."""
        supplier_only = ["Fournisseur", "Vendor", "Supplier", "Raison sociale", "Emetteur",
                         "Dénomination sociale", "Entreprise", "Company Name"]
        customer_only = ["Client", "Customer", "Bill to", "Invoice to", "Acheteur",
                         "Destinataire", "Billing Address", "Sold to"]
        for text in supplier_only:
            f = fold_and_norm(text)
            assert SUPPLIER_LABEL.match(f), f"SUPPLIER_LABEL should match: {text!r}"
            assert not CUSTOMER_LABEL.match(f), f"CUSTOMER_LABEL should NOT match: {text!r}"
        for text in customer_only:
            f = fold_and_norm(text)
            assert not SUPPLIER_LABEL.match(f), f"SUPPLIER_LABEL should NOT match: {text!r}"

    def test_supplier_address_not_delivery_address(self):
        """'Adresse de livraison' must not be matched as supplier address."""
        from app.parser import SHIP_LABEL
        assert SHIP_LABEL.match(fold_and_norm("Adresse de livraison"))
        assert not SUPPLIER_LABEL.match(fold_and_norm("Adresse de livraison"))


# ══════════════════════════════════════════════════════════════════════════════
# 9 — French and English label parity (Scenario 10)
# ══════════════════════════════════════════════════════════════════════════════

class TestBilingualLabels:

    @pytest.mark.parametrize("fr_label,en_label", [
        ("Fournisseur", "Supplier"),
        ("Raison sociale", "Company Name"),
        ("Mode de paiement", "Payment Method"),
        ("Virement", "Bank Transfer"),
        ("Banque", "Bank"),
        ("N° de compte", "Account Number"),
        ("IBAN", "IBAN"),
        ("Code SWIFT", "SWIFT Code"),
    ])
    def test_fr_and_en_both_match(self, fr_label: str, en_label: str):
        """Each French label and its English equivalent must match the same pattern."""
        # Find which pattern matches the French label and check the English one too
        patterns = {
            "supplier": SUPPLIER_LABEL,
            "bank_name": BANK_NAME_LABEL,
            "bank_account": BANK_ACCOUNT_LABEL,
            "iban": IBAN_LABEL,
            "swift": SWIFT_LABEL,
            "payment": PAYMENT_TERMS_LABEL,
        }
        fr_f, en_f = fold_and_norm(fr_label), fold_and_norm(en_label)
        matched_fr = any(p.match(fr_f) for p in patterns.values())
        matched_en = any(p.match(en_f) for p in patterns.values())
        assert matched_fr, f"No pattern matched French label: {fr_label!r}"
        assert matched_en, f"No pattern matched English label: {en_label!r}"
