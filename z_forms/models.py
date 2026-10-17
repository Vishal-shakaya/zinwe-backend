"""Every form a visitor submits on the website (contact, setup call, ...), kept for the team's Forms inbox."""
from __future__ import annotations

from django.conf import settings
from django.db import models
from django.utils import timezone


class FormSubmission(models.Model):
    """
    One submitted website form. The common fields (who, what) are columns; anything specific to one
    form (team size, platforms, ...) goes in `details`, so a new form needs no migration.
    """

    class Status(models.TextChoices):
        NEW = "new", "New"
        HANDLED = "handled", "Handled"

    kind = models.SlugField(max_length=40, help_text="Which form: contact, setup_call, ... (see z_forms/kinds.py).")
    name = models.CharField(max_length=120, blank=True)
    email = models.EmailField(blank=True, help_text="Stored lowercased.")
    company = models.CharField(max_length=160, blank=True)
    phone = models.CharField(max_length=32, blank=True)
    subject = models.CharField(max_length=160, blank=True, help_text="Topic or short title, if the form has one.")
    message = models.TextField(blank=True)
    details = models.JSONField(default=dict, blank=True, help_text="Fields only this form has.")
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.NEW)
    note = models.TextField(blank=True, help_text="The team's internal note; never shown to the visitor.")
    handled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    handled_at = models.DateTimeField(null=True, blank=True)
    ip = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=255, blank=True)
    # A default (not auto_now_add) so older sign-ups keep their real date when backfilled.
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["kind", "status"]),
            models.Index(fields=["created_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.kind}: {self.name or self.email} ({self.status})"
