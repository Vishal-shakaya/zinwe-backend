"""Customer support: an owner opens a conversation from the dashboard; Zinwe staff answer it in the admin app."""
from __future__ import annotations

from django.db import models


class SupportTicket(models.Model):
    """One support conversation with an owner (their dashboard's floating Support chat)."""

    class Status(models.TextChoices):
        OPEN = "open", "Waiting for us"
        ANSWERED = "answered", "Answered"
        CLOSED = "closed", "Closed"

    class Category(models.TextChoices):
        QUESTION = "question", "Question"
        PROBLEM = "problem", "Something isn't working"
        BILLING = "billing", "Billing"
        SETUP = "setup", "Setting up PCs"
        IDEA = "idea", "Idea or request"

    owner = models.ForeignKey("z_auth.User", on_delete=models.CASCADE, related_name="support_tickets")
    subject = models.CharField(max_length=160)
    category = models.CharField(max_length=16, choices=Category.choices, default=Category.QUESTION)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.OPEN, db_index=True)
    # Where in the dashboard it was opened from (helps staff reproduce).
    page = models.CharField(max_length=120, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True, db_index=True)

    class Meta:
        ordering = ["-updated_at", "-id"]

    def __str__(self) -> str:
        return f"#{self.pk} {self.subject} ({self.status})"


class SupportMessage(models.Model):
    """One line in a support conversation, from the owner or from Zinwe staff."""

    class Sender(models.TextChoices):
        OWNER = "owner", "Customer"
        STAFF = "staff", "Zinwe team"

    ticket = models.ForeignKey(SupportTicket, on_delete=models.CASCADE, related_name="messages")
    sender = models.CharField(max_length=8, choices=Sender.choices)
    author = models.ForeignKey(
        "z_auth.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+",
    )
    body = models.TextField(max_length=4000)
    read_by_owner = models.BooleanField(default=False)
    read_by_staff = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "id"]

    def __str__(self) -> str:
        return f"{self.sender} on #{self.ticket_id}"
