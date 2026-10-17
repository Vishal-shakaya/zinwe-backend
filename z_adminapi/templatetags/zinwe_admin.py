"""
Template helpers for the staff-facing emails (payment_received_team, ...): absolute Back office links and
amounts as people read them ("₹1,145.78", "$144.00"). `{% load zinwe_admin %}`.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlencode

from django import template
from django.conf import settings

register = template.Library()


@register.simple_tag
def admin_url(path: str = "", **query: Any) -> str:
    """`{% admin_url "subscribers" review=payment.id %}` -> https://admin.zinwe.io/subscribers?review=12"""
    base = getattr(settings, "ZINWE_ADMIN_URL", "").rstrip("/")
    url = f"{base}/{path.lstrip('/')}" if path else base
    params = {k: v for k, v in query.items() if v not in (None, "")}
    return f"{url}?{urlencode(params)}" if params else url


def _indian_grouping(whole: str) -> str:
    """1234567 -> 12,34,567 (Indian digit grouping)."""
    if len(whole) <= 3:
        return whole
    head, tail = whole[:-3], whole[-3:]
    groups = []
    while len(head) > 2:
        groups.insert(0, head[-2:])
        head = head[:-2]
    if head:
        groups.insert(0, head)
    return ",".join(groups) + "," + tail


def format_money(amount: Any, currency: str = "INR") -> str:
    """'1145.78' + INR -> '₹1,145.78'; '971.00' -> '₹971'; '144' + USD -> '$144.00'."""
    try:
        value = Decimal(str(amount)).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError, TypeError):
        return str(amount)
    if (currency or "INR").upper() == "USD":
        return f"${value:,.2f}"
    whole, _, cents = f"{abs(value):.2f}".partition(".")
    text = "₹" + _indian_grouping(whole) + ("" if cents == "00" else f".{cents}")
    return f"-{text}" if value < 0 else text


@register.filter
def money(amount: Any, currency: str = "INR") -> str:
    return format_money(amount, currency)


@register.simple_tag
def amount_due(payment: Any) -> str:
    """
    What the customer had to pay for this payment, with the tax spelled out: "₹971 (no GST)",
    "₹1,145.78 (₹971 + GST ₹174.78)", "$144.00 (export, no GST)". From the quote frozen on the payment.
    """
    due = getattr(payment, "due", None) or {}
    currency = due.get("currency") or getattr(payment, "currency", "INR")
    total = due.get("total")
    if not total:
        return format_money(getattr(payment, "amount_inr", 0), currency)
    tax_total = Decimal(str(due.get("tax_total") or "0"))
    if tax_total > 0:
        taxes = " + ".join(f"{t.get('label', 'GST')} {format_money(t.get('amount'), currency)}" for t in due.get("taxes", []))
        return f"{format_money(total, currency)} ({format_money(due.get('amount'), currency)} + {taxes})"
    note = "export, no GST" if due.get("export") else "no GST"
    return f"{format_money(total, currency)} ({note})"
