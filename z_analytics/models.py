"""Append-only product event log."""
from django.db import models


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
