"""
Tax invoices: issued when staff confirm a payment, frozen, numbered without gaps per financial year.

- `issue_invoice(payment)` — called inside `billing.verify_payment`: takes the next number for the
  Indian financial year (April-March), e.g. ZW/2026-27/0001, and freezes everything printed.
- `invoice_for_payment(payment)` — the frozen invoice (issued now for a payment confirmed before
  invoices were stored).
- `invoice_pdf(invoice)` — the PDF the owner and staff download (fpdf2, built-in fonts: amounts are
  written "INR 1,145.78" / "USD 144.00" because the core fonts have no ₹ sign).

What's printed comes from the quote frozen on the payment when it was submitted (`ManualPayment.due`),
so the invoice shows what was charged — not today's prices.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Max
from django.utils import timezone

from . import pricing
from .models import Invoice, ManualPayment

PREFIX = "ZW"


def financial_year(moment: datetime) -> str:
    """Indian financial year of `moment`: April 2026 - March 2027 -> "2026-27"."""
    local = timezone.localtime(moment)
    start = local.year if local.month >= 4 else local.year - 1
    return f"{start}-{(start + 1) % 100:02d}"


# --- Amount in words (Indian numbering for INR, international for USD) --------------------

_ONES = (
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen "
    "sixteen seventeen eighteen nineteen"
).split()
_TENS = "_ _ twenty thirty forty fifty sixty seventy eighty ninety".split()


def _below_thousand(n: int) -> str:
    words = []
    if n >= 100:
        words.append(f"{_ONES[n // 100]} hundred")
        n %= 100
    if n >= 20:
        words.append(_TENS[n // 10] + (f"-{_ONES[n % 10]}" if n % 10 else ""))
    elif n or not words:
        words.append(_ONES[n])
    return " ".join(words)


def _words(n: int, indian: bool) -> str:
    if n == 0:
        return "zero"
    scales = [(10**7, "crore"), (10**5, "lakh"), (1000, "thousand")] if indian else [
        (10**9, "billion"), (10**6, "million"), (1000, "thousand")
    ]
    parts = []
    for size, name in scales:
        if n >= size:
            parts.append(f"{_words(n // size, indian) if n // size >= 1000 else _below_thousand(n // size)} {name}")
            n %= size
    if n:
        parts.append(_below_thousand(n))
    return " ".join(parts)


def amount_in_words(amount: Decimal, currency: str) -> str:
    whole = int(amount)
    fraction = int((amount - whole) * 100)
    if currency == pricing.INR:
        text = f"Rupees {_words(whole, indian=True)}"
        if fraction:
            text += f" and {_words(fraction, indian=True)} paise"
    else:
        text = f"US dollars {_words(whole, indian=False)}"
        if fraction:
            text += f" and {_words(fraction, indian=False)} cents"
    text = f"{text} only"
    return text[:1].upper() + text[1:]  # not .capitalize(): that lowercases "US" (J3-25)


# --- Issuing --------------------------------------------------------------------------------


def _due(payment: ManualPayment) -> dict[str, Any]:
    """The quote frozen on the payment, or (older payments) one worked out now in its currency."""
    if payment.due:
        return payment.due
    subscription = payment.subscription
    plan = payment.upgrade_plan or subscription.plan
    return pricing.quote(plan, subscription.company, payment.currency, subscription.billing_cycle).as_dict()


def _data(payment: ManualPayment, number: str, fy: str, issued_at: datetime) -> dict[str, Any]:
    subscription = payment.subscription
    company = subscription.company
    owner = subscription.owner
    due = _due(payment)
    seller = dict(settings.ZINWE_INVOICE_SELLER)
    seller["state"] = settings.ZINWE_GST["seller_state"]
    kind = "Upgrade" if payment.upgrade_plan_id else ("Renewal" if payment.is_renewal else "Subscription")
    months = int(due.get("months") or 1)
    cycle = f"{months} months" if months > 1 else "1 month"
    plan = payment.upgrade_plan or subscription.plan
    description = f"Zinwe {due.get('plan_name') or plan.name} plan — {kind.lower()}, {cycle}"
    if plan.max_devices:
        description += f" (up to {plan.max_devices} PCs)"
    currency = due["currency"]
    total = Decimal(due["total"])
    registered = bool((seller.get("gstin") or "").strip())
    if due.get("export"):
        title = "Tax invoice (export of services)" if registered else "Invoice (export of services)"
    else:
        title = "Tax invoice" if registered else "Invoice"
    notes = [n for n in [due.get("note", "")] if n]
    notes.append("This is a computer-generated invoice and needs no signature.")
    return {
        "number": number,
        "title": title,
        "financial_year": fy,
        "issued_on": timezone.localtime(issued_at).date().isoformat(),
        "paid_on": payment.paid_on.isoformat() if payment.paid_on else None,
        "status": "paid",
        "currency": currency,
        "seller": seller,
        "customer": {
            "name": company.name if company else (owner.full_name or owner.email),
            "contact": owner.full_name,
            "email": owner.email,
            "address": ", ".join(p for p in [getattr(company, "city", ""), getattr(company, "state", ""),
                                             getattr(company, "country", "")] if p),
            "state": getattr(company, "state", "") or "",
            "country": getattr(company, "country", "") or "",
            "gstin": getattr(company, "gstin", "") or "",
        },
        "place_of_supply": due.get("place_of_supply", ""),
        "export": bool(due.get("export")),
        "lines": [{
            "description": description,
            "sac": settings.ZINWE_GST["sac"],
            "period_from": payment.covers_from.date().isoformat() if payment.covers_from else None,
            "period_until": payment.covers_until.date().isoformat() if payment.covers_until else None,
            "list_amount": due["list_amount"],
            "discount_amount": due["discount_amount"],
            "amount": due["amount"],
            "amount_inr": due["amount"],  # legacy key (the amount, in `currency`)
        }],
        "taxable_amount": due["amount"],
        "taxes": due.get("taxes", []),
        "tax_total": due.get("tax_total", "0.00"),
        "total": due["total"],
        "total_inr": due["total"],  # legacy key (the total, in `currency`)
        "total_in_words": amount_in_words(total, currency),
        "payment": {
            "method": payment.get_method_display(),
            # What staff read off the receipt wins over what the owner typed (often nothing).
            "reference": (payment.staff_reference or payment.reference or "").strip(),
            "payer": payment.payer_name,
            "amount_paid": str(payment.amount_inr),
        },
        "notes": notes,
        "note": " ".join(notes[:-1]),  # legacy single-line note
    }


def issue_invoice(payment: ManualPayment) -> Invoice:
    """Number and freeze the invoice for a confirmed payment (idempotent). Call inside a transaction."""
    existing = Invoice.objects.filter(payment=payment).first()
    if existing is not None:
        return existing
    issued_at = payment.reviewed_at or timezone.now()
    fy = financial_year(issued_at)
    for _ in range(5):  # two confirmations at once: the loser takes the next number
        last = Invoice.objects.filter(financial_year=fy).aggregate(m=Max("sequence"))["m"] or 0
        sequence = last + 1
        number = f"{PREFIX}/{fy}/{sequence:04d}"
        data = _data(payment, number, fy, issued_at)
        try:
            with transaction.atomic():
                return Invoice.objects.create(
                    payment=payment, number=number, financial_year=fy, sequence=sequence, issued_at=issued_at,
                    currency=data["currency"], total=Decimal(data["total"]), data=data,
                )
        except IntegrityError:
            existing = Invoice.objects.filter(payment=payment).first()
            if existing is not None:
                return existing
    raise RuntimeError("Couldn't take an invoice number; try again.")


def invoice_for_payment(payment: ManualPayment) -> Invoice:
    """The invoice of a confirmed payment (issued now for one confirmed before invoices were stored)."""
    with transaction.atomic():
        return issue_invoice(payment)


# --- PDF ------------------------------------------------------------------------------------


def _fmt(amount: str | Decimal, currency: str) -> str:
    return f"{currency} {Decimal(amount):,.2f}"


def _latin(text: str) -> str:
    """The PDF core fonts are Latin-1: swap the few characters our text uses that it lacks."""
    return (
        (text or "").replace("—", "-").replace("–", "-").replace("₹", "INR ").replace("’", "'").replace("·", "-")
        .encode("latin-1", "replace").decode("latin-1")
    )


def payment_line(payment: dict[str, Any]) -> str:
    """ "UPI - reference 4021 - paid by Asha", leaving out the parts that are empty (J3-12)."""
    parts = [str(payment.get("method") or "").strip()]
    reference = str(payment.get("reference") or "").strip()
    if reference:
        parts.append(f"reference {reference}")
    payer = str(payment.get("payer") or "").strip()
    if payer:
        parts.append(f"paid by {payer}")
    return " - ".join(p for p in parts if p)


def invoice_filename(invoice: Invoice) -> str:
    return f"Zinwe-invoice-{invoice.number.replace('/', '-')}.pdf"


def invoice_pdf(invoice: Invoice) -> bytes:
    from fpdf import FPDF  # imported here: only needed for downloads

    d = invoice.data
    cur = d["currency"]
    pdf = FPDF(format="A4")
    pdf.set_auto_page_break(auto=True, margin=18)
    pdf.add_page()
    pdf.set_margins(16, 16, 16)
    w = pdf.w - 32

    def text(value: str, size: float = 10, bold: bool = False, align: str = "L", h: float = 5.5, width: float = 0) -> None:
        pdf.set_font("Helvetica", "B" if bold else "", size)
        pdf.multi_cell(width or w, h, _latin(value), align=align, new_x="LMARGIN", new_y="NEXT")

    # Header
    pdf.set_font("Helvetica", "B", 20)
    pdf.set_text_color(253, 109, 2)
    pdf.cell(w / 2, 10, "Zinwe", new_x="RIGHT", new_y="TOP")
    pdf.set_text_color(20, 20, 20)
    pdf.set_font("Helvetica", "B", 13)
    pdf.cell(w / 2, 10, _latin(d.get("title", "Invoice")).upper(), align="R", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 9)
    pdf.set_text_color(90, 90, 90)
    pdf.cell(w, 5, "No IT team? It's OK. Security, simplified.", new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(20, 20, 20)
    pdf.ln(4)

    # Seller | invoice details
    seller = d["seller"]
    y = pdf.get_y()
    pdf.set_xy(16, y)
    text("From", 8, True)
    text(seller.get("name", ""), 10, True)
    for line in [seller.get("address", ""), seller.get("email", "")]:
        if line:
            text(line, 9)
    if seller.get("gstin"):
        text(f"GSTIN: {seller['gstin']}", 9)
    left_end = pdf.get_y()
    pdf.set_xy(16 + w / 2, y)
    meta = [
        ("Invoice no.", d["number"]),
        ("Date", d["issued_on"]),
        ("Paid on", d.get("paid_on") or "-"),
        ("Place of supply", d.get("place_of_supply") or "-"),
        ("Status", "Paid"),
    ]
    for label, value in meta:
        pdf.set_x(16 + w / 2)
        pdf.set_font("Helvetica", "", 9)
        pdf.cell(32, 5.5, label)
        pdf.set_font("Helvetica", "B", 9)
        pdf.cell(w / 2 - 32, 5.5, _latin(str(value)), new_x="LMARGIN", new_y="NEXT")
    pdf.set_y(max(left_end, pdf.get_y()) + 4)

    # Customer
    customer = d["customer"]
    text("Billed to", 8, True)
    text(customer.get("name", ""), 10, True)
    for line in [customer.get("contact", ""), customer.get("email", ""), customer.get("address", "")]:
        if line:
            text(line, 9)
    if customer.get("gstin"):
        text(f"GSTIN: {customer['gstin']}", 9)
    pdf.ln(4)

    # Line table
    pdf.set_fill_color(247, 247, 244)
    pdf.set_font("Helvetica", "B", 9)
    cols = [w - 72, 22, 50]
    pdf.cell(cols[0], 8, "  Description", fill=True)
    pdf.cell(cols[1], 8, "SAC", fill=True, align="C")
    pdf.cell(cols[2], 8, f"Amount ({cur})  ", fill=True, align="R", new_x="LMARGIN", new_y="NEXT")
    for line in d["lines"]:
        period = ""
        if line.get("period_from") and line.get("period_until"):
            period = f"\n  Period: {line['period_from']} to {line['period_until']}"
        y0 = pdf.get_y()
        pdf.set_font("Helvetica", "", 9)
        pdf.multi_cell(cols[0], 5.5, _latin("  " + line["description"] + period), new_x="RIGHT", new_y="TOP")
        y1 = pdf.get_y()
        pdf.set_xy(16 + cols[0], y0)
        pdf.cell(cols[1], 5.5, line.get("sac", ""), align="C")
        pdf.cell(cols[2], 5.5, f"{Decimal(line['list_amount']):,.2f}  ", align="R")
        pdf.set_y(max(y1, y0 + 11))

    def row(label: str, value: str, bold: bool = False) -> None:
        pdf.set_font("Helvetica", "B" if bold else "", 10 if bold else 9)
        pdf.set_x(16 + w - 100)
        pdf.cell(60, 6.5, _latin(label), align="R")
        pdf.cell(40, 6.5, _latin(value) + "  ", align="R", new_x="LMARGIN", new_y="NEXT")

    pdf.set_draw_color(220, 220, 215)
    pdf.line(16, pdf.get_y() + 1, 16 + w, pdf.get_y() + 1)
    pdf.ln(3)
    line = d["lines"][0]
    if Decimal(line.get("discount_amount") or 0) > 0:
        row("Discount", f"- {Decimal(line['discount_amount']):,.2f}")
    row("Taxable value", f"{Decimal(d['taxable_amount']):,.2f}")
    for tax in d.get("taxes", []):
        row(f"{tax['label']} @ {tax['rate']}%", f"{Decimal(tax['amount']):,.2f}")
    if not d.get("taxes"):
        row("GST", "0.00" + (" (export)" if d.get("export") else ""))
    pdf.line(16 + w - 100, pdf.get_y() + 1, 16 + w, pdf.get_y() + 1)
    pdf.ln(2)
    row("Total", _fmt(d["total"], cur), bold=True)
    pdf.ln(2)
    text(d.get("total_in_words", ""), 9, True)
    pdf.ln(3)

    payment = d.get("payment", {})
    text("Payment", 8, True)
    text(payment_line(payment), 9)
    pdf.ln(3)
    for note in d.get("notes", []):
        text(note, 8)
    return bytes(pdf.output())
