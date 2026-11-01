"""Marketing: audiences, and one-to-one outreach campaigns that staff approve batch by batch."""
from __future__ import annotations

import secrets

from django.conf import settings
from django.db import models


def new_token() -> str:
    return secrets.token_urlsafe(32)


class Audience(models.Model):
    """Who we target: the group, its main persona, what they need and how to reach them."""

    class Confidence(models.TextChoices):
        GUESS = "Guess", "Guess"
        HEARD_ONCE = "Heard once", "Heard once"
        CONFIRMED = "Confirmed", "Confirmed"

    name = models.CharField(max_length=120)
    country = models.CharField(max_length=40, default="USA")
    decider = models.CharField(max_length=200, blank=True)
    setup = models.CharField(max_length=200, blank=True)
    today = models.CharField(max_length=300, blank=True)
    confidence = models.CharField(max_length=12, choices=Confidence.choices, default=Confidence.GUESS)
    # Lists of short texts (chips). The first channel is the best channel.
    segments = models.JSONField(default=list, blank=True)
    triggers = models.JSONField(default=list, blank=True)
    words = models.JSONField(default=list, blank=True)
    objections = models.JSONField(default=list, blank=True)
    not_for = models.JSONField(default=list, blank=True)
    problems = models.JSONField(default=list, blank=True)
    solutions = models.JSONField(default=list, blank=True)
    offers = models.JSONField(default=list, blank=True)
    channels = models.JSONField(default=list, blank=True)
    summary = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["id"]

    def __str__(self) -> str:
        return self.name


class Campaign(models.Model):
    """One email flow for one audience. Nothing goes out until staff launch it and approve each day's batch."""

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        ACTIVE = "active", "Active"
        PAUSED = "paused", "Paused"

    name = models.CharField(max_length=120)
    audience = models.ForeignKey(Audience, on_delete=models.PROTECT, related_name="campaigns")
    goal = models.CharField(max_length=120, blank=True)
    theme = models.CharField(max_length=60, blank=True)
    problem = models.CharField(max_length=240, blank=True)
    offer = models.CharField(max_length=240, blank=True)
    status = models.CharField(max_length=8, choices=Status.choices, default=Status.DRAFT)
    pause_reason = models.CharField(max_length=240, blank=True)
    daily_limit = models.PositiveSmallIntegerField(default=10)
    # Launch checks that a person answers for.
    rules_checked_at = models.DateTimeField(null=True, blank=True)
    rules_checked_by = models.EmailField(blank=True)
    test_sent_at = models.DateTimeField(null=True, blank=True)
    dns_result = models.JSONField(default=dict, blank=True)  # {"spf": bool, "dkim": bool, "dmarc": bool}
    dns_checked_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["id"]

    def __str__(self) -> str:
        return self.name


class CampaignEmail(models.Model):
    """One step of a campaign's flow. Subject and body may use {{FirstName}} and {{Firm}}."""

    campaign = models.ForeignKey(Campaign, on_delete=models.CASCADE, related_name="emails")
    n = models.PositiveSmallIntegerField()
    stage = models.CharField(max_length=12)  # Cold, Setup, Tips, Final
    when = models.CharField(max_length=30)  # as shown to staff: "Day 0", "Week 3"
    week = models.PositiveSmallIntegerField(default=0)  # sets the gap to the next email
    subject = models.CharField(max_length=200)
    body = models.TextField()

    class Meta:
        ordering = ["campaign_id", "n"]
        constraints = [models.UniqueConstraint(fields=["campaign", "n"], name="unique_email_step")]


class Suppression(models.Model):
    """Do not email: an address blocked from all outreach, for good."""

    email = models.EmailField(unique=True)
    reason = models.CharField(max_length=120)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]


class Contact(models.Model):
    """A hand-picked person in a campaign, and where they are in its flow."""

    class Status(models.TextChoices):
        NEW = "new", "Ready"
        COLD = "cold", "Cold"
        WAITING = "waiting", "Waiting for reply"
        OPTED = "opted", "Opted in"
        SIGNED = "signed", "Signed up"
        PAUSED = "paused", "Paused"
        SNOOZED = "snoozed", "Snoozed"
        CLOSED = "closed", "Closed"
        FINISHED = "finished", "Finished"
        SUPPRESSED = "suppressed", "Do not email"

    campaign = models.ForeignKey(Campaign, on_delete=models.CASCADE, related_name="contacts")
    first_name = models.CharField(max_length=80)
    firm = models.CharField(max_length=160)
    email = models.EmailField()
    city = models.CharField(max_length=80, blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.NEW, db_index=True)
    next_email = models.PositiveSmallIntegerField(default=1)  # 0: nothing planned
    next_date = models.DateField(null=True, blank=True)
    last_email = models.PositiveSmallIntegerField(default=0)
    opted_in_at = models.DateField(null=True, blank=True)
    proof = models.CharField(max_length=240, blank=True)  # their words when they said yes
    replied_at = models.DateField(null=True, blank=True)
    signed_up_at = models.DateField(null=True, blank=True)
    installed = models.BooleanField(default=False)
    reason = models.CharField(max_length=160, blank=True)
    prior = models.JSONField(null=True, blank=True)  # where the flow stood before a question paused it
    token = models.CharField(max_length=64, unique=True, default=new_token, editable=False)
    added_by = models.EmailField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["id"]
        constraints = [models.UniqueConstraint(fields=["campaign", "email"], name="unique_contact_per_campaign")]


class ContactEvent(models.Model):
    """The contact's history, as staff read it."""

    contact = models.ForeignKey(Contact, on_delete=models.CASCADE, related_name="events")
    at = models.DateTimeField(auto_now_add=True)
    text = models.CharField(max_length=240)
    actor = models.EmailField(blank=True)

    class Meta:
        ordering = ["at", "id"]


class OutreachSend(models.Model):
    """One approved email to one contact. `pending` means a person approved it; only these rows are ever sent.

    A row left in `sending` by a crash is marked failed, never sent again by itself: a second cold email is
    worse than a missing one, and the contact simply stays due for the next approved batch."""

    class Status(models.TextChoices):
        PENDING = "pending", "Approved"
        SENDING = "sending", "Sending"
        SENT = "sent", "Sent"
        FAILED = "failed", "Failed"
        SKIPPED = "skipped", "Skipped"

    contact = models.ForeignKey(Contact, on_delete=models.CASCADE, related_name="sends")
    n = models.PositiveSmallIntegerField()
    status = models.CharField(max_length=8, choices=Status.choices, default=Status.PENDING, db_index=True)
    approved_by = models.EmailField()
    approved_at = models.DateTimeField(auto_now_add=True)
    claimed_at = models.DateTimeField(null=True, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    error = models.CharField(max_length=300, blank=True)

    class Meta:
        ordering = ["approved_at", "id"]
        constraints = [models.UniqueConstraint(fields=["contact", "n"], name="one_send_per_step")]


def sender_address() -> str:
    """The bare address outreach comes from."""
    value = settings.ZINWE_OUTREACH_FROM
    return value.split("<")[-1].rstrip(">").strip().lower()
