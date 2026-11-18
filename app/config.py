"""Optional local configuration.

own_company.txt (one name per line) lists the names of YOUR company. A block that carries one of
these names is treated as the CUSTOMER (you receive the invoice), never as the supplier.
Override the file location with the INVOICE_OWN_COMPANY_FILE environment variable.
"""
from __future__ import annotations

import os
from pathlib import Path

from .numbers import fold

DEFAULT_FILE = Path(__file__).resolve().parents[1] / "own_company.txt"


def own_company_names() -> list[str]:
    path = Path(os.environ.get("INVOICE_OWN_COMPANY_FILE", DEFAULT_FILE))
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    names = [fold(l.strip()) for l in lines if l.strip() and not l.strip().startswith("#")]
    return sorted(set(names), key=len, reverse=True)


def matches_own_company(text: str) -> str | None:
    """Return the configured name found in `text` (whole words), else None."""
    import re

    f = fold(text)
    for name in own_company_names():
        if re.search(rf"(?<![a-z0-9]){re.escape(name)}(?![a-z0-9])", f):
            return name
    return None
