"""Financial consistency checks. They only *report*; they never change a value."""
from __future__ import annotations

from decimal import Decimal

from .models import InvoiceData, Warning_
from .numbers import format_amount, to_decimal

ZERO = Decimal(0)


def _close(a: Decimal, b: Decimal, tol: Decimal) -> bool:
    return abs(a - b) <= tol


def _fmt(d: Decimal) -> str:
    return format_amount(format(d, "f"))


def _discounted(qty: Decimal, unit: Decimal, discount: str) -> Decimal | None:
    d = discount.strip()
    if not d:
        return None
    if d.endswith("%"):
        pct = to_decimal(d.rstrip("%"))
        if pct is None:
            return None
        return qty * unit * (Decimal(100) - abs(pct)) / Decimal(100)
    amt = to_decimal(d)
    if amt is None:
        return None
    return qty * unit - abs(amt)


def validate(data: InvoiceData) -> list[Warning_]:
    w: list[Warning_] = []

    # ---- essentials missing ---------------------------------------------------------
    if not data.invoice_number.strip():
        w.append(Warning_(code="missing_invoice_number", severity="warning", field="invoice_number",
                          message="The original invoice number could not be identified."))
    if not data.supplier.name.strip():
        w.append(Warning_(code="missing_supplier", severity="warning", field="supplier.name",
                          message="The supplier name could not be identified."))
    if not data.items:
        w.append(Warning_(code="no_items", severity="warning", field="items",
                          message="No line items were found. Add them manually if the invoice has some."))
    if not data.total.strip():
        w.append(Warning_(code="missing_total", severity="warning", field="total",
                          message="The invoice total could not be identified."))
    if not data.currency.strip():
        w.append(Warning_(code="missing_currency", severity="info", field="currency",
                          message="No currency was identified."))
    if (data.invoice_number.strip() and data.po_number.strip()
            and data.invoice_number.strip().casefold() == data.po_number.strip().casefold()):
        w.append(Warning_(
            code="same_invoice_po", severity="warning", field="invoice_number",
            message=(f"The invoice number and the purchase-order number are both "
                     f"'{data.invoice_number}'. One of them was probably read from the wrong label.")))

    # ---- unreadable numbers ---------------------------------------------------------
    def bad(label: str, value: str, field: str) -> None:
        if value.strip() and to_decimal(value) is None:
            w.append(Warning_(code="bad_number", severity="warning", field=field,
                              message=f"{label} '{value}' is not a valid number, so it cannot be checked."))

    for i, it in enumerate(data.items):
        bad(f"Line {i + 1}: quantity", it.quantity, f"items.{i}.quantity")
        bad(f"Line {i + 1}: unit price", it.unit_price, f"items.{i}.unit_price")
        bad(f"Line {i + 1}: amount", it.amount, f"items.{i}.amount")
    bad("Subtotal", data.subtotal, "subtotal")
    bad("Total", data.total, "total")
    bad("Amount paid", data.amount_paid, "amount_paid")
    bad("Balance due", data.balance_due, "balance_due")

    # ---- line level: quantity x unit price = amount ---------------------------------
    line_amounts: list[Decimal] = []
    for i, it in enumerate(data.items):
        qty, unit, amt = to_decimal(it.quantity), to_decimal(it.unit_price), to_decimal(it.amount)
        if amt is not None:
            line_amounts.append(amt)
        if qty is not None and unit is not None and amt is not None:
            candidates = [qty * unit]
            disc = _discounted(qty, unit, it.discount)
            if disc is not None:
                candidates.append(disc)
            tol = max(Decimal("0.02"), abs(qty) * Decimal("0.005"))
            if not any(_close(c, amt, tol) for c in candidates):
                w.append(Warning_(
                    code="line_mismatch", severity="warning", field=f"items.{i}.amount",
                    message=(f"Line {i + 1}: quantity × unit price = {_fmt(candidates[0])}"
                             + (f" (or {_fmt(candidates[1])} after discount)" if len(candidates) > 1 else "")
                             + f", but the line amount is {_fmt(amt)}.")))

    # ---- sum of lines vs subtotal ---------------------------------------------------
    subtotal = to_decimal(data.subtotal)
    total = to_decimal(data.total)
    items_sum = sum(line_amounts, ZERO) if line_amounts and len(line_amounts) == len(data.items) else None
    tol_sum = Decimal("0.02") + Decimal("0.005") * len(data.items)
    if subtotal is not None and items_sum is not None and not _close(items_sum, subtotal, tol_sum):
        w.append(Warning_(
            code="subtotal_mismatch", severity="warning", field="subtotal",
            message=f"The line amounts add up to {_fmt(items_sum)} but the subtotal is {_fmt(subtotal)}."))

    # ---- taxes ----------------------------------------------------------------------
    tax_total = ZERO
    tax_known = bool(data.taxes) and all(to_decimal(t.amount) is not None for t in data.taxes)
    if tax_known:
        tax_total = sum((to_decimal(t.amount) or ZERO for t in data.taxes), ZERO)
    base = subtotal if subtotal is not None else items_sum
    disc_total = abs(to_decimal(data.discount_total) or ZERO)
    charges = sum((to_decimal(c.amount) or ZERO for c in data.additional_charges), ZERO)

    # per-line tax rates (only when every line carries a rate and an amount)
    if (tax_known and data.items and items_sum is not None
            and all(to_decimal(it.tax_rate) is not None for it in data.items)):
        expected = sum(((to_decimal(it.amount) or ZERO) * (to_decimal(it.tax_rate) or ZERO) / 100 for it in data.items), ZERO)
        if not _close(expected, tax_total, Decimal("0.05") + Decimal("0.01") * len(data.items)):
            w.append(Warning_(
                code="tax_mismatch", severity="warning", field="taxes",
                message=f"Using each line's tax rate, taxes should be {_fmt(expected)} but {_fmt(tax_total)} is declared."))
    elif tax_known and base is not None and len(data.taxes) == 1 and to_decimal(data.taxes[0].rate) is not None:
        rate = to_decimal(data.taxes[0].rate) or ZERO
        taxable = base - disc_total
        expected = taxable * rate / 100
        if not _close(expected, tax_total, Decimal("0.05")) and not _close(base * rate / 100, tax_total, Decimal("0.05")):
            w.append(Warning_(
                code="tax_mismatch", severity="warning", field="taxes.0.amount",
                message=f"{rate.normalize():f}% of {_fmt(taxable)} is {_fmt(expected)}, but the declared tax is {_fmt(tax_total)}."))

    # ---- subtotal / discount / charges / taxes -> total -----------------------------
    if total is not None and base is not None:
        excl = base - disc_total + charges + tax_total       # taxes added on top
        incl = base - disc_total + charges                   # prices already include taxes
        tol = Decimal("0.05")
        if not (_close(excl, total, tol) or _close(incl, total, tol)):
            what = "subtotal"
            if disc_total:
                what += " − discount"
            if charges:
                what += " + additional charges"
            if tax_known:
                what += " + taxes"
            what = what[0].upper() + what[1:]
            w.append(Warning_(
                code="total_mismatch", severity="warning", field="total",
                message=f"{what} = {_fmt(excl)}, but the declared total is {_fmt(total)} (difference {_fmt(abs(excl - total))})."))

    # ---- payments -------------------------------------------------------------------
    paid, balance = to_decimal(data.amount_paid), to_decimal(data.balance_due)
    if total is not None and paid is not None and balance is not None:
        if not _close(total - paid, balance, Decimal("0.05")):
            w.append(Warning_(
                code="balance_mismatch", severity="warning", field="balance_due",
                message=f"Total {_fmt(total)} − paid {_fmt(paid)} = {_fmt(total - paid)}, but the balance due is {_fmt(balance)}."))

    # ---- sanity ---------------------------------------------------------------------
    for i, it in enumerate(data.items):
        if it.description.strip() == "" and (it.amount.strip() or it.unit_price.strip()):
            w.append(Warning_(code="item_no_description", severity="info", field=f"items.{i}.description",
                              message=f"Line {i + 1} has no description."))
    return w
