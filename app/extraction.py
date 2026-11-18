"""PDF -> words. Chooses per page between the embedded text layer and OCR."""
from __future__ import annotations

import io
import logging
import os
import re
import shutil
from dataclasses import dataclass, field

import pymupdf

from .layout import Word
from .numbers import parse_number

log = logging.getLogger(__name__)

MAX_PAGES = 40
MIN_TEXT_CHARS = 25  # below this a page is treated as scanned
OCR_DPI = 300


class PdfError(Exception):
    """A user-facing problem with the uploaded file."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class PageText:
    index: int
    method: str  # "text" | "ocr"
    words: list[Word]
    width: float
    height: float
    ocr_confidence: float | None = None


@dataclass
class DocumentText:
    pages: list[PageText] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def method(self) -> str:
        methods = {p.method for p in self.pages}
        if methods == {"text"}:
            return "text"
        if methods == {"ocr"}:
            return "ocr"
        return "mixed"


def open_pdf(data: bytes) -> pymupdf.Document:
    if not data[:1024].lstrip(b"\x00\r\n\t ").startswith(b"%PDF"):
        raise PdfError("not_a_pdf", "This file is not a PDF document.")
    try:
        doc = pymupdf.open(stream=data, filetype="pdf")
    except Exception as exc:  # corrupted / truncated
        raise PdfError("corrupted", f"The PDF is corrupted or unreadable ({exc}).") from exc
    if doc.needs_pass:
        doc.close()
        raise PdfError("encrypted", "The PDF is password-protected. Remove the password and try again.")
    if doc.page_count == 0:
        doc.close()
        raise PdfError("empty", "The PDF contains no pages.")
    if doc.page_count > MAX_PAGES:
        n = doc.page_count
        doc.close()
        raise PdfError("too_many_pages", f"The PDF has {n} pages; the limit is {MAX_PAGES}.")
    return doc


_WINDOWS_PATHS = (
    r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Programs\Tesseract-OCR\tesseract.exe"),
)


def find_tesseract() -> str | None:
    """Locate the tesseract binary: TESSERACT_CMD env var, PATH, then usual install folders."""
    candidates = [os.environ.get("TESSERACT_CMD"), shutil.which("tesseract"), *_WINDOWS_PATHS,
                  "/opt/homebrew/bin/tesseract", "/usr/local/bin/tesseract"]
    for c in candidates:
        if c and os.path.isfile(c):
            return c
    return None


def tesseract_status() -> dict:
    """Diagnostic: where tesseract was found, which languages, and why it is unusable if it is."""
    try:
        import pytesseract
    except Exception as exc:
        return {"ok": False, "path": None, "languages": [],
                "problem": f"The Python package 'pytesseract' is missing ({exc}). "
                           "Run: py -m pip install pytesseract pillow"}
    cmd = find_tesseract()
    if cmd is None:
        return {"ok": False, "path": None, "languages": [],
                "problem": "tesseract.exe was not found (not in PATH nor in the usual install folders). "
                           "Set TESSERACT_CMD to its full path."}
    pytesseract.pytesseract.tesseract_cmd = cmd
    try:
        langs = [l for l in pytesseract.get_languages(config="") if l != "osd"]
    except Exception as exc:
        return {"ok": False, "path": cmd, "languages": [],
                "problem": f"Tesseract was found at {cmd} but could not be run ({exc})."}
    if not langs:
        return {"ok": False, "path": cmd, "languages": [], "problem": f"Tesseract at {cmd} has no language data."}
    return {"ok": True, "path": cmd, "languages": langs, "problem": ""}


def tesseract_languages() -> list[str]:
    return tesseract_status()["languages"]


def _ocr_lang() -> str | None:
    langs = tesseract_languages()
    if not langs:
        return None
    wanted = [l for l in ("eng", "fra") if l in langs]
    return "+".join(wanted) if wanted else langs[0]


def _erase_runs(mask, min_run: int):
    import numpy as np

    out = np.zeros_like(mask)
    for i, row in enumerate(mask):
        if not row.any():
            continue
        d = np.diff(np.concatenate(([0], row.astype(np.int8), [0])))
        for s_, e_ in zip(np.where(d == 1)[0], np.where(d == -1)[0]):
            if e_ - s_ >= min_run:
                out[i, s_:e_] = True
    return out


def _preprocess(img):
    """Grey + contrast stretch, then erase long table rules that confuse Tesseract (cells lose numbers)."""
    from PIL import Image, ImageOps

    gray = ImageOps.autocontrast(img.convert("L"), cutoff=2)
    try:
        import numpy as np

        a = np.asarray(gray).copy()
        dark = a < 150
        run = max(60, int(a.shape[1] * 0.045))  # ~ 1 cm at 300 dpi
        rules = _erase_runs(dark, run) | _erase_runs(dark.T, run).T
        if rules.any():
            pad = np.pad(rules, 1)
            grown = np.zeros_like(rules)
            for dy in range(3):
                for dx in range(3):
                    grown |= pad[dy : dy + rules.shape[0], dx : dx + rules.shape[1]]
            a[grown] = 255
        return Image.fromarray(a)
    except Exception as exc:  # numpy missing: plain greyscale still works
        log.warning("table-rule removal skipped: %s", exc)
        return gray


def _clean_token(txt: str) -> str:
    txt = txt.strip(" |[]{}!_~\u2018\u2019`").lstrip("'\"")
    return re.sub(r"(?<=\d)[,;:]+$", "", txt)


def _box_overlap(a: Word, b: Word) -> float:
    x0, y0 = max(a.x0, b.x0), max(a.y0, b.y0)
    x1, y1 = min(a.x1, b.x1), min(a.y1, b.y1)
    if x1 <= x0 or y1 <= y0:
        return 0.0
    inter = (x1 - x0) * (y1 - y0)
    return inter / max(1.0, min((a.x1 - a.x0) * (a.y1 - a.y0), (b.x1 - b.x0) * (b.y1 - b.y0)))


def _better_identifier(ocr_text: str, text_text: str) -> bool:
    """True when the embedded layer has a letter where OCR confidently produced a digit."""
    import difflib

    a, b = ocr_text.strip(), text_text.strip()
    if len(a) < 4 or len(a) != len(b):
        return False
    if not any(ch.isdigit() for ch in a) or not any(ch.isalpha() for ch in b):
        return False
    if sum(ch.isalpha() for ch in b) <= sum(ch.isalpha() for ch in a):
        return False
    return difflib.SequenceMatcher(None, a.casefold(), b.casefold()).ratio() >= 0.7


def _restore_text_tokens(ocr_words: list[Word], text_words: list[Word]) -> list[Word]:
    """Use overlapping embedded text for low-confidence OCR tokens (e.g. T0024965 read as 10024965)."""
    used: set[int] = set()
    for word in ocr_words:
        if word.conf is not None and word.conf >= 55:
            continue
        best_i, best_overlap = None, 0.0
        for i, candidate in enumerate(text_words):
            if i in used:
                continue
            overlap = _box_overlap(word, candidate)
            if overlap > best_overlap:
                best_i, best_overlap = i, overlap
        if best_i is None or best_overlap < 0.5:
            continue
        candidate = text_words[best_i]
        if _better_identifier(word.text, candidate.text):
            word.text = candidate.text
            word.conf = max(word.conf or 0.0, 80.0)
            used.add(best_i)
    return ocr_words


def _ocr_page(page: pymupdf.Page, index: int) -> tuple[list[Word], float | None]:
    import pytesseract
    from PIL import Image

    lang = _ocr_lang()
    if lang is None:
        status = tesseract_status()
        raise PdfError(
            "ocr_unavailable",
            "This looks like a scanned PDF but OCR cannot run: " + status["problem"] +
            " (Windows installer: https://github.com/UB-Mannheim/tesseract/wiki). Restart the app after fixing it.",
        )
    zoom = OCR_DPI / 72
    pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
    img = _preprocess(Image.open(io.BytesIO(pix.tobytes("png"))))
    data = pytesseract.image_to_data(
        img, lang=lang, config="--psm 4", output_type=pytesseract.Output.DICT
    )
    words: list[Word] = []
    confs: list[float] = []
    for i, txt in enumerate(data["text"]):
        txt = _clean_token(txt or "")
        if not txt or not re.search(r"[A-Za-z0-9À-ÿ]", txt):
            continue
        conf = float(data["conf"][i])
        if conf < 0:
            continue
        x, y, w, h = data["left"][i], data["top"][i], data["width"][i], data["height"][i]
        words.append(Word(x / zoom, y / zoom, (x + w) / zoom, (y + h) / zoom, txt, index, conf))
        confs.append(conf)
    return words, (sum(confs) / len(confs) if confs else None)


_WORD_OK = re.compile(r"^[A-Za-zÀ-ÿ'’\-]{3,}$")
_VOWEL = re.compile(r"[aeiouyàâäéèêëîïôöùûüAEIOUY]")


def _quality(words: list[Word]) -> int:
    """Number of well-formed tokens (real-looking words and numbers): higher = more trustworthy text."""
    n = 0
    for w in words:
        t = w.text.strip(".,;:()")
        if _WORD_OK.match(t) and _VOWEL.search(t) and not re.search(r"[a-zà-ÿ][A-ZÀ-Ý]", t):
            n += 1
        elif parse_number(t) is not None:
            n += 1
    return n


def _fullpage_image(page: pymupdf.Page) -> bool:
    area = page.rect.width * page.rect.height
    for info in page.get_image_info():
        x0, y0, x1, y1 = info["bbox"]
        if (x1 - x0) * (y1 - y0) >= 0.7 * area:
            return True
    return False


def _ocr_logos(page: pymupdf.Page, index: int) -> list[Word]:
    """OCR the pictures in the top part of a text page: the supplier name is often only in the logo."""
    lang = _ocr_lang()
    if lang is None:
        return []
    import pytesseract
    from PIL import Image, ImageOps

    out: list[Word] = []
    H, W = page.rect.height, page.rect.width
    zoom = 4.0
    for info in page.get_image_info():
        x0, y0, x1, y1 = info["bbox"]
        w, h = x1 - x0, y1 - y0
        if w < 80 or h < 20 or y0 > 0.30 * H or w * h > 0.5 * W * H:
            continue
        try:
            pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), clip=pymupdf.Rect(x0, y0, x1, y1), alpha=False)
            img = Image.open(io.BytesIO(pix.tobytes("png"))).convert("L")
            if sum(img.resize((16, 16)).getdata()) / 256 < 110:  # dark background: invert
                img = ImageOps.invert(img)
            img = ImageOps.autocontrast(img, cutoff=1)  # faded / low-contrast logos
            data = pytesseract.image_to_data(img, lang=lang, config="--psm 6", output_type=pytesseract.Output.DICT)
        except Exception as exc:  # a failing logo must never break the extraction
            log.warning("logo OCR failed: %s", exc)
            continue
        for i, txt in enumerate(data["text"]):
            txt = (txt or "").strip()
            conf = float(data["conf"][i])
            if conf < 55 or not re.search(r"[A-Za-zÀ-ÿ]{2}", txt):
                continue
            x, y, ww, hh = data["left"][i], data["top"][i], data["width"][i], data["height"][i]
            out.append(Word(x0 + x / zoom, y0 + y / zoom, x0 + (x + ww) / zoom, y0 + (y + hh) / zoom, txt, index, conf))
    return out


def extract_words(doc: pymupdf.Document) -> DocumentText:
    result = DocumentText()
    ocr_ready = _ocr_lang() is not None
    for i, page in enumerate(doc):
        rect = page.rect
        raw = page.get_text("words")  # x0, y0, x1, y1, text, block, line, wordno
        words = [Word(w[0], w[1], w[2], w[3], w[4], i) for w in raw if str(w[4]).strip()]
        n_chars = sum(len(w.text) for w in words)
        if n_chars >= MIN_TEXT_CHARS:
            # A scanner's hidden text layer under a full-page picture is unreliable: compare with real OCR.
            if ocr_ready and _fullpage_image(page):
                ocr_words, conf = _ocr_page(page, i)
                if _quality(ocr_words) >= 0.7 * _quality(words):
                    ocr_words = _restore_text_tokens(ocr_words, words)
                    result.pages.append(PageText(i, "ocr", ocr_words, rect.width, rect.height, conf))
                    if conf is not None and conf < 70:
                        result.notes.append(f"Page {i + 1}: OCR confidence is low ({conf:.0f}%). Please check every value carefully.")
                    continue
            words = words + _ocr_logos(page, i)
            result.pages.append(PageText(i, "text", words, rect.width, rect.height))
            continue
        # Scanned (or nearly empty) page -> OCR
        ocr_words, conf = _ocr_page(page, i)
        ocr_words = _restore_text_tokens(ocr_words, words)
        result.pages.append(PageText(i, "ocr", ocr_words, rect.width, rect.height, conf))
        if conf is not None and conf < 70:
            result.notes.append(
                f"Page {i + 1}: OCR confidence is low ({conf:.0f}%). Please check every value carefully."
            )
        if not ocr_words:
            result.notes.append(f"Page {i + 1}: no text could be recognised.")
    if all(not p.words for p in result.pages):
        raise PdfError(
            "no_text", "No readable text was found in this PDF (blank or unreadable scan)."
        )
    return result


def render_page_png(data: bytes, page_index: int, zoom: float = 1.5) -> bytes:
    doc = open_pdf(data)
    try:
        if not 0 <= page_index < doc.page_count:
            raise PdfError("bad_page", "Page out of range.")
        pix = doc[page_index].get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
        return pix.tobytes("png")
    finally:
        doc.close()
