"""
The one entry point for sending notifications: `notify()`.

Templates live at `z_notification/templates/z_notification/<template>/`:
  subject.txt  (required, rendered and collapsed to one line)
  body.txt     (required, plain-text body)
  body.html    (optional, HTML alternative)
"""
from __future__ import annotations

import logging
from typing import Any

from django.template import TemplateDoesNotExist
from django.template.loader import render_to_string
from django.utils import timezone

from z_main.refs import Ref, resolve_ref

from .channels import get_channel
from .models import Notification

logger = logging.getLogger("zinwe.notification")


def render_template(template: str, context: dict[str, Any]) -> tuple[str, str, str | None]:
    """Render (subject, text body, html body or None) for a template folder."""
    base = f"z_notification/{template}"
    subject = render_to_string(f"{base}/subject.txt", context)
    subject = " ".join(subject.split())[:255]
    body = render_to_string(f"{base}/body.txt", context).strip() + "\n"
    try:
        html: str | None = render_to_string(f"{base}/body.html", context)
    except TemplateDoesNotExist:
        html = None
    return subject, body, html


def notify(
    channel: str,
    recipient: str,
    template: str,
    context: dict[str, Any] | None = None,
    ref: Ref = None,
) -> Notification | None:
    """
    Render, log and send one notification. Never raises.

    Returns the `Notification` row (status sent/failed), or None if even the
    log row could not be written.
    """
    context = context or {}
    ref_type, ref_id = resolve_ref(ref)
    notification: Notification | None = None
    try:
        notification = Notification.objects.create(
            channel=channel,
            recipient=recipient,
            template=template,
            ref_type=ref_type,
            ref_id=ref_id,
        )
        subject, body, html = render_template(template, context)
        notification.subject = subject
        notification.body = body
        get_channel(channel).send(recipient, subject, body, html=html)
        notification.status = Notification.Status.SENT
        notification.sent_at = timezone.now()
        notification.save(update_fields=["subject", "body", "status", "sent_at"])
    except Exception as exc:  # noqa: BLE001 -- notify must never break the caller
        logger.exception("Notification %r to %s via %s failed", template, recipient, channel)
        if notification is not None:
            try:
                notification.status = Notification.Status.FAILED
                notification.error = f"{type(exc).__name__}: {exc}"
                notification.save(update_fields=["subject", "body", "status", "error"])
            except Exception:  # noqa: BLE001
                logger.exception("Could not record notification failure")
    return notification
