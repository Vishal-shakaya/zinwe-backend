"""
Channel registry.

A channel is any object implementing `Channel` (see `base.py`). Register one with
`register_channel(MyChannel())` -- typically from an AppConfig.ready() -- and
`notify("<name>", ...)` will route to it. Email is registered by default.
"""
from __future__ import annotations

from .base import Channel, ChannelError
from .email import EmailChannel

_REGISTRY: dict[str, Channel] = {}


def register_channel(channel: Channel, *, replace: bool = False) -> Channel:
    """Add a channel to the registry under `channel.name`."""
    if not channel.name:
        raise ValueError("A channel needs a non-empty `name`.")
    if channel.name in _REGISTRY and not replace:
        raise ValueError(f'Channel "{channel.name}" is already registered.')
    _REGISTRY[channel.name] = channel
    return channel


def get_channel(name: str) -> Channel:
    """Return the registered channel or raise `ChannelError`."""
    try:
        return _REGISTRY[name]
    except KeyError as exc:
        raise ChannelError(f'Unknown notification channel "{name}".') from exc


def registered_channels() -> list[str]:
    """Names of all registered channels."""
    return sorted(_REGISTRY)


register_channel(EmailChannel())

__all__ = [
    "Channel",
    "ChannelError",
    "EmailChannel",
    "register_channel",
    "get_channel",
    "registered_channels",
]
