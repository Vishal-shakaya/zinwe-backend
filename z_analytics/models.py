"""Append-only product event log."""
from django.db import models
from django.utils import timezone


class Event(models.Model):
    """A named product event, e.g. `early_access.registered`."""

    name = models.CharField(max_length=100, db_index=True)
    properties = models.JSONField(default=dict, blank=True)
    actor_email = models.EmailField(blank=True)
    ref_type = models.CharField(max_length=100, blank=True)
    ref_id = models.CharField(max_length=64, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.name} @ {self.created_at:%Y-%m-%d %H:%M}"


class OpsEvent(models.Model):
    """
    FR-37: one server event for the admin's Logs page (deploy, rollback, backup, API start, 500 error).

    Deploys and rollbacks, backups (with their off-site copy), API starts, Windows agent releases and unhandled
    server errors of this environment.

    Written by `manage.py ops_event` (called by zinwe-server.sh after deploy / rollback / backup), by
    `manage.py serve` when the API starts, and by `z_analytics.ops.OpsErrorHandler` for 500s. Staff actions are NOT
    stored here: the Logs API reads them from the audit trail (`Event` "admin.*") and merges the two.
    """

    class Type(models.TextChoices):
        SERVER = "server", "Server"
        DEPLOY = "deploy", "Deployment"
        BACKUP = "backup", "Backup"
        AGENT = "agent", "Agent release"
        STAFF = "staff", "Staff action"
        ERROR = "error", "Error"

    class Result(models.TextChoices):
        OK = "ok", "OK"
        FAIL = "fail", "Failed"
        ROLLED = "rolled", "Rolled back"

    at = models.DateTimeField(default=timezone.now, db_index=True)
    type = models.CharField(max_length=10, choices=Type.choices, db_index=True)
    label = models.CharField(max_length=40, help_text='Short type label: "Ship", "Promote", "Rollback", "Backup", ...')
    env = models.CharField(max_length=16, blank=True, help_text='"prod", "dev" or "local".')
    what = models.CharField(max_length=300)
    who = models.CharField(max_length=254, default="system", help_text='A staff email, or "system".')
    result = models.CharField(max_length=10, choices=Result.choices, default=Result.OK)
    result_label = models.CharField(max_length=40, blank=True, help_text='Custom result text ("Down 4 min").')
    details = models.JSONField(default=list, blank=True, help_text="[[label, value], ...] in display order.")
    steps = models.JSONField(default=list, blank=True, help_text="[[label, ok|fail|skip, note], ...].")
    source = models.CharField(max_length=60, blank=True, help_text='Who wrote it: "zinwe-server", "django.request", ...')
    unresolved = models.BooleanField(
        default=False, db_index=True, help_text="A failure nobody has fixed yet: the Logs dot and Needs attention."
    )
    resolved_at = models.DateTimeField(null=True, blank=True)
    resolved_by = models.CharField(max_length=254, blank=True)

    class Meta:
        ordering = ["-at", "-id"]

    def __str__(self) -> str:
        return f"{self.type} {self.result} @ {self.at:%Y-%m-%d %H:%M}: {self.what}"
