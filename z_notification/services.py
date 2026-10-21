"""
The one entry point for sending notifications: `notify()`.

Templates live at `z_notification/templates/z_notification/<template>/`:
  subject.txt  (required, rendered and collapsed to one line)
  body.txt     (required, plain-text body)
  body.html    (optional, HTML alternative)
"""
from __future__ import annotations

import logging
from typing import Any, Iterable

from django.template import TemplateDoesNotExist
from django.conf import settings
from django.template.loader import render_to_string
from django.utils import timezone

from z_main.demo import is_demo_email
from z_main.refs import Ref, resolve_ref

from .channels import get_channel
from .channels.base import Attachment
from .models import Notification

logger = logging.getLogger("zinwe.notification")

REDACTED = "•" * 6  # "••••••"


def redact_text(text: str, secrets: Iterable[str] | None) -> str:
    """Replace every occurrence of each (non-empty) secret with `REDACTED`."""
    for secret in secrets or ():
        if secret:
            text = text.replace(secret, REDACTED)
    return text


def render_plain(name: str, context: dict[str, Any]) -> str:
    """
    Render a plain-text template (subject.txt, body.txt) with HTML autoescaping OFF, whatever the template
    says: "Kulkarni & Associates" must reach the inbox as "&", never "&amp;" (J1-12 / J5-08). Only body.html
    is escaped.
    """
    from django.template.loader import get_template

    return render_unescaped(get_template(name), context)


def render_unescaped(template: Any, context: dict[str, Any]) -> str:
    """Render a loaded template (backend wrapper) with autoescape off."""
    from django.template import Context

    inner = getattr(template, "template", None)  # the Django-engine Template behind the backend wrapper
    if inner is None:  # another template engine: fall back to the normal render
        return template.render(context)
    return inner.render(Context(context, autoescape=False))


def render_template(template: str, context: dict[str, Any]) -> tuple[str, str, str | None]:
    """Render (subject, text body, html body or None) for a template folder."""
    base = f"z_notification/{template}"
    # A hosted logo image once deployed; otherwise _brand.html draws the wordmark in text.
    context = {
        "brand_logo_url": getattr(settings, "ZINWE_EMAIL_LOGO_URL", ""),
        "app_url": getattr(settings, "ZINWE_APP_URL", ""),
        **context,
    }
    subject = render_plain(f"{base}/subject.txt", context)
    subject = " ".join(subject.split())[:255]
    body = render_plain(f"{base}/body.txt", context).strip() + "\n"
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
    redact: Iterable[str] | None = None,
    headers: dict[str, str] | None = None,
    attachments: list[Attachment] | None = None,
    reply_to: list[str] | None = None,
    fallback: bool = True,
) -> Notification | None:
    """
    Render, log and send one notification. Never raises.

    `redact`: strings (e.g. a one-time code) that are sent to the recipient but
    replaced with "••••••" in the logged subject and body.
    `headers`: extra message headers for channels that take them (email: e.g. List-Unsubscribe).
    `attachments`: files for channels that take them (email: e.g. the quote PDF); not logged.
    `reply_to`: addresses a reply goes to (email only), e.g. the visitor who wrote to the team.
    `fallback=False`: primary SMTP only, never the backup Gmail account (email only; bulk mail like the
    newsletter must not go out through it).

    Returns the `Notification` row (status sent/failed), or None if even the
    log row could not be written.
    """
    redact = [s for s in (redact or ()) if s]
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
        notification.subject = redact_text(subject, redact)
        notification.body = redact_text(body, redact)
        extra: dict[str, Any] = {"headers": headers} if headers else {}  # only channels that support it get the kwarg
        if attachments:
            extra["attachments"] = attachments
        if reply_to:
            extra["reply_to"] = [r for r in reply_to if r]
        if not fallback:
            extra["fallback"] = False
        if channel == "email" and is_demo_email(recipient):
            # A demo account's made-up address (z_main/demo.py): logged, never delivered. Callers see
            # "sent", so a demo's screens behave like the real thing.
            notification.error = "Not delivered: demo address (ZINWE_DEMO_EMAIL_DOMAINS)."
            notification.status = Notification.Status.SENT
            notification.sent_at = timezone.now()
            notification.save(update_fields=["subject", "body", "status", "sent_at", "error"])
            return notification
        get_channel(channel).send(recipient, subject, body, html=html, **extra)
        notification.status = Notification.Status.SENT
        notification.sent_at = timezone.now()
        notification.save(update_fields=["subject", "body", "status", "sent_at"])
    except Exception as exc:  # noqa: BLE001 -- notify must never break the caller
        logger.exception("Notification %r to %s via %s failed", template, recipient, channel)
        if notification is not None:
            try:
                notification.status = Notification.Status.FAILED
                notification.error = redact_text(f"{type(exc).__name__}: {exc}", redact)
                notification.save(update_fields=["subject", "body", "status", "error"])
            except Exception:  # noqa: BLE001
                logger.exception("Could not record notification failure")
    return notification
