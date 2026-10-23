"""
Online payments through Razorpay. One `RazorpayOrder` per checkout attempt: what it's for (frozen when the order
is made, like a quote), its Razorpay ids, and what happened. A paid order becomes an ordinary, already-approved
`z_auth.ManualPayment` (method "razorpay"), so plans, invoices and emails work exactly as for a receipt.
"""
from __future__ import annotations

from django.db import models


class RazorpayOrder(models.Model):
    class Status(models.TextChoices):
        CREATED = "created", "Waiting for payment"
        PAID = "paid", "Paid, plan updated"
        # Money was taken but the plan couldn't be updated automatically (it changed meanwhile): the payment is
        # left for staff to approve in the admin app, like a receipt. Never lost.
        NEEDS_REVIEW = "needs_review", "Paid, waiting for staff"
        FAILED = "failed", "Failed"

    owner = models.ForeignKey("z_auth.User", on_delete=models.CASCADE, related_name="razorpay_orders")
    subscription = models.ForeignKey("z_auth.Subscription", on_delete=models.CASCADE, related_name="razorpay_orders")
    upgrade_plan = models.ForeignKey(
        "z_auth.Plan", null=True, blank=True, on_delete=models.PROTECT, related_name="+",
    )
    is_renewal = models.BooleanField(default=False)
    # The admin offer (an Announcement with an offer price) this order was priced from; a paid order uses it up.
    offer = models.ForeignKey(
        "z_auth.Announcement", null=True, blank=True, on_delete=models.SET_NULL, related_name="razorpay_orders",
    )
    # What the order asked for, frozen (the quote) -- the amount Razorpay was told is amount_paise.
    due = models.JSONField(default=dict)
    amount_paise = models.PositiveIntegerField()
    currency = models.CharField(max_length=3, default="INR")
    order_id = models.CharField(max_length=40, unique=True)
    receipt = models.CharField(max_length=40)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.CREATED, db_index=True)
    razorpay_payment_id = models.CharField(max_length=40, blank=True, db_index=True)
    # The approved payment it became (null until paid).
    payment = models.OneToOneField(
        "z_auth.ManualPayment", null=True, blank=True, on_delete=models.SET_NULL, related_name="razorpay_order",
    )
    terms_accepted = models.BooleanField(default=False)
    # How it was confirmed: "checkout" (the browser), "webhook" or "reconcile" (the hourly check).
    confirmed_via = models.CharField(max_length=20, blank=True)
    error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    paid_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self) -> str:
        return f"{self.order_id} ({self.get_status_display()})"
