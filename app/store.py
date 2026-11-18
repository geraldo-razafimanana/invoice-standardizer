"""Session document store.

Retention policy: an uploaded PDF is kept in a private temp directory only for the working
session. It is deleted when the client calls DELETE /api/documents/{id}, or automatically
after DOC_TTL_SECONDS (default 2 hours). Nothing is sent to any external service.
"""
from __future__ import annotations

import os
import shutil
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

DOC_TTL_SECONDS = int(os.environ.get("INVOICE_DOC_TTL_SECONDS", 2 * 60 * 60))


@dataclass
class StoredDoc:
    doc_id: str
    filename: str
    path: Path
    created: float


class DocumentStore:
    def __init__(self) -> None:
        self._root = Path(tempfile.mkdtemp(prefix="invoice-std-"))
        self._docs: dict[str, StoredDoc] = {}
        self._lock = threading.Lock()

    def add(self, filename: str, data: bytes) -> StoredDoc:
        self.purge()
        doc_id = uuid.uuid4().hex
        path = self._root / f"{doc_id}.pdf"
        path.write_bytes(data)
        doc = StoredDoc(doc_id, filename, path, time.time())
        with self._lock:
            self._docs[doc_id] = doc
        return doc

    def get(self, doc_id: str) -> StoredDoc | None:
        self.purge()
        with self._lock:
            return self._docs.get(doc_id)

    def read(self, doc_id: str) -> bytes | None:
        doc = self.get(doc_id)
        return doc.path.read_bytes() if doc and doc.path.exists() else None

    def delete(self, doc_id: str) -> bool:
        with self._lock:
            doc = self._docs.pop(doc_id, None)
        if doc:
            doc.path.unlink(missing_ok=True)
            return True
        return False

    def purge(self) -> None:
        now = time.time()
        with self._lock:
            expired = [d for d in self._docs.values() if now - d.created > DOC_TTL_SECONDS]
            for d in expired:
                self._docs.pop(d.doc_id, None)
        for d in expired:
            d.path.unlink(missing_ok=True)

    def close(self) -> None:
        shutil.rmtree(self._root, ignore_errors=True)


from pathlib import Path


class FeedbackStore:
    def __init__(self, base: Path | None = None):
        if base is None:
            base = Path.home() / '.invoice-standardizer'
        base.mkdir(parents=True, exist_ok=True)
        self.path = base / 'feedback.jsonl'

    def add(self, entry: dict):
        import json
        from datetime import datetime
        entry = dict(entry)
        entry.setdefault('ts', datetime.utcnow().isoformat() + 'Z')
        with self.path.open('a', encoding='utf-8') as f:
            f.write(json.dumps(entry, ensure_ascii=False) + '\n')

    def all(self):
        import json
        out = []
        if not self.path.exists():
            return out
        for line in self.path.read_text(encoding='utf-8').splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                continue
        return out
