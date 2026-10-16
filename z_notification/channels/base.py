"""Abstract notification channel."""
from __future__ import annotations

from abc import ABC, abstractmethod

# (file name, content, MIME type), e.g. ("Zinwe-quote.pdf", b"%PDF...", "application/pdf").
Attachment = tuple[str, bytes, str]


class ChannelError(Exception):
    """Raised by a channel (or the registry) when a message cannot be delivered."""


class Channel(ABC):
    """
    A delivery mechanism (email, SMS, WhatsApp, Slack, ...).

    Subclasses set `name` (the registry key) and implement `send`. `send` should
    raise on failure; the `notify` service catches, logs and records the error.
    """

    name: str = ""

    @abstractmethod
    def send(self, recipient: str, subject: str, body: str, html: str | None = None) -> None:
        """Deliver one message to one recipient. Raise on failure."""
        raise NotImplementedError
