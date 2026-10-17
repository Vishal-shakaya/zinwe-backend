"""
History retention: each plan keeps `history_days` of alerts, activity and work log (z_auth.entitlements).

Reads already hide anything older (z_devices.services). This job deletes it for good once it is
`GRACE_DAYS` past the window, so an owner who upgrades within a month gets that history back.
Runs with the other scheduled jobs (`manage.py run_jobs`).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from django.utils import timezone

from z_auth.entitlements import entitlements_for
from z_auth.models import User

from .models import Device, DeviceEvent, UsageMetric, WorkLog

GRACE_DAYS = 30


@dataclass
class PruneResult:
    owners: int = 0
    events: int = 0
    metrics: int = 0
    work_log: int = 0
    failed: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"{self.events} event(s), {self.metrics} usage row(s), {self.work_log} work-log row(s) deleted "
            f"for {self.owners} owner(s), {len(self.failed)} failed"
        )


def prune_history(now: datetime | None = None, dry_run: bool = False) -> PruneResult:
    """Delete each owner's history older than their plan's window + GRACE_DAYS. Never raises per owner."""
    now = now or timezone.now()
    result = PruneResult()
    owner_ids = set(Device.objects.values_list("owner_id", flat=True)) | set(
        WorkLog.objects.values_list("owner_id", flat=True)
    )
    for owner in User.objects.filter(pk__in=owner_ids):
        try:
            ent = entitlements_for(owner)
            if not ent.history_days:
                continue
            cutoff = now - timedelta(days=ent.history_days + GRACE_DAYS)
            events = DeviceEvent.objects.filter(device__owner=owner, occurred_at__lt=cutoff)
            metrics = UsageMetric.objects.filter(device__owner=owner, bucket_start__lt=cutoff)
            logs = WorkLog.objects.filter(owner=owner, at__lt=cutoff)
            counts = (events.count(), metrics.count(), logs.count())
            if any(counts):
                result.owners += 1
                result.events += counts[0]
                result.metrics += counts[1]
                result.work_log += counts[2]
                if not dry_run:
                    events.delete()
                    metrics.delete()
                    logs.delete()
        except Exception as exc:  # noqa: BLE001 -- one owner's failure mustn't stop the rest
            result.failed.append(f"{owner.pk}: {exc!r}")
    return result
