"""Number, text-folding and formatting helpers.

All amounts travel as canonical decimal strings ("-1234.50"): a '.' decimal separator, no
grouping, original number of decimals preserved. Nothing here ever rounds a value.
"""
from __future__ import annotations

import re
import unicodedata
from decimal import Decimal, InvalidOperation

_CURRENCY_WORDS = re.compile(
    r"(?i)(?<![A-Za-z])(?:usd|eur|gbp|mga|ar|ariary|cad|chf|aud|jpy|cny|inr|zar|xof|xaf|fcfa|mur|kes|ngn|mad|aed)(?![A-Za-z])"
)
_CURRENCY_SYMBOLS = "$€£¥"


def fold(text: str) -> str:
    """Lower-case and strip accents while keeping the string length unchanged.

    Length preservation lets us find a match in the folded text and slice the *original*
    text at the same offsets (so values keep their original case and accents).
    """
    out = []
    for ch in text:
        d = unicodedata.normalize("NFKD", ch)
        base = d[0] if d else ch
        out.append(base.lower() if len(base.lower()) == 1 else base)
    return "".join(out).replace("’", "'").replace("`", "'")


def parse_number(text: str) -> str | None:
    """Parse a number written in any common format into a canonical decimal string.

    Handles 1,234.56 / 1.234,56 / 1 234,56 / 1'234.56 / (12.00) / -12 / 12- and strips
    currency symbols and codes. Returns None when the text is not a number.
    """
    if text is None:
        return None
    s = text.strip()
    if not s:
        return None
    neg = False
    if s.startswith("(") and s.endswith(")"):
        neg, s = True, s[1:-1].strip()
    s = _CURRENCY_WORDS.sub("", s)
    for sym in _CURRENCY_SYMBOLS:
        s = s.replace(sym, "")
    s = s.replace(" ", " ").replace(" ", " ").replace(" ", " ").strip()
    if s.endswith("-") or s.endswith("−"):
        neg, s = True, s[:-1].strip()
    if s[:1] in ("-", "−", "–"):
        neg, s = True, s[1:].strip()
    elif s[:1] == "+":
        s = s[1:].strip()
    if not re.fullmatch(r"[.,]?\d[\d .,']*", s):
        return None
    s = s.replace(" ", "").replace("'", "")
    if not re.search(r"\d", s) or s[-1] in ".,":
        # trailing separator: "12," is not a clean number
        s = s.rstrip(".,")
        if not s:
            return None
    has_dot, has_comma = "." in s, "," in s
    if has_dot and has_comma:
        dec_idx = max(s.rfind("."), s.rfind(","))
        int_part = re.sub(r"[.,]", "", s[:dec_idx])
        frac = s[dec_idx + 1 :]
        if not frac.isdigit():
            return None
        out = f"{int_part}.{frac}"
    elif has_dot or has_comma:
        sep = "." if has_dot else ","
        if s.count(sep) > 1:
            # "1.234.567" -> thousands separators
            groups = s.split(sep)
            if not all(len(g) == 3 for g in groups[1:]):
                return None
            out = "".join(groups)
        else:
            before, after = s.split(sep)
            if len(after) == 3 and before and before != "0" and len(before) <= 3:
                out = before + after  # "1,234" read as thousands
            else:
                out = f"{before or '0'}.{after}"
    else:
        out = s
    if not re.fullmatch(r"\d+(\.\d+)?", out):
        return None
    return ("-" if neg else "") + out


PERCENT_RE = re.compile(r"^[-+]?\d+(?:[.,]\d+)?\s*%$")


def parse_percent(text: str) -> str | None:
    """'20%' -> '20', '5,5 %' -> '5.5'. None if not a percentage."""
    t = text.strip()
    if not PERCENT_RE.match(t):
        return None
    return t.rstrip("%").strip().replace(",", ".").lstrip("+")


def to_decimal(value: str | None) -> Decimal | None:
    if value is None:
        return None
    v = value.strip()
    if not v:
        return None
    try:
        return Decimal(v)
    except InvalidOperation:
        parsed = parse_number(v)
        if parsed is None:
            return None
        try:
            return Decimal(parsed)
        except InvalidOperation:
            return None


def format_amount(value: str | None, *, min_decimals_if_fractional: bool = True) -> str:
    """Display an amount with thousands separators WITHOUT rounding.

    The number of decimals found in the source is preserved (a value with one decimal
    gets padded to two for readability, which does not change its value).
    """
    if value is None or not value.strip():
        return ""
    d = to_decimal(value)
    if d is None:
        return value.strip()  # not parseable: show the original untouched
    sign = "-" if d < 0 else ""
    d = abs(d)
    text = format(d, "f")
    if "." in text:
        int_part, frac = text.split(".")
    else:
        int_part, frac = text, ""
    if frac and min_decimals_if_fractional and len(frac) < 2:
        frac = frac.ljust(2, "0")
    grouped = f"{int(int_part):,}"
    return f"{sign}{grouped}" + (f".{frac}" if frac else "")


def format_percent(value: str | None) -> str:
    if not value or not value.strip():
        return ""
    v = value.strip().rstrip("%").strip()
    d = to_decimal(v)
    if d is None:
        return value.strip()
    text = format(d.normalize(), "f")
    return f"{text}%"


# --------------------------------------------------------------------------------------
# Number to words (basic support for common currencies/languages)
# --------------------------------------------------------------------------------------
_FR_UNITS = ["", "un", "deux", "trois", "quatre", "cinq", "six", "sept", "huit", "neuf"]
_FR_TEENS = ["dix", "onze", "douze", "treize", "quatorze", "quinze", "seize", "dix-sept", "dix-huit", "dix-neuf"]
_FR_TENS = ["", "dix", "vingt", "trente", "quarante", "cinquante", "soixante", "soixante-dix", "quatre-vingts", "quatre-vingt-dix"]


def _fr_less_than_thousand(n: int) -> str:
    if n < 0:
        return ""
    if n < 10:
        return _FR_UNITS[n]
    if n < 20:
        return _FR_TEENS[n - 10]
    if n < 100:
        tens = n // 10
        ones = n % 10
        if tens == 7:
            if ones == 1:
                return "soixante et onze"
            return "soixante-" + _FR_TEENS[ones]
        if tens == 9:
            if ones == 0:
                return "quatre-vingt-dix"
            return "quatre-vingt-" + _FR_TEENS[ones]
        if tens == 8:
            if ones == 0:
                return "quatre-vingts"
            return "quatre-vingt" + ("-" + _FR_UNITS[ones] if ones else "")
        res = _FR_TENS[tens]
        if ones == 0:
            return res
        if ones == 1 and tens != 8:
            return res + " et un"
        return res + "-" + _FR_UNITS[ones]
    hundreds = n // 100
    rest = n % 100
    if hundreds == 1:
        res = "cent"
    else:
        res = _FR_UNITS[hundreds] + " cents" if rest == 0 else _FR_UNITS[hundreds] + " cent"
    if rest:
        res += " " + _fr_less_than_thousand(rest)
    return res


def _number_to_words_fr(n: int) -> str:
    if n == 0:
        return "zéro"
    parts = []
    # million
    if n >= 1000000:
        m = n // 1000000
        r = n % 1000000
        if m == 1:
            parts.append("un million")
        else:
            parts.append(_fr_less_than_thousand(m) + " millions")
        n = r
    if n >= 1000:
        th = n // 1000
        r = n % 1000
        if th == 1:
            parts.append("mille")
        else:
            parts.append(_fr_less_than_thousand(th) + " mille")
        n = r
    if n > 0:
        parts.append(_fr_less_than_thousand(n))
    return " ".join(parts)


def amount_to_words(amount: str | None, currency: str = "", lang: str = "fr") -> str:
    """Convert numeric amount to words (best effort)."""
    d = to_decimal(amount)
    if d is None:
        return ""
    cur = (currency or "").upper()
    # Handle MGA / Ariary
    if cur in ("MGA", "AR") or "ARIARY" in cur:
        try:
            # MGA usually integer in Ariary, but handle decimals
            total = int(d) if d == int(d) else d
            # Simple form: use integer part
            num = abs(int(d))
            words = _number_to_words_fr(num)
            words = words.replace("quatre-vingts mille", "quatre-vingt mille")  # common form
            res = words[0].upper() + words[1:] if words else str(num)
            if num == 1:
                res = "Un"
            # Ariary form
            if cur == "MGA" or cur == "AR":
                return f"{res} Ariary" if not res.lower().startswith("un ") and res != "Un" else (f"Un Ariary" if num == 1 else f"{res} Ariary")
            return f"{res} Ariary"
        except Exception:
            pass
    if cur in ("EUR", "€"):
        try:
            num = abs(int(d))
            words = _number_to_words_fr(num)
            res = words[0].upper() + words[1:] if words else str(num)
            if num == 0:
                res = "Zéro"
            # euros
            sres = f"{res} euro{'s' if num != 1 else ''}"
            # cents if any
            cents = int(round((abs(d) - num) * 100))
            if cents:
                cwords = _number_to_words_fr(cents)
                cwords_res = cwords[0].upper() + cwords[1:] if cwords else str(cents)
                return f"{sres} et {cwords_res} centime{'s' if cents != 1 else ''}"
            return sres
        except Exception:
            pass
    if cur in ("USD", "$"):
        try:
            num = abs(int(d))
            # basic english style not required, fallback french-like
            words = _number_to_words_fr(num)
            res = words[0].upper() + words[1:] if words else str(num)
            cents = int(round((abs(d) - num) * 100))
            sres = f"{res} dollar{'s' if num != 1 else ''}"
            if cents:
                cwords = _number_to_words_fr(cents)
                cwords_res = cwords[0].upper() + cwords[1:] if cwords else str(cents)
                return f"{sres} and {cwords_res} cent{'s' if cents != 1 else ''}"
            return sres
        except Exception:
            pass
    # default french
    try:
        num = abs(int(d))
        words = _number_to_words_fr(num)
        res = words[0].upper() + words[1:] if words else str(num)
        return res
    except Exception:
        return ""

