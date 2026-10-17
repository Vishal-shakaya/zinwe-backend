"""Record website form submissions and serve the admin app's Forms inbox."""
from __future__ import annotations

from typing import Any

from django.db.models import Count, Q, QuerySet
from django.utils import timezone

from z_auth.models import User

from .kinds import FORM_KINDS, label_for
from .models import FormSubmission

STAFF_LIST_LIMIT = 500


def record_submission(
    kind: str,
    *,
    name: str = "",
    email: str = "",
    company: str = "",
    phone: str = "",
    subject: str = "",
    message: str = "",
    details: dict[str, Any] | None = None,
    ip: str | None = None,
    user_agent: str = "",
) -> FormSubmission:
    """Store one submitted form for the team (shows up in admin -> Forms as New)."""
    return FormSubmission.objects.create(
        kind=kind,
        name=(name or "").strip()[:120],
        email=(email or "").strip().lower()[:254],
        company=(company or "").strip()[:160],
        phone=(phone or "").strip()[:32],
        subject=(subject or "").strip()[:160],
        message=(message or "").strip(),
        details=details or {},
        ip=ip or None,
        user_agent=(user_agent or "")[:255],
    )


def kinds_with_counts() -> list[dict[str, Any]]:
    """Every form type with its totals: the known ones first (even when empty), then any other kind stored."""
    counts = {
        row["kind"]: row
        for row in FormSubmission.objects.values("kind").annotate(
            total=Count("id"), new=Count("id", filter=Q(status=FormSubmission.Status.NEW))
        )
    }
    ordered = list(FORM_KINDS) + sorted(k for k in counts if k not in FORM_KINDS)
    return [
        {
            "kind": kind,
            "label": label_for(kind),
            "total": counts.get(kind, {}).get("total", 0),
            "new": counts.get(kind, {}).get("new", 0),
        }
        for kind in ordered
    ]


def submissions_for_staff(kind: str | None = None, status: str | None = None, q: str = "") -> QuerySet:
    rows = FormSubmission.objects.select_related("handled_by")
    if kind:
        rows = rows.filter(kind=kind)
    if status:
        rows = rows.filter(status=status)
    q = (q or "").strip()
    if q:
        rows = rows.filter(
            Q(name__icontains=q) | Q(email__icontains=q) | Q(company__icontains=q)
            | Q(phone__icontains=q) | Q(subject__icontains=q) | Q(message__icontains=q)
        )
    return rows


def status_counts(kind: str | None = None) -> dict[str, int]:
    rows = FormSubmission.objects.all()
    if kind:
        rows = rows.filter(kind=kind)
    return {s: rows.filter(status=s).count() for s in FormSubmission.Status.values}


def update_submission(sub: FormSubmission, staff: User, status: str | None = None, note: str | None = None) -> FormSubmission:
    """Mark handled / reopen, and/or save the internal note."""
    fields: list[str] = []
    if status is not None and status != sub.status:
        sub.status = status
        handled = status == FormSubmission.Status.HANDLED
        sub.handled_by = staff if handled else None
        sub.handled_at = timezone.now() if handled else None
        fields += ["status", "handled_by", "handled_at"]
    if note is not None:
        sub.note = note.strip()
        fields.append("note")
    if fields:
        sub.save(update_fields=fields)
    return sub


def new_count() -> int:
    """For the sidebar badge."""
    return FormSubmission.objects.filter(status=FormSubmission.Status.NEW).count()
