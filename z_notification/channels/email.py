"""Email channel backed by Django's configured EMAIL_BACKEND."""
from __future__ import annotations

from django.conf import settings
from django.core.mail import EmailMultiAlternatives

from .base import Channel


class EmailChannel(Channel):
    """Sends a plain-text email, with an optional HTML alternative.

    No attachments or inline images: an inline (cid) logo made Gmail accept and then silently
    drop messages (2026-10-11). The logo is text, or a hosted image (ZINWE_EMAIL_LOGO_URL).
    """

    name = "email"

    def send(
        self, recipient: str, subject: str, body: str, html: str | None = None, headers: dict[str, str] | None = None
    ) -> None:
        message = EmailMultiAlternatives(
            subject=subject,
            body=body,
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=[recipient],
            headers=headers or None,
        )
        if html:
            message.attach_alternative(html, "text/html")
        message.send(fail_silently=False)
