"""
What an owner's plan switches on, and the one place that decides it.

A plan lists its `modules` (seeded by migration 0015, editable in admin). The owner's
entitlements come from their *active* subscription; with none (never subscribed, a paid plan
still waiting for its first payment, expired, cancelled) they fall back to the Free forever
plan. `max_devices` 0 means no PC limit.

Enforced in z_devices (see the "Plan enforcement" notes there): the PC limit at enrolment,
the effective policy sent to the agent, policy edits, uploaded and read data, and the chat /
work-log / alert-email endpoints. The dashboard reads the same answer from GET me/.
"""
from __future__ import annotations

import zlib
from dataclasses import dataclass
from typing import Any

from django.apps import apps
from django.db.models import Q
from rest_framework.exceptions import PermissionDenied

from .models import Plan, Subscription, User

USB_ALERTS = "usb_alerts"
USB_CONTROL = "usb_control"
DATA_TRANSFER = "data_transfer"
REMOTE_ACCESS = "remote_access"
SOFTWARE_INSTALLS = "software_installs"
WEB = "web"
WORK_TIME = "work_time"
APP_USAGE = "app_usage"
TEAM_CHAT = "team_chat"
ALERT_TO_EMPLOYEE = "alert_to_employee"

# Owner-language names, used in "… is on the Startups plan" messages.
MODULE_LABELS: dict[str, str] = {
    USB_ALERTS: "USB drive and file-copy alerts",
    USB_CONTROL: "Blocking USB drives",
    DATA_TRANSFER: "Alerts for files leaving by network folder, Outlook or upload",
    REMOTE_ACCESS: "Spotting and blocking remote-access tools",
    SOFTWARE_INSTALLS: "New software alerts",
    WEB: "Website protection",
    WORK_TIME: "Work hours and breaks",
    APP_USAGE: "App usage",
    TEAM_CHAT: "Chat with your team",
    ALERT_TO_EMPLOYEE: "Emailing an alert to the employee",
}
MODULES: tuple[str, ...] = tuple(MODULE_LABELS)

# Used only when the Free forever row is missing (it never should be).
FALLBACK_MODULES = frozenset({USB_ALERTS})
FALLBACK_MAX_DEVICES = 3

# Which module an agent event type / usage-metric category belongs to. Exact names first,
# then prefixes. Anything unlisted (e.g. policy.updated, usb.policy_applied) is always kept.
_EXACT: dict[str, str] = {
    "usb.connected": USB_ALERTS,
    "data.usb_copy": USB_ALERTS,
    "transfer.usb": USB_ALERTS,
    "usb.blocked": USB_CONTROL,
    "data.usb_locked_down": USB_CONTROL,
    "transfer.lan": DATA_TRANSFER,
    "transfer.email": DATA_TRANSFER,
    "transfer.other": DATA_TRANSFER,
    "data.share_copy": DATA_TRANSFER,
    "data.email_attachment": DATA_TRANSFER,
    "net.upload": DATA_TRANSFER,
    "net.upload_over_limit": DATA_TRANSFER,
    "software.installed": SOFTWARE_INSTALLS,
    "app.foreground": APP_USAGE,
}
_PREFIXES: tuple[tuple[str, str], ...] = (
    ("remote.", REMOTE_ACCESS),
    ("software.", SOFTWARE_INSTALLS),
    ("web.", WEB),
    ("net.", DATA_TRANSFER),
)


def module_for(kind: str) -> str | None:
    """The module an event type or metric category belongs to (None = not plan-gated)."""
    if kind in _EXACT:
        return _EXACT[kind]
    for prefix, module in _PREFIXES:
        if kind.startswith(prefix):
            return module
    return None


@dataclass(frozen=True)
class Entitlements:
    plan: Plan | None
    modules: frozenset[str]
    max_devices: int
    # False when this is the Free fallback rather than the owner's own active plan.
    on_active_plan: bool

    def has(self, module: str) -> bool:
        return module in self.modules

    def allows(self, kind: str) -> bool:
        """Keep this event type / metric category? (Ungated kinds are always kept.)"""
        module = module_for(kind)
        return module is None or module in self.modules

    @property
    def fingerprint(self) -> int:
        """0..999, changes when the module set does (folded into the agent's policy version)."""
        return zlib.crc32(",".join(sorted(self.modules)).encode()) % 1000


def locked_kinds_q(field: str, ent: "Entitlements") -> Q:
    """A Q matching rows whose `field` (event type / metric category) belongs to a module `ent` lacks."""
    locked = set(MODULES) - ent.modules
    query = Q(pk__in=[])
    for kind, module in _EXACT.items():
        if module in locked:
            query |= Q(**{field: kind})
    for prefix, module in _PREFIXES:
        if module in locked:
            query |= Q(**{f"{field}__startswith": prefix})
    return query


def _free_plan() -> Plan | None:
    return Plan.objects.filter(code=Plan.FREE).first()


def _from_plan(plan: Plan | None, on_active_plan: bool) -> Entitlements:
    if plan is None:
        return Entitlements(None, FALLBACK_MODULES, FALLBACK_MAX_DEVICES, False)
    modules = frozenset(m for m in (plan.modules or []) if m in MODULE_LABELS)
    return Entitlements(plan, modules, plan.max_devices or 0, on_active_plan)


def entitlements_for(user: User) -> Entitlements:
    """What `user`'s plan switches on right now."""
    from .billing import current_subscription  # billing imports services; keep this module light

    subscription = current_subscription(user)
    if subscription is not None and subscription.status == Subscription.Status.ACTIVE:
        return _from_plan(subscription.plan, on_active_plan=True)
    return _from_plan(_free_plan(), on_active_plan=False)


def devices_used(user: User) -> int:
    """PCs counted against the plan's limit: the owner's enrolled, not-removed devices."""
    Device = apps.get_model("z_devices", "Device")
    return Device.objects.filter(owner=user, is_revoked=False).count()


def cheapest_plan_with(module: str) -> Plan | None:
    """The lowest-priced active plan that includes `module` (for "upgrade to …")."""
    for plan in Plan.objects.filter(is_active=True).order_by("price_inr_monthly", "sort_order"):
        if module in (plan.modules or []):
            return plan
    return None


class FeatureLocked(PermissionDenied):
    """
    The owner's plan doesn't include `module`. A DRF 403, so a view or service can just raise it:
    body = {detail, code: "plan_feature", module, upgrade_plan, upgrade_plan_name}.
    """

    def __init__(self, module: str) -> None:
        self.module = module
        self.upgrade_plan = cheapest_plan_with(module)
        label = MODULE_LABELS.get(module, "This feature")
        if self.upgrade_plan is not None:
            message = f"{label} is on the {self.upgrade_plan.name} plan. Upgrade to switch it on."
        else:
            message = f"{label} isn't on your plan."
        self.message = message
        super().__init__(detail=self.payload())

    def __str__(self) -> str:
        return self.message

    def payload(self) -> dict[str, Any]:
        return {
            "detail": self.message,
            "code": "plan_feature",
            "module": self.module,
            # "" (not None) when no plan has it: DRF would turn None into the string "None".
            "upgrade_plan": self.upgrade_plan.code if self.upgrade_plan else "",
            "upgrade_plan_name": self.upgrade_plan.name if self.upgrade_plan else "",
        }


def require(user: User, module: str) -> Entitlements:
    """Raise FeatureLocked unless `user`'s plan includes `module`; returns the entitlements."""
    ent = entitlements_for(user)
    if not ent.has(module):
        raise FeatureLocked(module)
    return ent


def as_payload(user: User) -> dict[str, Any]:
    """The `entitlements` block of GET me/ (the dashboard locks sections from it)."""
    ent = entitlements_for(user)
    # For each locked module, the cheapest plan that has it ({code, name}) for "Upgrade to …".
    upgrade_for: dict[str, dict[str, str]] = {}
    for module in MODULES:
        if module not in ent.modules:
            plan = cheapest_plan_with(module)
            if plan is not None:
                upgrade_for[module] = {"code": plan.code, "name": plan.name}
    return {
        "plan_code": ent.plan.code if ent.plan else None,
        "plan_name": ent.plan.name if ent.plan else None,
        "on_active_plan": ent.on_active_plan,
        "modules": sorted(ent.modules),
        "max_devices": ent.max_devices,
        "devices_used": devices_used(user),
        "upgrade_for": upgrade_for,
    }
