"""
Shared quotes: what an owner has to pay next, with every way to pay, as a PDF and a public page.

- `quote_document(subscription)` — the quote: seller, customer, plan, amounts (z_auth.pricing, worked
  out now, so it always matches the payment page) and the payment details (UPI, bank, Skydo).
- `quote_pdf(doc)` — the same as a PDF (fpdf2 core fonts, like invoices: "INR 1,145.78").
- `email_quote(subscription, recipient)` — emails the PDF as an attachment, with the public link.
- The public page is /quote/<token> on the website (GET api/auth/quotes/<token>/). The token is made the
  first time the quote is shared and is the only key: anyone with the link sees the quote, which is the
  point (forward it to whoever pays). It stops working once nothing is due on that subscription.

A quote is not an invoice: no number from the invoice series, no tax-invoice wording. The invoice is
still issued when staff confirm the payment (z_auth.invoices).
"""
from __future__ import annotations

import io
import secrets
from datetime import timedelta
from decimal import Decimal
from typing import Any

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from z_notification.services import notify

from . import billing
from .invoices import _fmt, _latin, amount_in_words, financial_year
from .models import Subscription

Status = Subscription.Status


class QuoteNotAvailable(Exception):
    """Nothing is due on this subscription (free plan, trial, cancelled or expired)."""

    def __str__(self) -> str:
        return "There's nothing to pay on your plan right now, so there's no quote to share."


def is_quotable(subscription: Subscription | None) -> bool:
    """A plan waiting for its first payment, or an active paid plan (the quote is for the renewal)."""
    if subscription is None:
        return False
    if subscription.status == Status.PENDING_PAYMENT:
        return True
    return billing.is_paid_active(subscription)


def quote_token_for(subscription: Subscription) -> str:
    """The public link's secret, made once per subscription."""
    if subscription.quote_token:
        return subscription.quote_token
    for _ in range(3):
        try:
            with transaction.atomic():
                # Only fills an empty token, so two tabs sharing at once end up with the same link.
                Subscription.objects.filter(pk=subscription.pk, quote_token__isnull=True).update(
                    quote_token=secrets.token_urlsafe(24)
                )
        except IntegrityError:  # a clash with another subscription's token: astronomically unlikely
            continue
        subscription.refresh_from_db(fields=["quote_token"])
        if subscription.quote_token:
            return subscription.quote_token
    raise RuntimeError("Couldn't make a quote link; try again.")


def public_url(subscription: Subscription) -> str:
    return f"{settings.ZINWE_APP_URL}/quote/{quote_token_for(subscription)}"


def subscription_for_token(token: str) -> Subscription | None:
    """The quotable subscription behind a public link, or None (unknown token, or nothing due any more)."""
    if not token or len(token) > 64:
        return None
    subscription = (
        Subscription.objects.select_related("plan", "company", "owner").filter(quote_token=token).first()
    )
    if subscription is not None:
        subscription = billing.expire_if_due(subscription)
    return subscription if is_quotable(subscription) else None


def quote_document(subscription: Subscription) -> dict[str, Any]:
    """Everything the public page and the PDF show. Raises `QuoteNotAvailable`."""
    if not is_quotable(subscription):
        raise QuoteNotAvailable()
    due = billing.amount_due(subscription)
    company = subscription.company
    owner = subscription.owner
    today = timezone.localdate()
    seller = dict(settings.ZINWE_INVOICE_SELLER)
    renewal = subscription.status == Status.ACTIVE
    months = due.months
    period = "1 year (12 months)" if months == 12 else ("1 month" if months == 1 else f"{months} months")
    description = f"Zinwe {due.plan_name} plan, {'renewal' if renewal else 'subscription'}, {period}"
    if subscription.plan.max_devices:
        description += f" (up to {subscription.plan.max_devices} PCs)"
    instructions = billing.payment_instructions(subscription, company=company)
    return {
        "number": f"ZWQ/{financial_year(timezone.now())}/{subscription.pk:05d}",
        "issued_on": today.isoformat(),
        "valid_until": (today + timedelta(days=settings.ZINWE_QUOTE_VALID_DAYS)).isoformat(),
        "kind": "renewal" if renewal else "subscription",
        "seller": {k: seller.get(k, "") for k in ("name", "address", "email", "gstin")},
        "customer": {
            "name": company.name if company else (owner.full_name or owner.email),
            "contact": owner.full_name,
            "address": ", ".join(
                p for p in [getattr(company, "city", ""), getattr(company, "state", ""),
                            getattr(company, "country", "")] if p
            ),
            "gstin": getattr(company, "gstin", "") or "",
        },
        "plan": {"code": subscription.plan.code, "name": subscription.plan.name,
                 "max_devices": subscription.plan.max_devices},
        "description": description,
        "quote": due.as_dict(),
        "total_in_words": amount_in_words(due.total, due.currency),
        "instructions": instructions,
        "public_url": public_url(subscription),
    }


# --- PDF ------------------------------------------------------------------------------------


def _upi_qr_png(instructions: dict[str, Any], quote: dict[str, Any]) -> bytes | None:
    """The UPI QR (exact amount) as a PNG for the PDF; None when UPI isn't set up or it's not an INR quote."""
    upi_id = instructions.get("upi_id") or ""
    if not upi_id or quote.get("currency") != "INR" or Decimal(quote["total"]) <= 0:
        return None
    import segno
    from urllib.parse import quote as q

    payee = instructions.get("payee_name", "")
    uri = f"upi://pay?pa={q(upi_id)}&pn={q(payee)}&am={quote['total']}&cu=INR&tn={q(('Zinwe ' + quote['plan_name'])[:40])}"
    buf = io.BytesIO()
    segno.make(uri, error="m").save(buf, kind="png", scale=6, border=2, dark="#0d0d0e")
    return buf.getvalue()


def quote_pdf(doc: dict[str, Any]) -> bytes:
    from fpdf import FPDF  # imported here: only needed for downloads and emails

    quote = doc["quote"]
    cur = quote["currency"]
    ins = doc["instructions"]
    pdf = FPDF(format="A4")
    pdf.set_auto_page_break(auto=True, margin=18)
    pdf.add_page()
    pdf.set_margins(16, 16, 16)
    w = pdf.w - 32

    def text(value: str, size: float = 10, bold: bool = False, h: float = 5.5, color: tuple[int, int, int] = (20, 20, 20)) -> None:
        pdf.set_text_color(*color)
        pdf.set_font("Helvetica", "B" if bold else "", size)
        pdf.multi_cell(w, h, _latin(value), new_x="LMARGIN", new_y="NEXT")
        pdf.set_text_color(20, 20, 20)

    def kv(label: str, value: str, x: float, width: float) -> None:
        if not value:
            return
        link = value if value.startswith("https://") else ""
        pdf.set_x(x)
        pdf.set_font("Helvetica", "", 9)
        pdf.set_text_color(100, 100, 100)
        pdf.cell(42, 5.5, _latin(label))
        pdf.set_text_color(20, 20, 20)
        pdf.set_font("Helvetica", "B", 9)
        if link:
            pdf.set_text_color(178, 77, 1)
        pdf.multi_cell(width - 42, 5.5, _latin(value), new_x="LMARGIN", new_y="NEXT", link=link)
        pdf.set_text_color(20, 20, 20)

    # Header
    pdf.set_font("Helvetica", "B", 20)
    pdf.set_text_color(253, 109, 2)
    pdf.cell(w / 2, 10, "Zinwe", new_x="RIGHT", new_y="TOP")
    pdf.set_text_color(20, 20, 20)
    pdf.set_font("Helvetica", "B", 13)
    pdf.cell(w / 2, 10, "QUOTATION", align="R", new_x="LMARGIN", new_y="NEXT")
    text("IT Is OK. Security, simplified.", 9, color=(90, 90, 90))
    pdf.ln(4)

    # Seller | quote details
    seller = doc["seller"]
    y = pdf.get_y()
    text("From", 8, True, color=(100, 100, 100))
    text(seller.get("name", ""), 10, True)
    for line in [seller.get("address", ""), seller.get("email", "")]:
        if line:
            text(line, 9)
    if seller.get("gstin"):
        text(f"GSTIN: {seller['gstin']}", 9)
    left_end = pdf.get_y()
    pdf.set_xy(16 + w / 2, y)
    for label, value in [("Quote no.", doc["number"]), ("Date", doc["issued_on"]), ("Valid until", doc["valid_until"])]:
        kv(label, value, 16 + w / 2, w / 2)
    pdf.set_y(max(left_end, pdf.get_y()) + 4)

    # Customer
    customer = doc["customer"]
    text("Prepared for", 8, True, color=(100, 100, 100))
    text(customer.get("name", ""), 10, True)
    for line in [customer.get("contact", ""), customer.get("address", "")]:
        if line:
            text(line, 9)
    if customer.get("gstin"):
        text(f"GSTIN: {customer['gstin']}", 9)
    pdf.ln(4)

    # Line + totals
    pdf.set_fill_color(247, 247, 244)
    pdf.set_font("Helvetica", "B", 9)
    pdf.cell(w - 50, 8, "  Description", fill=True)
    pdf.cell(50, 8, f"Amount ({cur})  ", fill=True, align="R", new_x="LMARGIN", new_y="NEXT")
    y0 = pdf.get_y()
    pdf.set_font("Helvetica", "", 9)
    pdf.multi_cell(w - 50, 6, _latin("  " + doc["description"]), new_x="RIGHT", new_y="TOP")
    y1 = pdf.get_y()
    pdf.set_xy(16 + w - 50, y0)
    pdf.cell(50, 6, f"{Decimal(quote['list_amount']):,.2f}  ", align="R")
    pdf.set_y(max(y1, y0 + 8))

    def row(label: str, value: str, bold: bool = False) -> None:
        pdf.set_font("Helvetica", "B" if bold else "", 10 if bold else 9)
        pdf.set_x(16 + w - 100)
        pdf.cell(60, 6.5, _latin(label), align="R")
        pdf.cell(40, 6.5, _latin(value) + "  ", align="R", new_x="LMARGIN", new_y="NEXT")

    pdf.set_draw_color(220, 220, 215)
    pdf.line(16, pdf.get_y() + 1, 16 + w, pdf.get_y() + 1)
    pdf.ln(3)
    if Decimal(quote.get("discount_amount") or 0) > 0:
        row("Launch discount", f"- {Decimal(quote['discount_amount']):,.2f}")
    row("Taxable value" if quote.get("taxes") else "Subtotal", f"{Decimal(quote['amount']):,.2f}")
    for tax in quote.get("taxes", []):
        row(f"{tax['label']} @ {tax['rate']}%", f"{Decimal(tax['amount']):,.2f}")
    if quote.get("export"):
        row("GST", "0.00 (export)")
    pdf.line(16 + w - 100, pdf.get_y() + 1, 16 + w, pdf.get_y() + 1)
    pdf.ln(2)
    row("Total to pay", _fmt(quote["total"], cur), bold=True)
    pdf.ln(2)
    text(doc.get("total_in_words", ""), 9, True)
    pdf.ln(5)

    # How to pay
    text("How to pay", 12, True)
    text(
        f"Pay {_fmt(quote['total'], cur)} by any one of the ways below, then upload the receipt on your Zinwe "
        "payment page. We confirm it (usually within one working day), switch your plan on and email the invoice.",
        9, color=(60, 60, 60),
    )
    pdf.ln(2)

    def block(title: str, lines: list[tuple[str, str]], extra: str = "") -> None:
        lines = [(k, v) for k, v in lines if v]
        if not lines and not extra:
            return
        if pdf.get_y() > pdf.h - 60:
            pdf.add_page()
        text(title, 10, True, color=(178, 77, 1))
        for label, value in lines:
            kv(label, value, 16, w)
        if extra:
            text(extra, 9, color=(60, 60, 60))
        pdf.ln(3)

    qr = _upi_qr_png(ins, quote)
    upi_top = pdf.get_y()
    block("UPI", [("UPI ID", ins.get("upi_id", "")), ("Payee", ins.get("payee_name", "") if ins.get("upi_id") else "")])
    if qr:
        pdf.image(io.BytesIO(qr), x=16 + w - 34, y=upi_top, w=34)
        pdf.set_y(max(pdf.get_y(), upi_top + 37))
    account_type = (ins.get("account_type") or "").strip()
    if account_type and "account" not in account_type.lower():
        account_type += " account"
    block("Bank transfer (NEFT, IMPS or RTGS)", [
        ("Account name", ins.get("payee_name", "") if ins.get("account_number") else ""),
        ("Bank", ins.get("bank_name", "")),
        ("Account type", account_type if ins.get("account_number") else ""),
        ("Account number", ins.get("account_number", "")),
        ("IFSC", ins.get("ifsc", "")),
    ])
    skydo = ins.get("skydo") or {}
    link = ins.get("payment_link") or ""
    block(
        "International (card or US bank, through Skydo)",
        [
            ("Pay online", link),
            ("Account name", skydo.get("account_name", "")),
            ("Bank", skydo.get("bank_name", "")),
            ("Account number", skydo.get("account_number", "")),
            ("Routing (ABA)", skydo.get("routing_number", "")),
            ("SWIFT (wire)", skydo.get("swift", "")),
        ],
    )
    if ins.get("paypal_url"):
        block("PayPal", [("Pay online", ins["paypal_url"])])

    pdf.ln(2)
    notes = [n for n in [ins.get("note", ""), quote.get("note", "")] if n]
    notes.append(f"Prices are valid until {doc['valid_until']}. This quotation is not a tax invoice; "
                 "the invoice is issued once the payment is confirmed.")
    for note in notes:
        text(note, 8, color=(90, 90, 90))
    pdf.set_font("Helvetica", "", 8)
    pdf.set_text_color(90, 90, 90)
    pdf.write(5, "See this quote online, with every payment detail: ")
    pdf.set_text_color(178, 77, 1)
    pdf.write(5, _latin(doc["public_url"]), link=doc["public_url"])
    return bytes(pdf.output())


def quote_filename(doc: dict[str, Any]) -> str:
    return f"Zinwe-quote-{doc['number'].replace('/', '-')}.pdf"


# --- Email ----------------------------------------------------------------------------------


def email_quote(subscription: Subscription, recipient: str) -> dict[str, Any]:
    """Email the quote PDF (attached) and the public link. Returns the document. Raises `QuoteNotAvailable`."""
    doc = quote_document(subscription)
    owner = subscription.owner
    notify(
        "email",
        recipient,
        "quote_sent",
        {
            "doc": doc,
            "first_name": (owner.full_name or "").split(" ")[0] or "there",
            "to_owner": recipient.lower() == owner.email.lower(),
            "owner": owner,
            "total": _fmt(doc["quote"]["total"], doc["quote"]["currency"]),
        },
        ref=subscription,
        attachments=[(quote_filename(doc), quote_pdf(doc), "application/pdf")],
    )
    return doc
