"""Heuristic, layout-aware invoice parser (no paid API, fully local).

Works on `Line`/`Cell` objects built from the embedded text layer or OCR word boxes. The
parser never invents anything: when a value can't be found it stays an empty string and the
field is flagged as "missing"; when it came from a weak heuristic it is flagged "uncertain".
"""
from __future__ import annotations

import re
import statistics
from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal

from .extraction import DocumentText
from .layout import Cell, Line, Word, build_lines
from .config import matches_own_company
from .models import Charge, InvoiceData, LineItem, Party, TaxLine
from .numbers import fold, parse_number, parse_percent, to_decimal

LOW_CONF = 72.0  # OCR word confidence below this is flagged "uncertain"

# --------------------------------------------------------------------------------------
# OCR normalisation (labels only)
# --------------------------------------------------------------------------------------
# These substitutions are strictly length-preserving: label matching works on the
# normalised string but values are still sliced from the original text at the same
# offsets. Numeric tokens are never rewritten.
_OCR_NORM: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\bp0\b"), "po"),                 # P0 -> PO
    (re.compile(r"\b0(?=[a-z]{2})"), "o"),         # 0ffre -> offre
    (re.compile(r"\b1(?=[a-z]{2})"), "l"),         # 1nvoice -> lnvoice
    (re.compile(r"(?<=[a-z])0(?=[a-z])"), "o"),    # inv0ice -> invoice
    (re.compile(r"(?<=[a-z])1(?=[a-z])"), "l"),    # invo1ce -> invoice
    (re.compile(r"\blnv"), "inv"),                 # lnvoice -> invoice
]


def normalise_ocr(text: str) -> str:
    """Correct frequent OCR letter/digit substitutions on a folded string."""
    for pat, repl in _OCR_NORM:
        text = pat.sub(repl, text)
    return text


def fold_and_norm(text: str) -> str:
    """fold() + normalise_ocr() for label matching only (never for value extraction)."""
    return normalise_ocr(fold(text))


# --------------------------------------------------------------------------------------
# Regular expressions (applied to folded, OCR-normalised text)
# --------------------------------------------------------------------------------------
_MONTHS = (
    r"jan(?:uary|vier)?|janv|feb(?:ruary)?|fev(?:rier)?|mar(?:ch|s)?|apr(?:il)?|avr(?:il)?|may|mai|"
    r"jun(?:e)?|juin|jul(?:y)?|juil(?:let)?|aug(?:ust)?|aout|sep(?:t(?:ember)?)?|sept(?:embre)?|"
    r"oct(?:ober|obre)?|nov(?:ember|embre)?|dec(?:ember|embre)?|aoit|aot"
)
DATE_RE = re.compile(
    r"\b(?:"
    r"\d{4}[-/.]\d{1,2}[-/.]\d{1,2}"
    r"|\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4}"
    rf"|\d{{1,2}}(?:st|nd|rd|th|er)?[\s\-]+(?:de\s+)?(?:{_MONTHS})\.?,?[\s\-]+\d{{2,4}}"
    rf"|(?:{_MONTHS})\.?\s+\d{{1,2}}(?:st|nd|rd|th)?,?\s+\d{{2,4}}"
    r")\b"
)

INV_NO_LABEL = re.compile(
    # Primary: "invoice / facture / bill" + optional number-indicator keyword.
    # Negative lookahead blocks document-title lines that introduce a date, address or total.
    r"^(?:tax\s+)?(?:invoice|facture|bill|note\s+de\s+frais|rechnung|fattura|factura)(?![a-z])"
    r"(?!\s*(?:date|to\b|from\b|total|amount|due|terms|address|summary|details|period|issued|for\b"
    r"|du\b|de\b|d['\u2019]\b"   # "Facture du 15 jan", "facture de vente"
    r"))"
    r"\s*(?:no\.?|n[°o0º]\.?|nr\.?|num(?:[eé]ro|ber)?\.?|#|id|ref(?:[eé]rence)?\.?|code\.?)?"
    r"\s*[:#.\-–]*\s*"
)

# ---------------------------------------------------------------------------
# INV_REF_LABEL — English variants not covered by INV_NO_LABEL
# ---------------------------------------------------------------------------
# Covers (case-insensitive, after fold+normalise):
#   Invoice Reference / Invoice Ref. / Invoice Ref
#   Invoice ID / Invoice Code
#   Inv. No. / Inv No. / Inv No / Inv # / Inv. Ref.
#   Bill Number / Bill No. / Bill # / Bill ID
#   Billing Reference / Billing Number / Billing ID
#   Document Number / Document No. / Document Reference / Document Ref.
#   Reference Number / Ref. No. / Ref No
#   Transaction Reference
INV_REF_LABEL = re.compile(
    r"^(?:"
    # invoice / inv variants
    r"inv(?:oice)?\.?\s*(?:ref(?:[eé]rence)?\.?|no\.?|n[°o0º]\.?|num(?:ber)?\.?|#|id|code)"
    # bill / billing variants
    r"|bill(?:ing)?\.?\s*(?:ref(?:[eé]rence)?\.?|no\.?|n[°o0º]\.?|num(?:ber)?\.?|#|id|number)"
    # document number / reference
    r"|doc(?:ument)?\.?\s*(?:no\.?|n[°o0º]\.?|num(?:ber)?\.?|ref(?:[eé]rence)?\.?)"
    # "Reference Number", "Ref. No.", "Ref No"  (only when standing alone, not prefixed by PO/delivery)
    r"|ref(?:[eé]rence)?\.?\s*(?:no\.?|n[°o0º]\.?|num(?:ber)?\.?)"
    # "Transaction Reference"
    r"|transaction\s+ref(?:[eé]rence)?\.?"
    r"|invoice\s+ref(?:erence)?\.?"
    r")\s*[:#.\-–]*\s*",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# INV_NO_LABEL_FR — French-primary variants not covered by INV_NO_LABEL
# ---------------------------------------------------------------------------
# Covers (case-insensitive, after fold+normalise):
#   N° de facture / N° facture / No facture / Nº facture / N0 facture (OCR)
#   Numéro de facture / Numéro facture
#   Référence facture / Réf. facture / Référence de facturation
#   Référence de la facture / Référence facture fournisseur
#   Identifiant facture / Code facture
#   Facture N° / Facture numéro / Facture no. / Facture ref.
#   N° de pièce / Numéro de pièce / Référence pièce / Pièce comptable N°
#   N° document / Numéro de document / Référence document
INV_NO_LABEL_FR = re.compile(
    r"^(?:"
    # --- "N° facture", "N° de facture", "No facture", "Nº facture", "N0 facture" ---
    r"n[°o0º]\.?\s*(?:de\s+(?:la\s+)?)?facture"
    # --- "Numéro de facture", "Numéro facture" ---
    r"|num[eé]ro\s+(?:de\s+(?:la\s+)?)?facture"
    # --- "Référence facture", "Réf. facture", "Référence de facturation",
    #     "Référence de la facture", "Référence facture fournisseur" ---
    r"|r[eé]f(?:[eé]rence)?\.?\s*(?:de\s+(?:la\s+)?)?(?:facture(?:\s+fournisseur)?|facturation)"
    # --- "Identifiant facture", "Code facture" ---
    r"|(?:identifiant|code)\s+facture"
    # --- "Facture N°", "Facture numéro", "Facture no.", "Facture ref." ---
    r"|facture\s+(?:n[°o0º]\.?|num[eé]ro\.?|no\.?|#|ref(?:[eé]rence)?\.?|code\.?|id\.?)"
    # --- "N° de pièce", "Numéro de pièce", "Référence pièce" ---
    r"|(?:n[°o0º]\.?|num[eé]ro|r[eé]f(?:[eé]rence)?\.?)\s+(?:de\s+)?pi[eè]ce"
    # --- "Pièce comptable N°" ---
    r"|pi[eè]ce\s+comptable\s+n[°o0º]\.?"
    # --- "N° document", "Numéro de document", "Référence document" ---
    r"|(?:n[°o0º]\.?|num[eé]ro|r[eé]f(?:[eé]rence)?\.?)\s+(?:de\s+)?document"
    r"|invoice\s+(?:number|no\.?|#)|bill\s+(?:number|no\.?)|inv\.\s*no\.?"
    r")\s*[:#.\-–]*\s*",
    re.IGNORECASE,
)

INV_NO_LABEL_ALT = re.compile(
    # Short prefixes: "inv.", "fact.", "fac.", "fct.", "bil." followed by a number indicator.
    r"^(?:inv|fact?|fac|fct|bil)(?![a-z])\.?\s*(?:no\.?|n[°o0º]\.?|num\.?|#)\s*[:#.\-–]*\s*"
)
NUM_LABEL = re.compile(
    r"^(?:(?:pro\s*forma|proforma|devis|facture|bon\s+de\s+commande|avoir|invoice)\s+)?"
    r"(?:n[°º0]|no\.|numero|number)(?![a-z])"
    r"(?!\s*(?:de|d')\s*(?:tva|tel|compte|siret|stat|nif|cif|rc|commande|devis|client|fournisseur|dossier|offre|bon|bc))\s*"
    r"[:#.\-–]*\s*"
)
REF_LABEL = re.compile(
    r"^(?:(?:document|dossier|offre|devis|proforma|pro\s*forma|facture|invoice)\s+)?"
    r"(?:r[eé]f(?:[eé]rence)?|document|dossier|offre|devis|proforma|pro\s*forma|facture)(?![a-z])"
    r"(?!\s*(?:commande|bon\s+de\s+commande|client|fournisseur|dossier|offre)\b)\.?\s*[:#.\-–]*\s*"
)
ISSUE_LABEL = re.compile(
    r"^(?:(?:invoice|facture)\s+date|date\s+of\s+(?:invoice|issue|billing)|billing\s+date|issue\s*date|date\s+issued|issued(?:\s+on)?|"
    r"date\s+(?:de\s+|d['e]\s*)?(?:facture|facturation|emission|vente|creation|etablissement)|"
    r"date\s+d['e]\s*(?:achat|vente)|facture\s+du|factur[e]e?\s+le|"
    r"date(?!\s*(?:d['e]?\s*)?(?:echeance|due|limite|paiement|reglement|livraison|delivery|de\s+(?:paiement|livraison|reglement))))"
    r"(?![a-z])\s*[:#.\-–]*\s*"
)
DUE_LABEL = re.compile(
    r"^(?:due\s*date|date\s+(?:d['e]?\s*)?(?:echeance|limite(?:\s+de\s+paiement)?)|date\s+de\s+(?:paiement|reglement)|"
    r"echeance|payment\s+due(?:\s+date)?|due\s+on|due\s+by|due|payable\s+(?:avant\s+le|le|by)|pay(?:ment)?\s+by|a\s+regler\s+avant\s+le|"
    r"(?:valable|valid)\s+(?:jusqu'au|until)|expir\w*|date\s+limite\s+de\s+paiement)"
    r"(?![a-z])\s*[:#.\-–]*\s*"
)
PO_LABEL = re.compile(
    r"^(?:(?:purchase\s+order|order)\s*(?:no\.?|number|num\w*|#|ref\w*)?|"
    r"p\.?o\.?\s*(?:no\.?|number|num\w*|#|ref\w*)?|"
    r"bon\s+de\s+commande(?:\s*(?:no\.?|n[°o0]\.?|num\w*|#|ref\w*))?|"
    r"(?:no\.?|n[°o0]\.?|num\w*|reference|ref\.?)\s*(?:de\s+|d['e]\s*)?(?:commande|bon\s+de\s+commande|bc)|"
    r"(?:bc|b\.c\.?)\s*(?:no\.?|n[°o0]\.?|num\w*|#|ref\w*)?|"
    r"(?:your|votre)\s+ref\w*|customer\s+po|commande\s+n[°o0]\.?|n[°o0]\s*commande)"
    r"(?![a-z])\s*[:#.\-–]*\s*"
)
CUSTOMER_LABEL = re.compile(
    r"^(?:bill(?:ed)?\s*to|invoice\s*to|invoiced\s+to|sold\s*to|customer(?:\s+(?:name|details|info\w*))?|client(?:\s+(?:name|nom|details))?|"
    r"coordonnees(?:\s+client)?|factur[e]e?\s+a|facture\s+a|destinataire|dest\.?(?=\s|:|$)|adresse\s+de\s+facturation|"
    r"billing\s+(?:address|details|info\w*)|buyer(?:\s+name)?|acheteur|purchaser|attn\.?|doit(?=\s*:)|to(?=\s*:|\s*$))"
    r"(?![a-z])(?!\s*(?:no|n[°o]|num\w*|id|ref\w*|code|po)\b)\s*[:\-–]*\s*"
)
SUBHEADER_LABEL = re.compile(
    r"^(?:coordonnees?(?:\s+(?:client|fournisseur))?|client\s+details|customer\s+details|"
    r"adresse\s+(?:de\s+)?(?:livraison|facturation)|delivery\s+address|billing\s+address|shipping\s+address|"
    r"(?:client|customer|fournisseur|supplier)\s*$)"
)
SUPPLIER_LABEL = re.compile(
    r"^(?:"
    # --- English ---
    r"from|supplier(?:\s+(?:name|details|info\w*|address))?|vendor(?:\s+(?:name|details|info\w*))?|"
    r"seller|company(?:\s+name)?|business(?:\s+name)?|issued\s+by|billed\s+from|sold\s+by|"
    # --- French ---
    r"fournisseur(?:\s+(?:nom|details|info\w*|adresse))?|nom\s+du\s+fournisseur|"
    r"vendeur|emetteur|"
    r"raison\s+sociale|denomination\s+sociale|societe|entreprise|etablissement|prestataire|"
    r"emis\s+par"
    r")(?![a-z])\s*[:\-–]*\s*"
)
SHIP_LABEL = re.compile(r"^(?:ship(?:ped)?\s*to|deliver\w*\s*(?:to|address)|livr\w+|adresse\s+de\s+livraison)")
PAYMENT_TERMS_LABEL = re.compile(
    r"^(?:"
    # --- English ---
    r"payment\s+(?:terms|method|mode|type|details|info\w*|instructions|conditions?)|"
    r"method\s+of\s+payment|terms\s+of\s+payment|payment\s+conditions?|"
    r"bank\s+transfer|wire\s+transfer|"
    # --- French ---
    r"conditions?\s+de\s+(?:paiement|reglement)|modalites?\s+de\s+(?:paiement|reglement)|"
    r"mode\s+de\s+(?:paiement|reglement)|moyen\s+de\s+(?:paiement|reglement)|"
    r"paiement\s+(?:par|a\s+terme|comptant)|reglement(?:\s+par)?|"
    r"virement(?:\s+bancaire)?|cheque|especes|carte\s+bancaire"
    r")"
)
AMOUNT_WORDS_LABEL = re.compile(
    r"^(?:amount\s+in\s+words|montant\s+en\s+(?:toutes\s+)?lettres|"
    r"arrete\w*\s+(?:(?:la\s+presente|le\s+present)\s+)?(?:facture|devis|pro\s*forma|somme)|"
    r"somme\s+en\s+lettres|"
    r"total\s+en\s+lettres|"
    r"le\s+montant\s+en\s+lettres|"
    r"say\s*:?|in\s+words)"
)
IBAN_LABEL = re.compile(
    r"^(?:"
    r"iban(?:\s*(?:no\.?|n[°o0]\.?|number|num[eé]ro))?|"
    r"n[°o0]\.?\s*iban|num[eé]ro\s+iban|"
    r"international\s+bank\s+account(?:\s+number)?"
    r"|compte\s+iban"
    r")\s*[:#.\-–]*\s*"
)
SWIFT_LABEL = re.compile(
    r"^(?:"
    r"swift(?:\s*/\s*bic)?(?:\s*(?:code|no\.?))?|"
    r"bic(?:\s*/\s*swift)?(?:\s*code)?|"
    r"code\s+(?:swift|bic)|"
    r"bank\s+identifier\s+(?:code|no\.?)|"
    r"swift\s*code"
    r")\s*[:#.\-–]*\s*"
)
BANK_NAME_LABEL = re.compile(
    r"^(?:"
    r"bank(?:\s+name)?|banque(?:\s+(?:domiciliataire|du\s+fournisseur))?|"
    r"nom\s+(?:de\s+la\s+)?banque|etablissement\s+bancaire|"
    r"beneficiary\s+bank|account\s+bank|banking\s+institution"
    r")\s*[:#.\-–]*\s*"
)
BANK_ACCOUNT_LABEL = re.compile(
    r"^(?:"
    r"(?:bank\s+)?account(?:\s*(?:no\.?|number|n[°o0]\.?))?|"
    r"n[°o0]\s+(?:de\s+)?compte|num[eé]ro\s+(?:de\s+)?compte|"
    r"comptes?\s+bancaires?(?:\s*(?:no\.?|n[°o0]\.?))?|"
    r"compte\s+(?:bancaire|n[°o0]\.?)|"
    r"rib(?:\s*(?:no\.?|n[°o0]\.?))?|relev[eé]\s+d['']identit[eé]\s+bancaire|"
    r"r[eé]f[eé]rence\s+bancaire|coordonn[eé]es\s+bancaires?|"
    r"a/c\s*no\.?|bank\s+details"
    r")\s*[:#.\-–]*\s*"
)
BANK_HEADER_LABEL = re.compile(
    r"^(?:"
    r"comptes?\s+bancaires?|coordonn[eé]es\s+bancaires?|"
    r"r[eé]f[eé]rence\s+bancaire|bank\s+(?:details|accounts?|information|info\w*)|"
    r"bankverbindung|payment\s+details"
    r")\s*[:#.\-–]*\s*"
)
BANK_CODE_LINE = re.compile(
    r"^(boa|bni|bmoi|bfv(?:-sg)?|bnp(?:\s*paribas)?|societe\s+generale|sgb|hsbc|barclays|stanbic|ecobank|"
    r"access\s+bank|bank\s+of\s+africa|mcb|maubank|sbi|cim\s+finance|bgfi|orabank|coris\s+bank|"
    r"credit\s+agricole|lcl|banque\s+populaire|caisse\s+d['']epargne|bred|cic|la\s+banque\s+postale|"
    r"attijariwafa|cih|bmce|awb|equity\s+bank|kcb|absa|nedbank|standard\s+bank|fnb|capitec|"
    r"afriland|rawbank|trust\s+merchant|absa|bicec|uba|bgfibs|mufg|jpmorgan)\b\s*[:#\-]?\s*(.+)$",
    re.I,
)

TITLE_ONLY = re.compile(
    r"^(?:tax\s+)?(?:invoice|facture|bill|receipt|recu|quote|devis|credit\s+note|avoir|proforma(?:\s+invoice)?|"
    r"pro\s*forma|statement|original|copy|copie|duplicata)\b[\s\-:#.n°o0-9a-z/]*$"
)

_TAXID_LABELS = (
    r"tax\s*id|tax\s*no\.?|tax\s*number|tax\s*identification(?:\s+number)?|"
    r"fiscal\s*(?:id|no\.?|number|identification)?|"
    r"identifiant\s+fiscal|identification\s+fiscale|num[eé]ro\s+d['']\s*identification\s+fiscale|"
    r"vat(?:\s*(?:no\.?|number|id|reg\w*))?|tva(?:\s*intra\w*)?|n[°o]\s*tva|"
    r"tin|nif|stat|siret|siren|cif|rcs|rc|ein|gst(?:\s*(?:no\.?|number|id))?|abn|uen|"
    r"business\s+reg(?:istration)?\.?\s*(?:no\.?|number)?|"
    r"company\s+reg(?:istration)?\.?\s*(?:no\.?|number)?|"
    r"reg(?:istration)?\.?\s*(?:no\.?|number)|company\s*(?:no\.?|number|reg\w*)"
)
TAXID_RE = re.compile(rf"\b({_TAXID_LABELS})\b\s*[:#.\-]?\s*")
CONTACT_LINE = re.compile(
    r"^(?:tel|tél|phone|telephone|mobile|mob|fax|e-?mail|mail|web(?:site)?|site|www|http|contact|iban|bic|swift|rib|"
    r"comptes?\s+bancaires?|compte\s+bancaire|boa|bni|bmoi|bfv(?:-sg)?|bnp|sgb|hsbc|barclays|stanbic|ecobank|"
    r"access\s+bank|bank|banque|mcb|maubank|bgfi|orabank|coris|afriland|rawbank)\b|@|^www\.|https?://"
)
LEGAL_ENTITY = re.compile(
    r"\b(?:sarl|sarlu|sa|sau|sas|sasu|eurl|llc|ltd|limited|inc|incorporated|corp|corporation|gmbh|plc|bv|nv|pty|srl|spa|ets|cie|co\.)\b"
)

# Totals section
_NOT_TOTAL = r"(?!\s*(?:pages?|items?|qty|quantity|quantite|articles?|weight|poids|colis|lines?|lignes?|units?|unites?|hours?|heures?))"
RE_SUBTOTAL = re.compile(
    r"^(?:sub[\s\-]?total|sous[\s\-]?total|total\s+(?:ht|h\.t\.?|hors\s+tax\w*|excl\w*[\w\s.]*|before\s+tax\w*|net)\b|"
    r"montant\s+(?:ht|h\.t\.?|hors\s+tax\w*|net\b)|net\s+commercial|net\s+total|total\s+net|amount\s+before\s+tax|"
    r"net\s+amount|taxable\s+(?:amount|base|total)|base\s+(?:ht|imposable))"
)
RE_DISCOUNT = re.compile(r"^(?:total\s+)?(?:discounts?|remises?|rabais|reductions?)\b")
RE_CHARGE = re.compile(r"^(?:shipping|freight|delivery|livraison|transport|handling|frais|surcharge|postage|packing|emballage|fees?)\b")
RE_BALANCE = re.compile(
    r"^(?:balance(?:\s+due)?|amount\s+due|reste\s+(?:a\s+)?payer|solde(?:\s+(?:du|restant|a\s+payer))?|outstanding(?:\s+balance)?|"
    r"montant\s+restant|remaining\s+balance|balance\s+remaining)"
)
RE_PAID = re.compile(
    r"^(?:amount\s+paid|total\s+paid|paid(?:\s+to\s+date)?|payments?\s+(?:received|made)|"
    r"payments?(?![a-z\s]*(?:terms|method|details|instructions|info|due|conditions|date))|deposits?|acompte|avance|"
    r"deja\s+(?:paye|verse)|paiements?\s+(?:recus?|effectues?)|versement)\b"
)
RE_TAX_RATE = re.compile(
    r"^(?:taux|rate)\s+(?:de\s+|of\s+|d['e]\s*)?(?:tva|vat|taxe?)\b"
)
RE_TAX = re.compile(
    r"^(?:total\s+)?(?:vat|tva|tax(?:es)?|taxe|gst|sales\s+tax|tps|tvq|hst|pst|impot|montant\s+(?:tva|taxe))\b"
)
RE_TOTAL_STRONG = re.compile(
    r"^(?:grand\s+total|total\s+(?:ttc|t\.t\.c\.?|due|amount|a\s+payer|to\s+pay|invoice|incl\w*[\w\s.]*|gross|general|global)\b|"
    r"net\s+a\s+payer|montant\s+(?:ttc|total|a\s+payer)|invoice\s+total|total\s+general|amount\s+total|total\s+price)"
)
RE_TOTAL_PLAIN = re.compile(rf"^total\b{_NOT_TOTAL}")
NOT_A_CHARGE = re.compile(r"gratuit|offert|a partir|franco|offre|minimum|des\s+\d|free")
TOTALS_ANY = [RE_SUBTOTAL, RE_DISCOUNT, RE_CHARGE, RE_BALANCE, RE_PAID, RE_TAX, RE_TOTAL_STRONG, RE_TOTAL_PLAIN]
FOOTER_STOP = re.compile(
    r"^(?:payment\s+(?:terms|method|details|info\w*|instructions)|bank\b|banque|iban|bic\b|swift|terms\b|conditions|notes?\b|"
    r"merci|thank|please|rib\b|mode\s+de|reglement|signature|late\s+payment|penalit|comptes?\s+bancaires?|account|compte|"
    r"amount\s+in\s+words|montant\s+en\s+(?:toutes\s+)?lettres|arrete\w*\s+(?:la\s+presente\s+)?(?:facture|devis|pro\s*forma|somme))"
)
SKIP_LINE = re.compile(r"^(?:page\s*\d|www\.|https?://)")
PAGE_NUM = re.compile(r"\bpage\s*\d+\s*(?:of|/|sur)\s*\d+|\bpage\s+\d+\b")

# --------------------------------------------------------------------------------------
# Table column vocabulary
# --------------------------------------------------------------------------------------
_PHRASES: list[tuple[tuple[str, ...], str]] = []


def _add(kind: str, *phrases: tuple[str, ...]) -> None:
    for p in phrases:
        _PHRASES.append((p, kind))


_add("tax_amount", ("tax", "amount"), ("vat", "amount"), ("montant", "tva"), ("montant", "taxe"), ("tva", "montant"))
_add("unit_price", ("unit", "price"), ("prix", "unitaire"), ("unit", "cost"), ("unit", "rate"), ("prix", "unit"),
     ("p.u",), ("p.u.",), ("pu",), ("pu", "ht"), ("price", "unit"), ("prix", "unitaire", "ht"),
     ("net", "price"), ("prix", "net"), ("unit", "rate"), ("rate",), ("tarif",), ("cost",),
     ("prix", "ht"), ("p.u.", "ht"), ("prix", "u"), ("prix", "unitaire"), ("prix", "unite"),
     ("pu", "ttc"), ("prix", "unitaire", "ttc"))
_add("amount", ("line", "total"), ("net", "amount"), ("montant", "ht"), ("total", "ht"), ("total", "price"),
     ("amount",), ("total",), ("montant",), ("extended",), ("ttc",), ("sum",), ("montant", "ttc"), ("total", "ttc"),
     ("montant", "ligne"), ("total", "ligne"), ("sous", "total"), ("ligne", "montant"), ("montant", "total"))
_add("qty", ("ate",), ("qty",), ("qte",), ("quantity",), ("quantite",), ("quantites",), ("units",), ("nb",), ("nbre",),
     ("hours",), ("hrs",), ("heures",), ("days",), ("jours",), ("qty.",), ("qte.",), ("quantite",), ("qté",))
_add("discount", ("discount",), ("disc",), ("remise",), ("rabais",), ("reduction",), ("disc.",), ("%disc",), ("remise", "%"))
_add("tax", ("tax", "rate"), ("tax",), ("vat",), ("tva",), ("taxe",), ("gst",), ("taux",), ("tax%",), ("vat%",), ("tva%",), ("taux", "tva"))
_add("desc", ("description",), ("designation",), ("libelle",), ("item",), ("items",), ("article",), ("articles",),
     ("produit",), ("produits",), ("product",), ("products",), ("service",), ("services",), ("details",),
     ("detail",), ("nature",), ("intitule",), ("particulars",), ("descriptions",), ("prestation",), ("prestation", "description"))
_add("ignore", ("no",), ("n",), ("#",), ("ref",), ("reference",), ("sku",), ("code",), ("pos",), ("ligne",), ("line",),
     ("unit",), ("uom",), ("unite",), ("u",), ("n°",), ("num",), ("id",), ("code", "article"))
_PHRASES.sort(key=lambda t: -len(t[0]))

UOM_WORDS = {"pcs", "pc", "pce", "pces", "piece", "pieces", "kg", "g", "h", "hr", "hrs", "u", "unit", "units",
             "unite", "unites", "l", "m", "m2", "m3", "set", "box", "lot", "day", "days", "jour", "jours", "each", "ea"}
CURRENCY_TOKENS = {"usd", "eur", "gbp", "mga", "ar", "ariary", "cad", "chf", "aud", "fcfa", "xof", "xaf", "$", "€", "£"}


def _tok(text: str) -> str:
    t = fold(text).strip()
    t = re.sub(r"^[\(\[]+|[\)\]:;,]+$", "", t)
    if t.endswith(".") and len(t) > 2 and t[:-1] not in ("p.u", "p.u."):
        t = t[:-1]
    return t


@dataclass
class Col:
    kind: str
    x0: float
    x1: float

    @property
    def cx(self) -> float:
        return (self.x0 + self.x1) / 2


def detect_header(line: Line) -> list[Col] | None:
    if any(parse_number(w.text) is not None for w in line.words):
        return None
    cols: list[Col] = []
    # Match phrases inside one cell only, so adjacent header cells ("TVA" | "Montant HT") never merge.
    for cell in line.cells:
        words = cell.words
        toks = [_tok(w.text) for w in words]
        i = 0
        while i < len(toks):
            matched = False
            for phrase, kind in _PHRASES:
                n = len(phrase)
                if tuple(toks[i : i + n]) == phrase:
                    cols.append(Col(kind, words[i].x0, words[i + n - 1].x1))
                    i += n
                    matched = True
                    break
            if not matched:
                i += 1
    kinds = {c.kind for c in cols} - {"ignore"}
    if len(kinds) < 2:
        return None
    if not ("amount" in kinds or "unit_price" in kinds):
        return None
    if not ("desc" in kinds or len(kinds) >= 3):
        return None
    # Header lines are made mostly of header words
    matched_words = sum(1 for c in cols for w in line.words if w.x0 >= c.x0 - 0.5 and w.x1 <= c.x1 + 0.5)
    if matched_words < 0.5 * len(line.words):
        return None
    # no amount column but "price" without a quantity column -> that price is the amount
    if "amount" not in kinds and "qty" not in kinds:
        for c in cols:
            if c.kind == "unit_price":
                c.kind = "amount"
    # keep a single column per kind (first occurrence), except ignore
    seen: set[str] = set()
    uniq: list[Col] = []
    for c in cols:
        if c.kind in seen and c.kind != "ignore":
            continue
        seen.add(c.kind)
        uniq.append(c)
    return uniq


# --------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------
def conf_flag(conf: float | None) -> bool:
    return conf is not None and conf < LOW_CONF


def _first_date(text: str) -> str | None:
    f = fold(text)
    m = DATE_RE.search(f)
    return text[m.start() : m.end()] if m else None


_NON_CODE_WORDS = {
    "total", "subtotal", "sous", "tax", "tva", "amount", "montant", "net", "page",
    "reference", "ref", "number", "numero", "none", "null", "n/a", "na",
    "doit", "pro", "proforma", "test", "de", "du", "des", "le", "la", "les",
    "et", "of", "the", "to", "for", "and", "from", "with", "without",
    "bon", "commande", "devis", "avoir", "facture", "invoice", "client",
    "customer", "fournisseur", "supplier", "address", "adresse", "tel",
    "email", "phone", "fax", "web", "site", "coordonnees",
}


def _inv_no(text: str, *, allow_alpha: bool = True) -> str | None:
    """Extract an invoice number token from the text that follows a label.

    The function is deliberately permissive about format (letters, digits, hyphens,
    slashes, dots, underscores) but rejects values that look like dates, plain label
    words, or non-code strings.

    Length cap: 50 chars (raised from 30 to handle long vendor-specific references).
    """
    t = text.strip(" :#.-–—\t")
    # Strip a leading number-indicator prefix if one slipped through the label regex
    t = re.sub(r"^(?:n[°º]|no\.?|nr\.?|num(?:[eé]ro|ber)?\.?|#)\s*[:.\-]*\s*", "", t, flags=re.I)
    m = re.match(r"([A-Za-z0-9][A-Za-z0-9\-/_.#]*)", t)
    if not m:
        return None
    tok = m.group(1).rstrip(".-/_#")
    if not tok or len(tok) > 50:   # raised from 30 to 50
        return None
    if DATE_RE.fullmatch(fold(tok)):
        return None
    if not re.search(r"\d", tok):
        if not allow_alpha:
            return None
        # An all-letter token is valid only if it looks like a vendor code (contains
        # a separator, is all-uppercase, or is very short like "ABS") AND is not a
        # plain label word or month name.
        f = fold_and_norm(tok)
        code_like = len(tok) >= 2 and (
            re.fullmatch(r"[A-Z\-/_.#]+", tok) is not None or re.search(r"[-/_.#]", tok) is not None
        )
        if (not code_like or is_section_label(f) or f in _NON_CODE_WORDS
                or re.fullmatch(_MONTHS, f)):
            return None
    return tok


def _inv_no_strict(text: str) -> str | None:
    return _inv_no(text, allow_alpha=False)


def _ref_token(text: str) -> str | None:
    return _inv_no(text)


def find_value(lines: list[Line], label_re: re.Pattern, extract, *, limit: int | None = None,
               neighbor_extract=None):
    """Find `label` then its value: in the same cell, in the next cell(s), or right below."""
    end = len(lines) if limit is None else min(limit, len(lines))
    side = neighbor_extract or extract
    for li in range(end):
        line = lines[li]
        for ci, cell in enumerate(line.cells):
            f = fold_and_norm(cell.text)
            m = label_re.match(f)
            if not m:
                continue
            v = extract(cell.text[m.end():])
            if v:
                return v, cell.conf, li
            for nc in line.cells[ci + 1 : ci + 3]:
                v = side(nc.text)
                if v:
                    return v, nc.conf, li
            for nl in lines[li + 1 : li + 3]:
                if nl.page != line.page or nl.y0 - line.y1 > 2.2 * line.h:
                    continue
                for nc in nl.cells:
                    if abs(nc.cx - cell.cx) < max(70.0, (cell.x1 - cell.x0)) or (nc.x0 < cell.x1 and nc.x1 > cell.x0):
                        v = side(nc.text)
                        if v:
                            return v, nc.conf, li
    return None


def _plain_text(text: str) -> str | None:
    t = text.strip(" :#.-–—\t\r\n")
    t = re.sub(r"\s+", " ", t)
    if not t or not re.search(r"[A-Za-zÀ-ÿ]{2}", t):
        return None
    f = fold_and_norm(t)
    if is_section_label(f) or TAXID_RE.search(f):
        return None
    # Allow bank names even if they look like org names
    return t


def _terms_text(text: str) -> str | None:
    t = text.strip(" :#.-–—\t\r\n")
    t = re.sub(r"\s+", " ", t)
    if not t or not re.search(r"[A-Za-zÀ-ÿ]{2}", t):
        return None
    f = fold_and_norm(t)
    if DATE_RE.fullmatch(f) or TAXID_RE.search(f):
        return None
    if re.fullmatch(r"(?:net\s+)?(?:a\s+)?(?:payer|regler|payable)(?:\s+(?:ttc|ht|tva))?\s*:?", f):
        return None
    if re.search(r"\b(?:bon\s+pour\s+accord|lu\s+et\s+approuve|signature|cachet|service\s+commercial)\b", f):
        return None
    # A payment-method keyword ("Virement", "Chèque", "Cash", "Virement bancaire") is a
    # valid value even though it also matches PAYMENT_TERMS_LABEL.  Allow it through.
    # Only reject labels that are NOT payment-method values (e.g. invoice/date/address labels).
    if is_section_label(f) and not re.search(r"\d", f):
        # Exception: if the label is itself a payment method keyword, keep it as the value
        if not PAYMENT_TERMS_LABEL.match(f):
            return None
    return t


def _account_text(text: str) -> str | None:
    t = text.strip(" :#.-–—\t\r\n")
    t = re.sub(r"\s+", " ", t)
    if not t or not re.search(r"\d", t):
        return None
    f = fold_and_norm(t)
    if TAXID_RE.search(f) or (is_section_label(f) and not BANK_CODE_LINE.match(f)):
        return None
    return t


def _iban_value(text: str) -> str | None:
    t = re.sub(r"\s+", "", text).upper()
    m = re.search(r"\b[A-Z]{2}\d{2}[A-Z0-9]{10,30}\b", t)
    return m.group(0) if m else None


def _swift_value(text: str) -> str | None:
    t = re.sub(r"[^A-Za-z0-9]", "", text).upper()
    m = re.search(r"\b[A-Z]{6}[A-Z0-9]{2}(?:[A-Z0-9]{3})?\b", t)
    return m.group(0) if m else None


def find_terms(lines: list[Line]):
    """Payment terms may be on the label line, in the next cell, or a few lines below."""
    for li, line in enumerate(lines):
        for ci, cell in enumerate(line.cells):
            m = PAYMENT_TERMS_LABEL.match(fold_and_norm(cell.text))
            if not m:
                continue
            v = _terms_text(cell.text[m.end():])
            if v:
                return v, cell.conf, li
            for nc in line.cells[ci + 1 : ci + 3]:
                v = _terms_text(nc.text)
                if v:
                    return v, nc.conf, li
            for nl in lines[li + 1 : li + 5]:
                if nl.page != line.page or nl.y0 - line.y1 > 2.6 * max(line.h, nl.h):
                    break
                folded_line = fold_and_norm(" ".join(c.text for c in nl.cells))
                if (re.search(r"\b(?:bon\s+pour\s+accord|lu\s+et\s+approuve|signature|cachet|service\s+commercial)\b",
                              folded_line)
                        or any(p.match(folded_line) for p in TOTALS_ANY)):
                    break
                for nc in nl.cells:
                    if abs(nc.cx - cell.cx) < max(90.0, cell.x1 - cell.x0) or (nc.x0 < cell.x1 and nc.x1 > cell.x0):
                        v = _terms_text(nc.text)
                        if v:
                            return v, nc.conf, li
    return None


def _known_bank_line(text: str) -> tuple[str, str] | None:
    t = text.strip(" :#.-–—\t\r\n")
    m = BANK_CODE_LINE.match(t)
    if not m:
        return None
    label, value = m.group(1).strip().upper(), m.group(2).strip(" :#.-–—\t")
    if len(re.sub(r"\D", "", value)) >= 6:
        return label, value
    return None


def _bank_line(text: str) -> tuple[str, str] | None:
    known = _known_bank_line(text)
    if known:
        return known
    t = text.strip(" :#.-–—\t\r\n")
    if not t:
        return None
    f = fold(t)
    if CONTACT_LINE.search(f) or TAXID_RE.search(f):
        return None
    m = re.match(r"^([A-Za-z][A-Za-z0-9 .&/\-]{1,24})\s*:\s*(.+)$", t)
    if not m:
        return None
    label, value = m.group(1).strip(), m.group(2).strip()
    if re.search(r"\b(?:compte|account|iban|rib|swift|bic|bank|banque|tel|contact|standard|mobile|fax|mail|email)\b", fold(label)):
        return None
    if len(re.sub(r"\D", "", value)) < 6:
        return None
    return label.upper(), value


def find_bank_details(lines: list[Line]) -> list[tuple[str, str]]:
    """Structured bank lines under a "Comptes bancaires" header, or known bank codes."""
    for li, line in enumerate(lines):
        for ci, cell in enumerate(line.cells):
            m = BANK_HEADER_LABEL.match(fold_and_norm(cell.text))
            if not m:
                continue
            details: list[tuple[str, str]] = []
            first = _bank_line(cell.text[m.end():])
            if first:
                details.append(first)
            for nl in lines[li + 1 : li + 5]:
                if nl.page != line.page or nl.y0 - line.y1 > 3.0 * max(line.h, nl.h):
                    break
                found = _bank_line(" ".join(c.text for c in nl.cells))
                if found:
                    details.append(found)
                elif details:
                    break
            if details:
                return details

    details = []
    for line in lines:
        for cell in line.cells:
            found = _known_bank_line(cell.text)
            if found and found not in details:
                details.append(found)
                if len(details) >= 3:
                    return details
    return details


def find_amount_words(lines: list[Line]):
    """Collect the amount-in-words line (and its wrapped continuation)."""
    for li, line in enumerate(lines):
        for ci, cell in enumerate(line.cells):
            m = AMOUNT_WORDS_LABEL.match(fold_and_norm(cell.text))
            if not m:
                continue
            parts: list[str] = []
            rest = cell.text[m.end():].strip(" :#.-–—\t")
            rest = re.sub(
                r"^[^A-Za-zÀ-ÿ]*(?:(?:[àa]\s+)?la\s+somme(?:\s+totale)?\s+de|somme\s+de)\s*[:#.\-–—\s]*",
                "",
                rest,
                flags=re.I,
            )
            if rest and not is_section_label(fold_and_norm(rest)):
                parts.append(rest)
            for nc in line.cells[ci + 1 : ci + 3]:
                t = nc.text.strip(" :#.-–—\t")
                if t and not is_section_label(fold_and_norm(t)):
                    parts.append(t)
            for nl in lines[li + 1 : li + 4]:
                if nl.page != line.page or nl.y0 - line.y1 > 2.8 * max(line.h, nl.h):
                    break
                t = " ".join(c.text for c in nl.cells).strip()
                f = fold_and_norm(t)
                if (is_section_label(f) or TAXID_RE.search(f) or CONTACT_LINE.search(f)
                        or re.search(r"\b(?:signature|cachet|page)\b", f)
                        or not re.search(r"[A-Za-zÀ-ÿ]{2}", t)):
                    break
                parts.append(t)
            value = " ".join(parts).strip(" :#.-–—\t")
            if not value:
                for pl in reversed(lines[max(0, li - 2) : li]):
                    if pl.page != line.page or line.y0 - pl.y1 > 2.8 * max(line.h, pl.h):
                        continue
                    t = " ".join(c.text for c in pl.cells).strip(" :#.-–—\t")
                    f = fold_and_norm(t)
                    if (re.search(r"\b(?:mille|cent|million|milliard|thousand|hundred|billion)\b", f)
                            and not re.search(r"\d", t) and not is_section_label(f)
                            and not TAXID_RE.search(f) and not CONTACT_LINE.search(f)):
                        value = t
                        break
            if value and re.search(r"[A-Za-zÀ-ÿ]{2}", value):
                return value, cell.conf, li
    return None


def is_party_label(folded_text: str) -> bool:
    return bool(CUSTOMER_LABEL.match(folded_text) or SUPPLIER_LABEL.match(folded_text))


def is_section_label(folded_text: str) -> bool:
    # Don't treat known bank names as generic section labels that block values
    if BANK_CODE_LINE.match(folded_text) and not re.search(r"\d", folded_text):
        # bank name alone, not a label line with value in usual form? maybe skip treating as blocking
        pass
    return bool(
        INV_NO_LABEL.match(folded_text)
        or INV_NO_LABEL_ALT.match(folded_text)
        or INV_REF_LABEL.match(folded_text)
        or INV_NO_LABEL_FR.match(folded_text)
        or ISSUE_LABEL.match(folded_text)
        or DUE_LABEL.match(folded_text)
        or PO_LABEL.match(folded_text)
        or CUSTOMER_LABEL.match(folded_text)
        or SUPPLIER_LABEL.match(folded_text)
        or SHIP_LABEL.match(folded_text)
        or PAYMENT_TERMS_LABEL.match(folded_text)
        or AMOUNT_WORDS_LABEL.match(folded_text)
        or IBAN_LABEL.match(folded_text)
        or SWIFT_LABEL.match(folded_text)
        or BANK_NAME_LABEL.match(folded_text)
        or BANK_ACCOUNT_LABEL.match(folded_text)
        or BANK_HEADER_LABEL.match(folded_text)
        or (BANK_CODE_LINE.match(folded_text) and re.search(r"[:#]", folded_text))
    )


def block_below(lines: list[Line], li: int, ci: int, *, first_text: str | None, hard_end: int, max_lines: int = 6,
                left_edge: float | None = None) -> list[str]:
    """Text of the column block starting at cell (li, ci): first_text then lines underneath."""
    line = lines[li]
    cell = line.cells[ci]
    if left_edge is None:
        left_edge = cell.x0
    out: list[str] = []
    if first_text and first_text.strip():
        out.append(first_text.strip())
    prev = line
    for nj in range(li + 1, min(hard_end, len(lines))):
        nl = lines[nj]
        if nl.page != prev.page or nl.y0 - prev.y1 > 2.6 * max(prev.h, nl.h):
            break
        picked = [c for c in nl.cells if left_edge - 25 <= c.x0 <= left_edge + 180]
        if not picked or abs(picked[0].x0 - left_edge) > 25:
            break
        text = " ".join(c.text for c in picked).strip()
        if detect_header(nl):
            break
        folded = fold_and_norm(text)
        if SUBHEADER_LABEL.match(folded):
            prev = nl
            continue
        if is_section_label(folded):
            # A party name can legitimately start with a label word ("Client Example SA").
            # Keep the first body line in that case, but stop on explicit "Label: value"
            # fields and on every non-party section label.
            if out or not is_party_label(folded) or re.search(r"[:#]", text):
                break
        out.append(text)
        prev = nl
        if len(out) >= max_lines:
            break
    return out


def clean_party(block: list[str]) -> Party:
    """Turn a list of block lines into name / address / tax id; drop contact details.

    Improvements over the original:
    - Recognises French address prefixes (Siège social, Adresse administrative, Agence…)
      and strips them before storing in the address field.
    - Keeps address lines that start with known geographic tokens (Lot, Rue, BP, Quartier…).
    - Preserves leading zeros in tax-id values.
    """
    _ADDR_PREFIX = re.compile(
        r"^(?:si[eè]ge\s+(?:social)?|adresse\s+(?:administrative|commerciale|du\s+fournisseur|"
        r"de\s+facturation|de\s+l['']\s*entreprise)?|agence|[eé]tablissement|"
        r"head\s+(?:office|quarters?)|registered\s+address|company\s+address|"
        r"business\s+address|vendor\s+address|supplier\s+address)\s*[:\-–]?\s*",
        re.I,
    )
    _GEO_LINE = re.compile(
        r"^(?:lot\s|rue\s|bd\s|boulevard\s|avenue\s|av\.\s|route\s|rte\s|"
        r"quartier\s|cite\s|villa\s|zone\s|immeuble\s|bat(?:iment)?\s|"
        r"bp\s|b\.p\.\s|po\s+box|po\.\s+box|"
        r"\d{4,5}\s|cp\s|\d+\s+(?:rue|avenue|bd|boulevard))",
        re.I,
    )

    name = ""
    address_lines: list[str] = []
    tax_ids: list[str] = []
    for idx, raw in enumerate(block):
        text = raw.strip(" ,;")
        if not text:
            continue
        f = fold(text)
        # tax ids can sit in any line
        matches = list(TAXID_RE.finditer(f))
        if matches:
            for k, m in enumerate(matches):
                end = matches[k + 1].start() if k + 1 < len(matches) else len(text)
                val = text[m.end():end].strip(" ,;|")
                if re.search(r"\d", val):
                    tax_ids.append(f"{text[m.start():m.end()].strip(' :#.-').upper()}: {val}")
            before = text[: matches[0].start()].strip(" ,;|-")
            if not before:
                continue
            text, f = before, fold(before)
        if CONTACT_LINE.search(f):
            continue
        if re.fullmatch(r"[+\d][\d\s().\-]{7,}", text):  # bare phone number
            continue
        # Strip address-section prefixes (Siège social :, Adresse :, …)
        addr_m = _ADDR_PREFIX.match(text)
        if addr_m:
            remainder = text[addr_m.end():].strip(" ,;")
            if remainder:
                address_lines.append(remainder)
            else:
                # prefix-only line — the next line carries the actual address text;
                # mark that we want to force-include it as address even if name not yet set
                pass
            continue
        text = re.sub(r"^(?:adresse|address|addr)\s*[:\-]\s*", "", text, flags=re.I)
        if not name:
            name = text
        else:
            address_lines.append(text)
    return Party(name=name, address=", ".join(address_lines), tax_id=" | ".join(tax_ids))


def prefer_full_own_name(p: Party) -> Party:
    """'HITA' on the first line and 'HUILERIE INDUSTRIELLE DE TAMATAVE' on the second: keep the long name."""
    first = p.address.split(", ")[0] if p.address else ""
    if first and matches_own_company(p.name) and matches_own_company(first) and len(first) > len(p.name):
        p.address = ", ".join(p.address.split(", ")[1:])
        p.name = first
    return p


def other_tax_ids(lines: list[Line], exclude_digits: set[str]) -> str:
    """NIF / STAT / CIF / RCS printed anywhere (header, footer) that do not belong to the customer.

    Recognises explicit label+value pairs as well as inline "NIF: 123" patterns.
    The label variants handled include:
      - "NIF", "N° NIF", "Numéro NIF", "Identifiant fiscal", "Identification fiscale"
      - "STAT", "N° STAT", "Numéro STAT"
      - "CIF", "N° CIF"  /  "RCS", "N° RCS", "Numéro RCS"
      - "Tax ID", "Tax Number", "TIN", "VAT Number", "Business Registration Number", etc.
    """
    # Canonical display label for each key
    wanted = {
        "nif":   "NIF",
        "stat":  "STAT",
        "tin":   "TIN",
        "siret": "SIRET",
        "siren": "SIREN",
        "ein":   "EIN",
        "abn":   "ABN",
        "rcs":   "RCS",
        "rc":    "RC",
        "cif":   "CIF",
    }

    # Pattern for numeric identifiers (NIF, STAT, TIN, SIRET, EIN, ABN …)
    # Handles labels such as:
    #   "NIF", "N° NIF", "Numéro NIF", "Numéro d'identification fiscale",
    #   "Identifiant fiscal", "Identification fiscale",
    #   "Tax ID", "Tax Number", "TIN", "VAT Number", "VAT No."
    numeric_pat = re.compile(
        r"(?<![a-z])"
        r"(?:n[°o0]\.?\s*|num[eé]ro\s+(?:d['']\s*)?|identifiant\s+|identification\s+)?"
        r"(nif|stat|tin|siret|siren|ein|abn"
        r"|tax\s*(?:id|no\.?|number|identification)"
        r"|fiscal\s*(?:id|no\.?|number|identification)?"
        r")"
        r"\s*(?:no\.?|n[°o0]\.?)?\s*[:.]?\s*"
        r"(\d[\d ./\-]*\d)"
    )
    # VAT / GST values are often alphanumeric (FR12345, GB123456789, etc.)
    # Must use re.I because fold() lowercases everything
    vat_alpha_pat = re.compile(
        r"(?<![a-z])"
        r"(vat\s*(?:no\.?|number|id|reg\w*)?|gst\s*(?:no\.?|number|id)?)"
        r"\s*(?:no\.?|n[°o0]\.?)?\s*[:.]?\s*"
        r"([A-Za-z]{0,3}\d[\d ./\-A-Za-z]*\d)",
        re.I,
    )
    # Pattern for alphanumeric identifiers (RCS, RC, CIF …)
    alpha_pat = re.compile(
        r"(?<![a-z])"
        r"(?:n[°o0]\.?\s*|num[eé]ro\s+)?"
        r"(rcs|rc|cif)"
        r"\s*(?:no\.?|n[°o0]\.?)?\s*[:.]?\s*"
        r"([A-Za-z0-9][A-Za-z0-9./\-]*(?:\s+(?!du\b|le\b|et\b)[A-Za-z0-9][A-Za-z0-9./\-]*){0,2})",
        re.I,
    )
    found: dict[str, str] = {}
    for line in lines:
        for c in line.cells:
            f = fold(c.text)
            for pat in (numeric_pat, vat_alpha_pat, alpha_pat):
                for m in pat.finditer(f):
                    raw_key = m.group(1).strip().lower()
                    raw_key = re.sub(r"\s+", " ", raw_key)
                    val = c.text[m.start(2): m.end(2)].strip()
                    digits = re.sub(r"\D", "", val)

                    # Map verbose/composite labels to a short display key
                    if re.match(r"^(tax|fiscal|vat|gst)", raw_key):
                        # e.g. "tax id", "tax number", "vat number", "fiscal id"
                        display = raw_key.upper()
                        # Simplify well-known forms
                        if re.match(r"vat", raw_key):
                            display = "VAT"
                        elif re.match(r"gst", raw_key):
                            display = "GST"
                        elif re.match(r"tax", raw_key):
                            display = "TAX ID"
                        elif re.match(r"fiscal", raw_key):
                            display = "FISCAL ID"
                        if display in found:
                            continue
                        if len(digits) < 4 and len(val) < 4:
                            continue
                        if digits in exclude_digits:
                            continue
                        found[display] = f"{display}: {val}"
                        continue

                    # Standard single-word keys (nif, stat, rcs, etc.)
                    key = raw_key.split()[0]
                    display = wanted.get(key, key.upper())
                    if display in found:
                        continue
                    min_digits = 4 if key in ("rcs", "rc", "cif") else 6
                    if len(digits) < min_digits and len(val) < 4:
                        continue
                    if digits in exclude_digits:
                        continue
                    found[display] = f"{display}: {val}"
    return " | ".join(found.values())


def detect_currency(text: str) -> tuple[str, bool]:
    """Return (currency as written, uncertain?)."""
    counts: Counter[str] = Counter()
    first_pos: dict[str, int] = {}

    def add(key: str, pos: int, n: int = 1) -> None:
        counts[key] += n
        first_pos.setdefault(key, pos)

    for m in re.finditer(r"(?<![A-Za-z])(USD|EUR|GBP|MGA|CAD|CHF|AUD|JPY|CNY|INR|ZAR|XOF|XAF|MUR|KES|NGN|MAD|AED|FCFA)(?![A-Za-z])", text):
        add(m.group(1), m.start())
    for sym in "€£¥":
        for m in re.finditer(re.escape(sym), text):
            add(sym, m.start())
    for m in re.finditer(r"\$", text):
        add("$", m.start())
    for m in re.finditer(r"(?:\d|^|\s)(Ar)(?=\s|$|[.,;:])", text):
        # "Ar" is only trusted next to numbers to avoid false positives
        if re.search(r"\d[\d\s.,]*\s*Ar\b|\bAr\s+\d", text[max(0, m.start() - 12) : m.end() + 6]):
            add("Ar", m.start())
    for m in re.finditer(r"(?i)\bariary\b", text):
        add("Ar", m.start())
    if not counts:
        return "", False
    # a "$" together with an ISO code (USD/CAD/AUD) -> keep the code
    if "$" in counts:
        for code in ("USD", "CAD", "AUD"):
            if code in counts:
                counts[code] += counts.pop("$")
                break
    best = sorted(counts, key=lambda k: (-counts[k], first_pos.get(k, 0)))[0]
    return best, best in {"$", "¥"}


# --------------------------------------------------------------------------------------
# Line items
# --------------------------------------------------------------------------------------
@dataclass
class Row:
    line_idx: int
    desc_words: list[Word] = field(default_factory=list)
    values: dict[str, Word] = field(default_factory=dict)  # kind -> token
    numeric: bool = False


def parse_row(line: Line, line_idx: int, cols: list[Col]) -> Row:
    row = Row(line_idx)
    real = sorted((c for c in cols if c.kind not in ("desc", "ignore", "tax_amount")), key=lambda c: c.x0)
    dcol = next((c for c in cols if c.kind == "desc"), None)
    ignore_left = [c for c in cols if c.kind == "ignore" and dcol is not None and c.x1 < dcol.x0]
    ignore_right = [c for c in cols if c.kind == "ignore" and (dcol is None or c.x0 > dcol.x1)]
    if not real:
        row.desc_words = list(line.words)
        return row
    first_x0 = real[0].x0
    desc: list[Word] = []
    cand: list[Word] = []

    for w in line.words:
        is_num = parse_number(w.text) is not None
        is_pct = parse_percent(w.text) is not None
        if is_num or is_pct:
            if any(c.x0 - 30 <= w.cx <= c.x1 + 30 for c in ignore_left):
                continue  # reference / item-code column: not needed on the standardized invoice
            if w.x1 < first_x0 - 40:
                desc.append(w)  # a number inside the description ("10 L", "3.2")
            else:
                cand.append(w)
            continue
        tl = _tok(w.text)
        if w.cx >= first_x0 - 4 and (tl in CURRENCY_TOKENS or tl in UOM_WORDS):
            continue
        if any(c.x0 - 8 <= w.cx <= c.x1 + 8 for c in ignore_right) and w.cx > (dcol.x1 if dcol else 0):
            continue  # unit column ("PCS", "Kg", "Unité")
        desc.append(w)

    cand.sort(key=lambda w: w.x0)
    assigned: dict[int, Word] = {}
    widths = [c.x1 - c.x0 for c in real] or [30.0]
    threshold = max(35.0, 2.5 * statistics.median(widths))

    def dist(c: Col, w: Word) -> float:
        return min(abs(w.x1 - c.x1), abs(w.x0 - c.x0), abs(w.cx - c.cx))

    best: dict[int, tuple[float, Word]] = {}
    for w in cand:
        is_pct = parse_percent(w.text) is not None
        opts = [(i, c) for i, c in enumerate(real) if (not is_pct or c.kind in ("tax", "discount"))]
        if not opts:
            desc.append(w)
            continue

        # Prefer a column that geometrically contains the token. This is
        # essential for optional columns: 6.50 in the Price column must not
        # become a discount just because the Discount column has no value.
        contained = [
            item for item in opts
            if item[1].x0 - 8 <= w.cx <= item[1].x1 + 8
        ]
        pick = min(contained or opts, key=lambda t: dist(t[1], w), default=None)
        if pick is None or dist(pick[1], w) > threshold:
            desc.append(w)
            continue
        i, c = pick
        d = dist(c, w)
        if i not in best or d < best[i][0]:
            if i in best:
                desc.append(best[i][1])
            best[i] = (d, w)
        else:
            desc.append(w)
    assigned = {i: w for i, (d, w) in best.items()}
    for i, w in assigned.items():
        row.values[real[i].kind] = w
    row.desc_words = sorted(desc, key=lambda w: w.x0)
    row.numeric = any(k in row.values for k in ("amount", "unit_price", "qty"))
    return row


def _row_to_item(rows: list[Row], flags: dict[str, str], idx: int) -> LineItem:
    # numbers come from the first numeric row; descriptions from all rows (wrapped text)
    item = LineItem()
    desc_parts: list[str] = []
    for r in rows:
        if r.desc_words:
            desc_parts.append(" ".join(w.text for w in r.desc_words))
    item.description = " ".join(desc_parts).strip()
    num_row = next((r for r in rows if r.numeric), None)
    if num_row:
        v = num_row.values
        if "qty" in v:
            item.quantity = parse_number(v["qty"].text) or ""
            if conf_flag(v["qty"].conf):
                flags[f"items.{idx}.quantity"] = "uncertain"
        if "unit_price" in v:
            item.unit_price = parse_number(v["unit_price"].text) or ""
            if conf_flag(v["unit_price"].conf):
                flags[f"items.{idx}.unit_price"] = "uncertain"
        if "amount" in v:
            item.amount = parse_number(v["amount"].text) or ""
            if conf_flag(v["amount"].conf):
                flags[f"items.{idx}.amount"] = "uncertain"
        if "discount" in v:
            pct = parse_percent(v["discount"].text)
            item.discount = f"{pct}%" if pct is not None else (parse_number(v["discount"].text) or "")
            dv = to_decimal(item.discount.rstrip("%"))
            if dv is not None and dv == 0:
                item.discount = ""  # "0.00%" carries no information
        if "tax" in v:
            pct = parse_percent(v["tax"].text)
            item.tax_rate = pct if pct is not None else (parse_number(v["tax"].text) or "")
    # Cross-check: qty * unit_price == amount (if all present)
    try:
        from decimal import Decimal

        q = to_decimal(item.quantity)
        up = to_decimal(item.unit_price)
        amt = to_decimal(item.amount)
        if q is not None and up is not None and amt is not None and q != 0:
            calc = q * up
            # if discount given, could affect; but don't silently overwrite
            if abs(calc - amt) > Decimal("0.02"):
                flags[f"items.{idx}.amount"] = flags.get(f"items.{idx}.amount") or "uncertain"
                notes_warn = True
    except Exception:
        pass
    if not item.description:
        flags[f"items.{idx}.description"] = "missing"
    if not item.amount:
        flags[f"items.{idx}.amount"] = "missing"
    return item


_SOFT_TOTALS = (RE_DISCOUNT, RE_CHARGE)


def is_totals_line(line: Line, anchor_x0: float | None = None) -> bool:
    """True when the line is part of the totals block rather than a line item.

    "Shipping" / "Transport" / "Discount" are legitimate item descriptions too. When we know
    where item descriptions start (`anchor_x0`), such a line that starts there is an item.
    """
    for cell in line.cells[:3]:
        f = fold_and_norm(cell.text)
        for r in TOTALS_ANY:
            if r.match(f):
                if r in _SOFT_TOTALS and anchor_x0 is not None and line.cells[0].x0 <= anchor_x0 + 12:
                    return False
                return True
    return False


def parse_items_with_header(lines: list[Line], hdr_idx: int, cols: list[Col], flags: dict[str, str]) -> tuple[list[LineItem], int]:
    rows: list[Row | None] = []
    end_idx = len(lines)
    anchor_x0 = None
    if hdr_idx + 1 < len(lines) and lines[hdr_idx + 1].page == lines[hdr_idx].page:
        anchor_x0 = lines[hdr_idx + 1].words[0].x0  # where item descriptions start
    for li in range(hdr_idx + 1, len(lines)):
        line = lines[li]
        f = line.folded
        if is_totals_line(line, anchor_x0):
            end_idx = li
            break
        if FOOTER_STOP.match(f):
            end_idx = li
            break
        if SKIP_LINE.match(f):
            continue
        if detect_header(line):  # repeated header on the next page
            continue
        if PAGE_NUM.search(f):  # running header/footer such as "Invoice INV-001 - page 2 of 3"
            continue
        rows.append(parse_row(line, li, cols))

    # group rows into items: numeric rows start items; text rows are descriptions
    items_rows: list[list[Row]] = []
    pending: list[Row] = []
    for pos, r in enumerate(rows):
        assert r is not None
        if not r.numeric and not any(re.search(r"[A-Za-zÀ-ÿ]", w.text) for w in r.desc_words):
            continue  # only codes/numbers (e.g. captions under pictures): not part of any item
        if r.numeric:
            group = pending + [r]
            pending = []
            items_rows.append(group)
        else:
            nxt = rows[pos + 1] if pos + 1 < len(rows) else None
            prev_gap_ok = bool(items_rows)
            # a text line right before a numeric row without its own description is its lead-in
            if nxt is not None and nxt.numeric and not nxt.desc_words:
                pending.append(r)
            elif prev_gap_ok:
                items_rows[-1].append(r)
            else:
                pending.append(r)
    if pending and items_rows:
        items_rows[-1].extend(pending)
    items: list[LineItem] = []
    for k, group in enumerate(items_rows):
        items.append(_row_to_item(group, flags, k))
    return items, end_idx


def parse_items_fallback(lines: list[Line], flags: dict[str, str]) -> tuple[list[LineItem], int]:
    """No table header recognised: use lines that end with numbers, before the totals block."""
    first_totals = next((i for i, l in enumerate(lines) if is_totals_line(l)), len(lines))
    items: list[LineItem] = []
    for li in range(first_totals):
        line = lines[li]
        f = line.folded
        if CONTACT_LINE.search(f) or SKIP_LINE.match(f) or is_section_label(f) or TAXID_RE.search(f):
            continue
        words = line.words
        trailing: list[Word] = []
        for w in reversed(words):
            if parse_number(w.text) is not None or parse_percent(w.text) is not None:
                trailing.insert(0, w)
            elif _tok(w.text) in CURRENCY_TOKENS:
                continue
            else:
                break
        if not trailing:
            continue
        head = words[: len(words) - len(trailing)]
        head_words = [w for w in head if _tok(w.text) not in CURRENCY_TOKENS]
        desc = " ".join(w.text for w in head_words).strip()
        if len(re.findall(r"[A-Za-zÀ-ÿ]", desc)) < 3:
            continue
        nums = [w for w in trailing if parse_percent(w.text) is None]
        pcts = [w for w in trailing if parse_percent(w.text) is not None]
        if not nums:
            continue
        item = LineItem(description=desc)
        vals = [parse_number(w.text) or "" for w in nums][-3:]
        if len(vals) == 1:
            item.amount = vals[0]
        elif len(vals) == 2:
            first = vals[0]
            if "." not in first:
                item.quantity = first
            else:
                item.unit_price = first
            item.amount = vals[1]
        else:
            item.quantity, item.unit_price, item.amount = vals
        if pcts:
            item.tax_rate = parse_percent(pcts[-1].text) or ""
        k = len(items)
        for fld in ("description", "quantity", "unit_price", "amount"):
            if getattr(item, fld):
                flags[f"items.{k}.{fld}"] = "uncertain"
        items.append(item)
    return items, first_totals


# --------------------------------------------------------------------------------------
# Totals
# --------------------------------------------------------------------------------------
@dataclass
class TotalHit:
    kind: str
    line_idx: int
    label: str
    value: str
    rate: str
    conf: float | None


def _label_and_numbers(line: Line, ci: int) -> tuple[str, list[Word]]:
    words: list[Word] = []
    for c in line.cells[ci:]:
        words.extend(c.words)
    label_words: list[str] = []
    nums: list[Word] = []
    started = False
    for w in words:
        if parse_number(w.text) is not None or parse_percent(w.text) is not None:
            started = True
            nums.append(w)
        elif not started:
            label_words.append(w.text)
        else:
            if _tok(w.text) in CURRENCY_TOKENS:
                continue
            # text after numbers (e.g. "(20%)" fragments) is ignored
    return " ".join(label_words), nums


_TOTAL_ORDER = [
    ("subtotal", RE_SUBTOTAL), ("discount", RE_DISCOUNT), ("charge", RE_CHARGE), ("balance", RE_BALANCE),
    ("paid", RE_PAID), ("tax_rate", RE_TAX_RATE), ("tax", RE_TAX), ("total_strong", RE_TOTAL_STRONG),
    ("total", RE_TOTAL_PLAIN),
]


def classify_total(folded: str) -> str | None:
    f = folded.strip()
    kind = next((k for k, r in _TOTAL_ORDER if r.match(f)), None)
    if kind == "charge" and NOT_A_CHARGE.search(f):
        return None
    return kind


def scan_totals_box(lines: list[Line], start: int) -> list[TotalHit]:
    """Totals laid out as a table: labels on one line ("TOTAL HT | TVA | Montant TVA | TOTAL TTC"),
    their values on the line right below."""
    hits: list[TotalHit] = []
    for li in range(start, len(lines) - 1):
        line, nxt = lines[li], lines[li + 1]
        if nxt.page != line.page or nxt.y0 - line.y1 > 3 * line.h:
            continue
        labelled = [(c, classify_total(fold_and_norm(c.text))) for c in line.cells]
        good = [(c, k) for c, k in labelled if k]
        if len(good) < 2 or len(good) < 0.6 * len(line.cells):
            continue
        rate = ""
        for c, kind in good:
            cells = [n for n in nxt.cells if min(n.x1, c.x1) - max(n.x0, c.x0) > 0.3 * min(n.x1 - n.x0, c.x1 - c.x0)]
            words = [w for n in cells for w in n.words]
            pct = [w for w in words if parse_percent(w.text) is not None]
            plain = [w for w in words if parse_number(w.text) is not None and parse_percent(w.text) is None]
            if kind == "tax" and pct and not plain:
                rate = parse_percent(pct[0].text) or ""
                continue
            if not plain:
                continue
            w = plain[-1]
            hits.append(TotalHit("tax" if kind == "tax" else kind, li, "TVA" if kind == "tax" else c.text,
                                 parse_number(w.text) or "", rate if kind == "tax" else "", w.conf))
        for h in hits:
            if h.kind == "tax" and not h.rate:
                h.rate = rate
    return hits


def scan_totals(lines: list[Line], start: int) -> list[TotalHit]:
    hits: list[TotalHit] = []
    order = _TOTAL_ORDER
    for li in range(start, len(lines)):
        line = lines[li]
        for ci, cell in enumerate(line.cells):
            f = fold_and_norm(cell.text)
            kind = classify_total(f)
            if kind is None:
                continue
            label, nums = _label_and_numbers(line, ci)
            plain = [w for w in nums if parse_percent(w.text) is None]
            pcts = [w for w in nums if parse_percent(w.text) is not None]
            if kind == "tax_rate":
                rate = parse_percent(pcts[0].text) if pcts else None
                if rate is None and plain:
                    candidate = to_decimal(parse_number(plain[0].text) or "")
                    if candidate is not None and Decimal(0) <= candidate <= Decimal(100):
                        rate = format(candidate, "f")
                if rate:
                    conf = (pcts[0] if pcts else plain[0]).conf
                    hits.append(TotalHit("tax_rate", li, label.strip(), "", rate, conf))
                break
            if not plain:
                continue
            val = parse_number(plain[-1].text) or ""
            rate = ""
            m = re.search(r"(\d+(?:[.,]\d+)?)\s*%", label)
            if m:
                rate = m.group(1).replace(",", ".")
            elif pcts:
                rate = parse_percent(pcts[0].text) or ""
            hits.append(TotalHit(kind, li, label.strip(), val, rate, plain[-1].conf))
            break  # one hit per line
    return hits


# --------------------------------------------------------------------------------------
# Main entry
# --------------------------------------------------------------------------------------
@dataclass
class ParseOutcome:
    data: InvoiceData
    flags: dict[str, str]
    notes: list[str]
    raw_text: str


def parse_document(doc_text: DocumentText) -> ParseOutcome:
    flags: dict[str, str] = {}
    notes: list[str] = list(doc_text.notes)
    lines: list[Line] = []
    page_heights: dict[int, float] = {}
    for p in doc_text.pages:
        page_heights[p.index] = p.height
        lines.extend(build_lines(p.words, p.index))
    raw_text = "\n".join(
        (f"--- page {l.page + 1} ---\n" if i == 0 or lines[i - 1].page != l.page else "") + l.text
        for i, l in enumerate(lines)
    )
    full_text = "\n".join(l.text for l in lines)
    data = InvoiceData()

    # ---- table header -------------------------------------------------------------
    hdr_idx = None
    cols: list[Col] = []
    for li, line in enumerate(lines):
        c = detect_header(line)
        if c:
            hdr_idx, cols = li, c
            break
    body_limit = hdr_idx if hdr_idx is not None else len(lines)

    # ---- invoice identification -----------------------------------------------------
    # Priority order:
    # 1. Primary label patterns (highest confidence)
    # 2. French-specific label patterns
    # 3. English reference / short-prefix patterns
    # 4. Generic N°/number label (limited to header area to avoid PO confusion)
    # 5. Fallback: regex scan of the header area
    r = (
        find_value(lines, INV_NO_LABEL,    _inv_no, neighbor_extract=_inv_no_strict)
        or find_value(lines, INV_NO_LABEL_FR,  _inv_no, neighbor_extract=_inv_no_strict)
        or find_value(lines, INV_REF_LABEL,    _inv_no, neighbor_extract=_inv_no_strict)
        or find_value(lines, INV_NO_LABEL_ALT, _inv_no)
        or find_value(lines, NUM_LABEL, _inv_no)
        or find_value(lines, REF_LABEL, _inv_no, limit=body_limit, neighbor_extract=_inv_no_strict)
    )
    if r:
        data.invoice_number = r[0]
        if conf_flag(r[1]):
            flags["invoice_number"] = "uncertain"
    else:
        # Fallback — search only the header area (page 1 before the item table).
        # Use two patterns:
        #   A) a well-known short prefix followed by digits (FT354992, INV-001, FAC-2026-00125…)
        #   B) a structured alphanumeric reference (ABC/2026/045, 2026/00125, 24-000158…)
        # Pattern A: 1-6 uppercase/lowercase letters, optional separator, then digits
        _RE_FALLBACK_A = re.compile(
            r"(?<![A-Za-z0-9])"
            r"([A-Za-z]{1,6}[-/_ ]?\d{2,}[A-Za-z0-9\-/_]*)"
            r"(?![A-Za-z0-9])"
        )
        # Pattern B: structured reference with at least one separator and no leading keyword
        _RE_FALLBACK_B = re.compile(
            r"(?<![A-Za-z0-9])"
            r"(\d{2,4}[-/]\d{2,6}(?:[-/][A-Za-z0-9]+)?)"
            r"(?![A-Za-z0-9])"
        )
        head_lines = [l for l in lines[: max(body_limit, 1)] if l.page == 0]
        head = "\n".join(l.text for l in head_lines)
        head_f = fold(head)

        def _first_non_date(pattern: re.Pattern, text: str, original: str) -> str | None:
            """Return the first match that is not a bare date and not a known exclusion."""
            for m in pattern.finditer(text):
                tok = original[m.start(1): m.end(1)].replace(" ", "")
                if DATE_RE.fullmatch(fold(tok)):
                    continue
                fk = fold_and_norm(tok)
                if fk in _NON_CODE_WORDS or is_section_label(fk):
                    continue
                return tok
            return None

        found = (
            _first_non_date(_RE_FALLBACK_A, head_f, head)
            or _first_non_date(_RE_FALLBACK_B, head_f, head)
        )
        if found:
            data.invoice_number = found
            flags["invoice_number"] = "uncertain"
        else:
            flags["invoice_number"] = "missing"

    r = find_value(lines, ISSUE_LABEL, _first_date)
    if r:
        data.issue_date = r[0]
        if conf_flag(r[1]):
            flags["issue_date"] = "uncertain"
    else:
        # dates in the top part of page 1, ignoring due dates and registration lines ("RC ... du 31/12/02")
        cands: list[tuple[str, bool]] = []
        for line in lines[: max(body_limit, 1)]:
            if line.page != 0:
                break
            f = line.folded
            if DUE_LABEL.match(f) or re.search(r"\b(?:stat|rc|rcs|cif|cf|nif|siret|n\.?i\.?f)\b", f):
                continue
            d = _first_date(line.text)
            if d:
                cands.append((d, bool(re.search(r"\ble\s+\d", f))))
        if cands:
            cands.sort(key=lambda t: not t[1])  # "Tamatave, le 5 août 2026" first; stable otherwise
            data.issue_date = cands[0][0]
            if len({c[0] for c in cands}) > 1:
                flags["issue_date"] = "uncertain"
        else:
            flags["issue_date"] = "missing"

    r = find_value(lines, DUE_LABEL, _first_date)
    if r:
        data.due_date = r[0]
        if conf_flag(r[1]):
            flags["due_date"] = "uncertain"

    r = find_value(lines, PO_LABEL, _ref_token)
    if r:
        data.po_number = r[0]
        if conf_flag(r[1]):
            flags["po_number"] = "uncertain"

    r = find_terms(lines)
    if r:
        data.payment_terms = r[0]
        if conf_flag(r[1]):
            flags["payment_terms"] = "uncertain"

    # ---- parties --------------------------------------------------------------------
    customer_hit = supplier_hit = None
    for li in range(min(body_limit, len(lines))):
        for ci, cell in enumerate(lines[li].cells):
            f = fold_and_norm(cell.text)
            if customer_hit is None:
                m = CUSTOMER_LABEL.match(f)
                if m:
                    customer_hit = (li, ci, cell.text[m.end():])
                    continue
            if supplier_hit is None:
                m = SUPPLIER_LABEL.match(f)
                if m:
                    supplier_hit = (li, ci, cell.text[m.end():])

    def party_from(hit) -> Party:
        li, ci, rest = hit
        left_edge = None
        if not rest.strip() and ci + 1 < len(lines[li].cells):
            nc = lines[li].cells[ci + 1]
            nf = fold_and_norm(nc.text)
            if (nc.x0 - lines[li].cells[ci].x1 < 300 and not is_section_label(nf)
                    and not re.fullmatch(r"(?:reference|ref|date|adresse|address|client|customer|"
                                         r"fournisseur|supplier|description|designation|quantity|"
                                         r"quantite|price|prix|amount|montant|total|contact)", nf)
                    and not DATE_RE.search(nf) and not TAXID_RE.search(nf)
                    and re.search(r"[a-z]{2}", nf)):
                # "Dest.   HUILERIE ..." or a two-column "Raison sociale | ACME" field
                rest, left_edge = nc.text, nc.x0
        block = block_below(lines, li, ci, first_text=rest, hard_end=body_limit, left_edge=left_edge)
        return prefer_full_own_name(clean_party(block))

    if not customer_hit:
        # no label: the block carrying one of YOUR company names (own_company.txt) is the customer
        for li in range(min(body_limit, len(lines))):
            for ci, cell in enumerate(lines[li].cells):
                if matches_own_company(cell.text):
                    customer_hit = (li, ci, cell.text)
                    break
            if customer_hit:
                break

    if customer_hit:
        data.customer = party_from(customer_hit)
        if conf_flag(lines[customer_hit[0]].cells[customer_hit[1]].conf):
            flags["customer.name"] = "uncertain"
    if customer_hit and data.customer.name and not data.customer.address:
        li0 = customer_hit[0]
        # the label may sit in the header row ("CLIENT | ADRESSE ...") or the value row may carry the name
        for hdr_i, val_i in ((li0, li0 + 1), (li0 - 1, li0)):
            if hdr_i < 0 or val_i >= len(lines) or lines[val_i].page != lines[hdr_i].page:
                continue
            done = False
            for hc in lines[hdr_i].cells:
                if not re.search(r"adress|coordonn|address", fold(hc.text)):
                    continue
                vcells = lines[val_i].cells
                nc = next((c for c in vcells if abs(c.x0 - hc.x0) <= 40), None)
                if nc is not None and not matches_own_company(nc.text):
                    blk = block_below(lines, val_i, vcells.index(nc), first_text=nc.text,
                                      hard_end=body_limit, max_lines=3)
                    data.customer.address = ", ".join(blk)
                    done = True
                    break
            if done:
                break
    if supplier_hit:
        data.supplier = party_from(supplier_hit)
    # Fallback 1: the most prominent text at the top of page 1 before any label
    if not data.supplier.name:
        label_idx = [
            li for li in range(min(body_limit, len(lines)))
            if lines[li].page == 0 and any(
                INV_NO_LABEL.match(fold_and_norm(c.text)) and not TITLE_ONLY.match(fold(c.text).strip())
                or CUSTOMER_LABEL.match(fold_and_norm(c.text))
                or ISSUE_LABEL.match(fold_and_norm(c.text)) or DUE_LABEL.match(fold_and_norm(c.text))
                for c in lines[li].cells
            )
        ]
        top_limit = min(label_idx) if label_idx else min(body_limit, len(lines))
        candidates: list[tuple[float, float, int, int]] = []
        for li in range(min(top_limit, len(lines), 16)):
            if lines[li].page != 0:
                break
            for ci, c in enumerate(lines[li].cells):
                ft = fold(c.text).strip()
                ftl = fold_and_norm(c.text).strip()
                if (
                    TITLE_ONLY.match(ft) or is_section_label(ftl) or CONTACT_LINE.search(ftl)
                    or len(re.sub(r"[^a-z]", "", ft)) < 3
                    or not re.search(r"[A-Za-z]{3,}", c.text)
                    or not re.search(r"[a-z]{2}", ft) or re.fullmatch(r"[\d\W]+", ft) or DATE_RE.search(ft)
                    or ft.endswith(":") or TAXID_RE.search(ft) or matches_own_company(c.text)
                    or (re.search(r"\d", ft) and not LEGAL_ENTITY.search(ft))
                    or re.match(r"^(?:agence|annexe|siege|adresse|lot|rue|boulevard|bd|av|avenue|immeuble|zone|route|quartier|servi|dest)\b", ft)
                    or re.match(r"^(?:observations?|objet|reference|page|date|bp|rc|rcs|cif|validite|arret\w*)\b", ft)
                ):
                    continue
                candidates.append((c.h, lines[li].y0, li, ci))
        if candidates:
            hmax = max(h for h, *_ in candidates)
            top = [t for t in candidates if t[0] >= 0.8 * hmax]
            _, _, li, ci = min(top, key=lambda t: t[1])  # the topmost of the most prominent texts
            name_text = lines[li].cells[ci].text
            cells = lines[li].cells
            k = ci + 1  # a logo's words are spaced widely: glue the cells that follow on the same line
            while k < len(cells) and cells[k].x0 - cells[k - 1].x1 < 3 * cells[ci].h:
                nxt = fold_and_norm(cells[k].text).strip()
                if CONTACT_LINE.search(nxt) or is_section_label(nxt) or re.search(r"\d", nxt) or TAXID_RE.search(nxt):
                    break
                name_text += " " + cells[k].text
                k += 1
            block = block_below(lines, li, ci, first_text=name_text, hard_end=body_limit)
            if len(block) > 1 and lines[li].cells[ci].h > 1.25 * statistics.median(l.h for l in lines[:40]):
                nl = lines[li + 1] if li + 1 < len(lines) else None
                if (nl is not None and nl.cells and abs(nl.cells[0].x0 - lines[li].cells[ci].x0) < 6
                        and nl.cells[0].h >= 0.85 * lines[li].cells[ci].h
                        and not re.search(r"\d|@", nl.cells[0].text)):
                    block = [block[0] + " " + block[1]] + block[2:]  # "MADA" / "FOURNITURES"
            data.supplier = clean_party(block)
            segs = [x for x in data.supplier.address.split(", ") if x]
            if len(segs) >= 1 and re.search(r"[•—|]", segs[0]) and not re.search(r"\d", segs[0]):
                data.supplier.address = ", ".join(segs[1:])      # tagline ("MATÉRIEL ÉLECTRIQUE • DEVIS")
            flags["supplier.name"] = "uncertain"
    # Fallback 2: a line carrying a legal-entity suffix (SARL, Ltd, ...)
    if not data.supplier.name:
        cust_name = fold(data.customer.name)
        for li, line in enumerate(lines):
            for ci, c in enumerate(line.cells):
                ft = fold(c.text)
                if LEGAL_ENTITY.search(ft) and not (cust_name and cust_name in ft):
                    block = block_below(lines, li, ci, first_text=c.text, hard_end=len(lines), max_lines=3)
                    data.supplier = clean_party(block)
                    flags["supplier.name"] = "uncertain"
                    break
            if data.supplier.name:
                break
    if not data.supplier.name:
        flags["supplier.name"] = "missing"
    if data.supplier.name and not data.supplier.tax_id:
        own = {re.sub(r"\D", "", v) for v in re.findall(r"[\d][\d ]{5,}", data.customer.tax_id)}
        found = other_tax_ids(lines, own)
        if found:
            data.supplier.tax_id = found
            flags["supplier.tax_id"] = "uncertain"

    # ---- bank details ---------------------------------------------------------------
    r = find_value(lines, IBAN_LABEL, _iban_value)
    if r:
        data.supplier.iban = r[0]
        if conf_flag(r[1]):
            flags["supplier.iban"] = "uncertain"
    r = find_value(lines, SWIFT_LABEL, _swift_value)
    if r:
        data.supplier.swift = r[0]
        if conf_flag(r[1]):
            flags["supplier.swift"] = "uncertain"
    r = find_value(lines, BANK_NAME_LABEL, _plain_text)
    if r:
        cand = r[0]
        cust_fold = fold(data.customer.name)
        cand_fold = fold(cand)
        is_bank_like = bool(re.search(r"\b(?:bnp|paribas|credit|societe|generale|sg|hsbc|barclays|boa|bmo|bnI|banque|bank)\b", cand_fold))
        if cust_fold and cand_fold == cust_fold and not is_bank_like:
            # skip if it's just the customer
            pass
        else:
            if not (cust_fold and cand_fold == cust_fold) or is_bank_like:
                data.supplier.bank_name = cand
                if conf_flag(r[1]):
                    flags["supplier.bank_name"] = "uncertain"
    r = find_value(lines, BANK_ACCOUNT_LABEL, _account_text)
    if r:
        data.supplier.bank_account = r[0]
        if conf_flag(r[1]):
            flags["supplier.bank_account"] = "uncertain"
    details = find_bank_details(lines)
    if details:
        if not data.supplier.bank_account:
            data.supplier.bank_account = " | ".join(f"{label}: {value}" for label, value in details)
            flags["supplier.bank_account"] = "uncertain"
        if not data.supplier.bank_name and details[0][0] not in {"IBAN", "RIB", "SWIFT", "BIC", "ACCOUNT", "COMPTE"}:
            cand_b = details[0][0]
            if not (fold(data.customer.name) and fold(cand_b) == fold(data.customer.name)):
                data.supplier.bank_name = cand_b
                flags["supplier.bank_name"] = "uncertain"

    # ---- line items -----------------------------------------------------------------
    items: list[LineItem] = []
    items_end = 0
    if hdr_idx is not None:
        items, items_end = parse_items_with_header(lines, hdr_idx, cols, flags)
    if not items:
        flags_fb: dict[str, str] = {}
        fb_items, fb_end = parse_items_fallback(lines, flags_fb)
        if fb_items:
            items, items_end = fb_items, fb_end
            for k, v in flags_fb.items():
                flags[k] = v  # type: ignore[assignment]
            notes.append(
                "No recognisable line-item table header was found; the lines below were guessed from rows that end "
                "with numbers. Please check each one."
            )
    data.items = items
    if not items:
        flags["items"] = "missing"

    # ---- totals ---------------------------------------------------------------------
    totals_start = items_end if items else 0
    hits = scan_totals(lines, totals_start) + scan_totals_box(lines, totals_start)
    if totals_start > 0:
        # Some layouts print a summary box above the item table: use it only for fields
        # that the main totals block did not provide.
        early = scan_totals(lines, 0) + scan_totals_box(lines, 0)
        present = {h.kind for h in hits}
        for h in early:
            if h.kind not in present:
                hits.append(h)
                present.add(h.kind)
    by_kind: dict[str, list[TotalHit]] = {}
    for h in hits:
        by_kind.setdefault(h.kind, []).append(h)

    if by_kind.get("subtotal"):
        h = by_kind["subtotal"][-1] if len(by_kind["subtotal"]) == 1 else by_kind["subtotal"][0]
        data.subtotal = h.value
        if conf_flag(h.conf):
            flags["subtotal"] = "uncertain"
    total_hit = None
    if by_kind.get("total_strong"):
        total_hit = by_kind["total_strong"][-1]
    elif by_kind.get("total"):
        total_hit = by_kind["total"][-1]
    if total_hit:
        data.total = total_hit.value
        if conf_flag(total_hit.conf):
            flags["total"] = "uncertain"
    if by_kind.get("balance"):
        h = by_kind["balance"][-1]
        data.balance_due = h.value
        if conf_flag(h.conf):
            flags["balance_due"] = "uncertain"
    if by_kind.get("paid"):
        h = by_kind["paid"][-1]
        data.amount_paid = h.value
        if conf_flag(h.conf):
            flags["amount_paid"] = "uncertain"
    if by_kind.get("discount"):
        vals = [to_decimal(h.value) for h in by_kind["discount"]]
        if all(v is not None for v in vals):
            tot = sum((abs(v) for v in vals if v is not None), Decimal(0))
            data.discount_total = format(tot, "f") if len(vals) > 1 else by_kind["discount"][0].value
    for h in by_kind.get("charge", []):
        label = re.sub(r"[:\s]+$", "", h.label) or "Additional charge"
        data.additional_charges.append(Charge(label=label, amount=h.value))

    tax_hits = by_kind.get("tax", [])
    agg = [h for h in tax_hits if re.match(r"^total\s+", fold(h.label))]
    per_rate = [h for h in tax_hits if h not in agg]
    chosen: list[TotalHit]
    if per_rate and agg:
        s = sum((to_decimal(h.value) or Decimal(0) for h in per_rate), Decimal(0))
        a = to_decimal(agg[-1].value)
        chosen = per_rate if a is not None and abs(s - a) <= Decimal("0.02") else agg[-1:]
    else:
        chosen = per_rate or agg[-1:]
    rate_hits = by_kind.get("tax_rate", [])
    distinct_rates = {h.rate for h in rate_hits if h.rate}
    if len(distinct_rates) == 1:
        only_rate = next(iter(distinct_rates))
        for h in chosen:
            if not h.rate:
                h.rate = only_rate
    elif len(distinct_rates) > 1:
        notes.append("Several tax rates were found; please check the rate on each tax line.")
    for h in chosen:
        label = re.sub(r"\(?\s*\d+(?:[.,]\d+)?\s*%\s*\)?", "", h.label)
        label = re.sub(r"[:\s]+$", "", label).strip() or "Tax"
        data.taxes.append(TaxLine(label=label, rate=h.rate, amount=h.value))
        if conf_flag(h.conf):
            flags[f"taxes.{len(data.taxes) - 1}.amount"] = "uncertain"

    # Only an "amount due" was printed: use it as the total when nothing was paid.
    if not data.total and data.balance_due and not data.amount_paid:
        data.total = data.balance_due
        flags["total"] = "uncertain"
        notes.append("No separate 'Total' line was found; the 'Amount due' value was used as the total. Please confirm.")
    if not data.total:
        flags["total"] = "missing"

    r = find_amount_words(lines)
    if r:
        data.amount_in_words = r[0]
        if conf_flag(r[1]):
            flags["amount_in_words"] = "uncertain"
    # If not found, try to generate from total if possible
    if not data.amount_in_words and data.total and data.currency:
        try:
            from .numbers import amount_to_words

            gen = amount_to_words(data.total, data.currency)
            if gen:
                data.amount_in_words = gen
                flags["amount_in_words"] = "uncertain"
                notes.append("Amount in words was auto-generated from the total and currency.")
        except Exception:
            pass

    # ---- currency -------------------------------------------------------------------
    cur, uncertain = detect_currency(full_text)
    data.currency = cur
    if not cur:
        flags["currency"] = "missing"
    elif uncertain:
        flags["currency"] = "uncertain"
        notes.append(f"The currency symbol '{cur}' is ambiguous. Please confirm the currency.")

    return ParseOutcome(data=data, flags=flags, notes=notes, raw_text=raw_text)
