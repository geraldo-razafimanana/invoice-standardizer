"""Pydantic data models shared by the extraction engine, the API and the PDF generator.

Design choice: every value is kept as a *string*. Amounts are stored as canonical
decimal strings ("1234.56") so that nothing is ever rounded or silently changed, and an
empty string always means "not found / not provided" (we never invent values).
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class Party(BaseModel):
    name: str = ""
    address: str = ""
    tax_id: str = ""
    bank_name: str = ""
    bank_account: str = ""
    iban: str = ""
    swift: str = ""
    payment_method: str = ""


class LineItem(BaseModel):
    description: str = ""
    quantity: str = ""
    unit_price: str = ""
    # Either a percentage ("10%") or an amount ("5.00"), exactly as found.
    discount: str = ""
    # Percentage without the % sign ("20", "5.5").
    tax_rate: str = ""
    amount: str = ""


class TaxLine(BaseModel):
    label: str = "Tax"
    rate: str = ""
    amount: str = ""


class Charge(BaseModel):
    label: str = ""
    amount: str = ""


class InvoiceData(BaseModel):
    invoice_number: str = ""
    issue_date: str = ""  # kept as written in the source (never re-interpreted)
    due_date: str = ""
    po_number: str = ""
    payment_terms: str = ""
    supplier: Party = Field(default_factory=Party)
    customer: Party = Field(default_factory=Party)
    items: list[LineItem] = Field(default_factory=list)
    subtotal: str = ""
    discount_total: str = ""
    additional_charges: list[Charge] = Field(default_factory=list)
    taxes: list[TaxLine] = Field(default_factory=list)
    total: str = ""
    currency: str = ""
    amount_in_words: str = ""
    amount_paid: str = ""
    balance_due: str = ""


Severity = Literal["error", "warning", "info"]


class Warning_(BaseModel):
    code: str
    severity: Severity
    message: str
    field: str = ""


class PageInfo(BaseModel):
    page: int
    method: Literal["text", "ocr"]
    ocr_confidence: float | None = None
    width: float
    height: float


class DocumentInfo(BaseModel):
    doc_id: str
    filename: str
    page_count: int
    method: Literal["text", "ocr", "mixed"]
    pages: list[PageInfo]


class ExtractionResult(BaseModel):
    document: DocumentInfo
    data: InvoiceData
    # Path -> "uncertain" | "missing". Paths look like "invoice_number", "supplier.name",
    # "items.2.amount", "total", ...
    flags: dict[str, Literal["uncertain", "missing"]] = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)
    warnings: list[Warning_] = Field(default_factory=list)
    raw_text: str = ""


class ValidateRequest(BaseModel):
    data: InvoiceData


class GenerateRequest(BaseModel):
    data: InvoiceData
    doc_id: str | None = None
    # Paths the user left flagged as unresolved (printed as "not identified" when essential)


class FeedbackRequest(BaseModel):
    doc_id: str | None = None
    filename: str | None = None
    field: str
    original_value: str = ""
    corrected_value: str
    context: dict | None = None
