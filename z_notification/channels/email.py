"""Email channel backed by Django's configured EMAIL_BACKEND."""
from __future__ import annotations

from django.conf import settings
from django.core.mail import EmailMultiAlternatives

from .base import Channel


class EmailChannel(Channel):
    """Sends a plain-text email, with an optional HTML alternative."""

    name = "email"

    def send(self, recipient: str, subject: str, body: str, html: str | None = None) -> None:
        message = EmailMultiAlternatives(
            subject=subject,
            body=body,
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=[recipient],
        )
        if html:
            message.attach_alternative(html, "text/html")
        message.send(fail_silently=False)
