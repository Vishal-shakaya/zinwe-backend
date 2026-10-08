"""
Plain-English titles for agent events, shown in the owner's activity feed.

Titles are built at read time from (type, data, hostname), so wording can be
improved later without touching stored rows. `data` comes from the PC and is
untrusted: only string values are used, whitespace is collapsed and long values
are cut short. Unknown types get a generic sentence instead of failing.
"""
from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from typing import Any

# Event types the agent emits today (see window-app/docs/architecture.md).
USB_CONNECTED = "usb.connected"
USB_BLOCKED = "usb.blocked"
USB_POLICY_APPLIED = "usb.policy_applied"

# Same wording as the dashboard's rule picker.
USB_MODE_LABELS = {"allow": "Allow", "read_only": "Read-only", "block": "Block"}

EVENT_TYPE_PATTERN = r"^[a-z_]+(\.[a-z_]+)+$"

_MAX_VALUE_CHARS = 80
_WHITESPACE = re.compile(r"\s+")


def _text(data: Mapping[str, Any], key: str) -> str:
    """A tidy, bounded string from `data[key]`, or "" when missing/not a string."""
    value = data.get(key)
    if not isinstance(value, str):
        return ""
    value = _WHITESPACE.sub(" ", value).strip()
    if len(value) > _MAX_VALUE_CHARS:
        value = value[: _MAX_VALUE_CHARS - 1].rstrip() + "…"
    return value


def _usb_connected(data: Mapping[str, Any], host: str) -> str:
    name = _text(data, "device_name")
    return f'USB drive "{name}" plugged into {host}' if name else f"A USB drive was plugged into {host}"


def _usb_blocked(data: Mapping[str, Any], host: str) -> str:
    name = _text(data, "device_name")
    return f'Blocked USB drive "{name}" on {host}' if name else f"Blocked a USB drive on {host}"


def _usb_policy_applied(data: Mapping[str, Any], host: str) -> str:
    label = USB_MODE_LABELS.get(_text(data, "mode"))
    title = f'USB rule "{label}" is now active on {host}' if label else f"A new USB rule is now active on {host}"
    if data.get("dry_run") is True:
        title += " (test mode)"
    return title


_BUILDERS: dict[str, Callable[[Mapping[str, Any], str], str]] = {
    USB_CONNECTED: _usb_connected,
    USB_BLOCKED: _usb_blocked,
    USB_POLICY_APPLIED: _usb_policy_applied,
}


def build_title(event_type: str, data: Any, hostname: str) -> str:
    """One sentence describing the event, e.g. 'USB drive "SanDisk Ultra" plugged into ACCOUNTS-PC-01'."""
    host = _WHITESPACE.sub(" ", hostname or "").strip() or "a PC"
    payload: Mapping[str, Any] = data if isinstance(data, Mapping) else {}
    builder = _BUILDERS.get(event_type)
    if builder is not None:
        return builder(payload, host)
    readable = (event_type or "").replace(".", " ").replace("_", " ").strip() or "unknown"
    return f'{host} reported a "{readable}" event'
