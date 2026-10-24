"""Delivery log for every notification Zinwe attempts to send."""
from django.db import models


class Notification(models.Model):
    """One send attempt on one channel to one recipient."""

    class Status(models.TextChoices):
        QUEUED = "queued", "Queued"
        SENT = "sent", "Sent"
        FAILED = "failed", "Failed"

    channel = models.CharField(max_length=40, db_index=True)
    recipient = models.CharField(max_length=254)
    subject = models.CharField(max_length=255, blank=True)
    body = models.TextField(blank=True)
    template = models.CharField(max_length=100, blank=True, db_index=True)
    status = models.CharField(
        max_length=10, choices=Status.choices, default=Status.QUEUED, db_index=True
    )
    error = models.TextField(blank=True)
    ref_type = models.CharField(
        max_length=100, blank=True, help_text='Related object type, e.g. "z_auth.earlyaccessregistration".'
    )
    ref_id = models.CharField(max_length=64, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    # Retries (FIX-008, z_notification/retry.py). A retryable email keeps what it needs to be sent again
    # (`retry_payload`: HTML, headers, reply-to, attachments) only until it is delivered or given up.
    attempts = models.PositiveSmallIntegerField(default=1, db_default=1, help_text="Send attempts so far.")
    next_retry_at = models.DateTimeField(null=True, blank=True, db_index=True)
    retry_payload = models.JSONField(null=True, blank=True, editable=False)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["ref_type", "ref_id"])]

    def __str__(self) -> str:
        return f"[{self.channel}] {self.template or self.subject} -> {self.recipient} ({self.status})"
