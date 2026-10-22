"""
The few Razorpay REST calls we need (orders, payments, capture), over HTTPS with basic auth, standard library only.
https://razorpay.com/docs/api/ -- amounts are in paise.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import urllib.error
import urllib.request
from typing import Any

from django.conf import settings

API = "https://api.razorpay.com/v1"
TIMEOUT = 20


class RazorpayError(Exception):
    """Razorpay refused the call or couldn't be reached. `status` is the HTTP status (0 = network)."""

    def __init__(self, message: str, status: int = 0) -> None:
        super().__init__(message)
        self.status = status


def _call(method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    token = base64.b64encode(f"{settings.RAZORPAY_KEY_ID}:{settings.RAZORPAY_KEY_SECRET}".encode()).decode()
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        f"{API}{path}", data=data, method=method,
        headers={"Authorization": f"Basic {token}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:  # noqa: S310 -- fixed https host
            return json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read() or b"{}").get("error", {}).get("description", "")
        except ValueError:
            detail = ""
        raise RazorpayError(detail or f"Razorpay answered {exc.code}", exc.code) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise RazorpayError(f"Couldn't reach Razorpay: {exc}") from exc


def create_order(amount_paise: int, currency: str, receipt: str, notes: dict[str, str]) -> dict[str, Any]:
    return _call("POST", "/orders", {"amount": amount_paise, "currency": currency, "receipt": receipt, "notes": notes})


def fetch_payment(payment_id: str) -> dict[str, Any]:
    return _call("GET", f"/payments/{payment_id}")


def order_payments(order_id: str) -> list[dict[str, Any]]:
    return _call("GET", f"/orders/{order_id}/payments").get("items", [])


def capture(payment_id: str, amount_paise: int, currency: str) -> dict[str, Any]:
    return _call("POST", f"/payments/{payment_id}/capture", {"amount": amount_paise, "currency": currency})


def _hmac(message: bytes, secret: str) -> str:
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def checkout_signature_ok(order_id: str, payment_id: str, signature: str) -> bool:
    """The checkout's razorpay_signature: HMAC-SHA256("<order_id>|<payment_id>", key secret)."""
    expected = _hmac(f"{order_id}|{payment_id}".encode(), settings.RAZORPAY_KEY_SECRET)
    return hmac.compare_digest(expected, signature or "")


def webhook_signature_ok(body: bytes, signature: str) -> bool:
    """X-Razorpay-Signature: HMAC-SHA256(raw body, webhook secret)."""
    secret = settings.RAZORPAY_WEBHOOK_SECRET
    return bool(secret) and hmac.compare_digest(_hmac(body, secret), signature or "")
