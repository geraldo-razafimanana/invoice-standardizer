"""FastAPI application: upload -> extract -> review -> preview -> generate."""
from __future__ import annotations

import atexit
import re
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import extraction
from .checks import validate
from .extraction import MIN_TEXT_CHARS, PdfError, open_pdf
from .models import (
    DocumentInfo, ExtractionResult, FeedbackRequest, GenerateRequest, InvoiceData, PageInfo, ValidateRequest, Warning_,
)
from .parser import parse_document
from .pdfgen import build_pdf, standardized_filename
from .store import DocumentStore, FeedbackStore

MAX_UPLOAD_BYTES = 25 * 1024 * 1024

app = FastAPI(title="Invoice Standardizer", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Content-Disposition"],
)
store = DocumentStore()
feedback_store = FeedbackStore()
atexit.register(store.close)


@app.exception_handler(PdfError)
async def pdf_error_handler(_, exc: PdfError):
    return JSONResponse(status_code=422, content={"detail": exc.message, "code": exc.code})


def _inspect(doc_id: str, filename: str, data: bytes) -> DocumentInfo:
    """Cheap page-by-page detection of selectable text vs scanned pages (no OCR yet)."""
    pdf = open_pdf(data)
    try:
        pages: list[PageInfo] = []
        for i, page in enumerate(pdf):
            chars = sum(len(str(w[4])) for w in page.get_text("words"))
            pages.append(PageInfo(
                page=i, method="text" if chars >= MIN_TEXT_CHARS else "ocr",
                width=page.rect.width, height=page.rect.height,
            ))
    finally:
        pdf.close()
    methods = {p.method for p in pages}
    method = "text" if methods == {"text"} else "ocr" if methods == {"ocr"} else "mixed"
    return DocumentInfo(doc_id=doc_id, filename=filename, page_count=len(pages), method=method, pages=pages)


@app.get("/api/health")
def health():
    return {"status": "ok", "ocr": extraction.tesseract_status()}


@app.post("/api/documents", response_model=DocumentInfo)
def upload_document(file: UploadFile = File(...)):
    filename = Path(file.filename or "invoice.pdf").name
    chunks: list[bytes] = []
    size = 0
    while True:
        chunk = file.file.read(1024 * 1024)
        if not chunk:
            break
        size += len(chunk)
        if size > MAX_UPLOAD_BYTES:
            raise HTTPException(413, f"The file is larger than {MAX_UPLOAD_BYTES // (1024 * 1024)} MB.")
        chunks.append(chunk)
    data = b"".join(chunks)
    if not data:
        raise PdfError("empty_file", "The file is empty.")
    open_pdf(data).close()  # raises PdfError for not-a-pdf / corrupted / encrypted / too many pages
    doc = store.add(filename, data)
    return _inspect(doc.doc_id, filename, data)


def _get_doc(doc_id: str):
    doc = store.get(doc_id)
    if doc is None or not doc.path.exists():
        raise HTTPException(404, "Document not found or expired. Please upload the PDF again.")
    return doc


@app.get("/api/documents/{doc_id}/file")
def original_file(doc_id: str):
    doc = _get_doc(doc_id)
    return FileResponse(doc.path, media_type="application/pdf", filename=doc.filename)


@app.get("/api/documents/{doc_id}/pages/{page}.png")
def page_image(doc_id: str, page: int, zoom: float = Query(1.5, ge=0.5, le=4.0)):
    doc = _get_doc(doc_id)
    png = extraction.render_page_png(doc.path.read_bytes(), page, zoom)
    return Response(png, media_type="image/png", headers={"Cache-Control": "private, max-age=600"})


@app.post("/api/documents/{doc_id}/extract", response_model=ExtractionResult)
def extract_document(doc_id: str):
    doc = _get_doc(doc_id)
    data = doc.path.read_bytes()
    pdf = open_pdf(data)
    try:
        text = extraction.extract_words(pdf)
    finally:
        pdf.close()
    outcome = parse_document(text)
    info = DocumentInfo(
        doc_id=doc_id, filename=doc.filename, page_count=len(text.pages), method=text.method,
        pages=[PageInfo(page=p.index, method=p.method, ocr_confidence=p.ocr_confidence,
                        width=p.width, height=p.height) for p in text.pages],
    )
    return ExtractionResult(
        document=info, data=outcome.data, flags=outcome.flags, notes=outcome.notes,
        warnings=validate(outcome.data), raw_text=outcome.raw_text[:60000],
    )


@app.post("/api/validate", response_model=list[Warning_])
def validate_data(req: ValidateRequest):
    return validate(req.data)


def _render(req: GenerateRequest) -> tuple[bytes, str]:
    filename, pages = None, None
    if req.doc_id:
        doc = store.get(req.doc_id)
        if doc:
            filename = doc.filename
            try:
                pdf = open_pdf(doc.path.read_bytes())
                pages = pdf.page_count
                pdf.close()
            except PdfError:
                pass
    pdf_bytes = build_pdf(req.data, original_filename=filename, original_pages=pages)
    return pdf_bytes, standardized_filename(req.data, filename)


def _safe_header_name(name: str) -> str:
    return re.sub(r'[^A-Za-z0-9._-]', "_", name)


@app.post("/api/preview")
def preview(req: GenerateRequest):
    pdf_bytes, filename = _render(req)
    return Response(pdf_bytes, media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="{_safe_header_name(filename)}"'})


@app.post("/api/preview-session")
def create_preview_session(req: GenerateRequest):
    """Generate the standardized PDF, store it server-side, and return a session id.

    The frontend uses the returned id with GET /api/preview-session/{id} to display
    the PDF inside an <iframe>.  Unlike a blob: URL, a regular HTTP URL is reliably
    rendered inline by the browser's built-in PDF viewer instead of being downloaded.
    """
    pdf_bytes, filename = _render(req)
    doc = store.add(filename, pdf_bytes)
    return {"session_id": doc.doc_id}


@app.get("/api/preview-session/{session_id}")
def serve_preview_session(session_id: str):
    doc = _get_doc(session_id)
    return FileResponse(
        doc.path, media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{_safe_header_name(doc.filename)}"'},
    )


@app.get("/api/preview-session/{session_id}/info")
def preview_session_info(session_id: str):
    """Return page count and dimensions for the stored preview PDF."""
    doc = _get_doc(session_id)
    pdf = open_pdf(doc.path.read_bytes())
    try:
        pages = [
            {"page": i, "width": page.rect.width, "height": page.rect.height}
            for i, page in enumerate(pdf)
        ]
    finally:
        pdf.close()
    return {"page_count": len(pages), "pages": pages}


@app.get("/api/preview-session/{session_id}/pages/{page}.png")
def preview_session_page(session_id: str, page: int, zoom: float = Query(1.5, ge=0.5, le=4.0)):
    """Render a single page of the stored preview PDF as PNG."""
    doc = _get_doc(session_id)
    png = extraction.render_page_png(doc.path.read_bytes(), page, zoom)
    return Response(png, media_type="image/png", headers={"Cache-Control": "private, max-age=600"})


@app.post("/api/generate")
def generate(req: GenerateRequest):
    pdf_bytes, filename = _render(req)
    return Response(pdf_bytes, media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="{_safe_header_name(filename)}"'})


@app.post("/api/documents/{doc_id}/delete")
def delete_document_beacon(doc_id: str):
    """POST variant of DELETE, usable from navigator.sendBeacon when the tab closes."""
    return {"deleted": store.delete(doc_id)}


@app.delete("/api/documents/{doc_id}")
def delete_document(doc_id: str):
    return {"deleted": store.delete(doc_id)}


@app.post("/api/feedback")
def submit_feedback(req: FeedbackRequest):
    entry = req.model_dump()
    feedback_store.add(entry)
    return {"status": "ok"}


@app.get("/api/feedback")
def list_feedback():
    return feedback_store.all()


# --- Serve the built frontend (npm run build) when present -----------------------------
_DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"
if _DIST.is_dir():
    app.mount("/assets", StaticFiles(directory=_DIST / "assets"), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    def spa(full_path: str):
        candidate = (_DIST / full_path).resolve()
        if full_path and candidate.is_file() and _DIST in candidate.parents:
            return FileResponse(candidate)
        return FileResponse(_DIST / "index.html")
