"""
Read models + actions for the Zinwe team's internal admin app (M-ADMIN).

This is a thin layer over z_auth: it reuses the billing engine for payment
review and plan grants, and never duplicates its rules. Everything here is
staff-only (the views enforce IsAdminUser).

Billing cycles: every *verified* payment is one paid cycle (one month of a
paid plan). A customer who pays again (renewal, or a new plan after an
expiry) gets cycle 2, 3, ... -- numbered by when the payment was approved.
"""
from __future__ import annotations

import html as html_lib
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.db.models import Avg, Count, Max, Sum
from django.db.models.functions import TruncDate, TruncMonth
from django.utils import timezone
from rest_framework.authtoken.models import Token

import nh3

from z_auth import billing, pricing
from z_analytics.services import track
from z_auth.models import (
    Company,
    CustomerRemark,
    Feedback,
    ManualPayment,
    Plan,
    Subscription,
    User,
)
from z_auth.services import activate_plan, starts_as_trial
from z_devices.models import Device, Member
from z_notification.models import Notification
from z_notification.services import notify

Status = Subscription.Status
PaymentStatus = ManualPayment.Status


class AdminActionError(Exception):
    """A refused admin action; str(exc) is shown to the operator."""


# --- Billing cycles -----------------------------------------------------------


def _owner_payments(owner_id: int) -> list[ManualPayment]:
    """Every payment the owner has reported, across all their subscriptions, oldest first."""
    return list(
        ManualPayment.objects.filter(subscription__owner_id=owner_id)
        .select_related("subscription__plan", "reviewed_by")
        .order_by("submitted_at", "id")
    )


def cycle_numbers(payments: list[ManualPayment]) -> dict[int, int]:
    """
    payment id -> its billing cycle. Verified payments are numbered 1..n in approval order;
    a payment still being checked gets the next number (the cycle it would start).
    Rejected payments have no cycle.
    """
    verified = sorted(
        (p for p in payments if p.status == PaymentStatus.VERIFIED),
        key=lambda p: (p.reviewed_at or p.submitted_at, p.id),
    )
    cycles = {p.id: n for n, p in enumerate(verified, start=1)}
    upcoming = len(verified) + 1
    for p in payments:
        if p.status == PaymentStatus.SUBMITTED:
            cycles[p.id] = upcoming
    return cycles


# --- Subscribers ------------------------------------------------------------


@dataclass
class SubscriberView:
    """A customer (account owner): current plan, latest payment, and the full payment history."""

    user: User
    company: Company | None
    subscription: Subscription | None
    payment: ManualPayment | None
    history: list[ManualPayment] = field(default_factory=list)  # newest first
    remark: CustomerRemark | None = None
    emails_sent: int = 0  # custom emails from the team (admin_message), sent successfully
    members: list[Member] = field(default_factory=list)  # team the owner added (each with .device_count)
    device_count: int = 0  # PCs enrolled on the account
    last_email_at: datetime | None = None
    cycles: dict[int, int] = field(default_factory=dict)  # payment id -> cycle number

    @property
    def cycles_paid(self) -> int:
        return sum(1 for p in self.history if p.status == PaymentStatus.VERIFIED)

    @property
    def total_paid_inr(self) -> Decimal:
        return sum(
            (p.amount_inr for p in self.history if p.status == PaymentStatus.VERIFIED), Decimal("0")
        )


def _email_stats(emails: list[str]) -> dict[str, tuple[int, datetime | None]]:
    """email -> (custom emails sent, when the last went), in one query for the whole table."""
    rows = (
        Notification.objects.filter(
            template=ADMIN_MESSAGE_TEMPLATE, status=Notification.Status.SENT, recipient__in=emails
        )
        .values("recipient")
        .annotate(n=Count("id"), last=Max("sent_at"))
    )
    return {r["recipient"]: (r["n"], r["last"]) for r in rows}


@dataclass
class TeamStats:
    members: dict[int, list[Member]]
    devices: dict[int, int]


def _team_stats(owner_ids: list[int]) -> TeamStats:
    """Members (with their PC counts) and enrolled devices per owner, in two queries for the whole table."""
    members: dict[int, list[Member]] = defaultdict(list)
    rows = (
        Member.objects.filter(owner_id__in=owner_ids)
        .annotate(device_count=Count("devices"))
        .order_by("name", "id")
    )
    for m in rows:
        members[m.owner_id].append(m)
    devices = dict(
        Device.objects.filter(owner_id__in=owner_ids).values_list("owner_id").annotate(n=Count("id")).order_by()
    )
    return TeamStats(members=members, devices=devices)


def _subscriber_view(
    user: User,
    email_stats: dict[str, tuple[int, datetime | None]] | None = None,
    team: TeamStats | None = None,
) -> SubscriberView:
    subscription = billing.current_subscription(user)
    payment = billing.latest_payment(subscription) if subscription else None
    company = getattr(user, "owned_company", None)
    payments = _owner_payments(user.pk)
    view = SubscriberView(
        user=user, company=company, subscription=subscription, payment=payment,
        history=list(reversed(payments)), cycles=cycle_numbers(payments),
        remark=CustomerRemark.objects.filter(owner=user).select_related("updated_by").first(),
    )
    stats = (email_stats if email_stats is not None else _email_stats([user.email])).get(user.email)
    if stats:
        view.emails_sent, view.last_email_at = stats
    team = team if team is not None else _team_stats([user.pk])
    view.members = team.members.get(user.pk, [])
    view.device_count = team.devices.get(user.pk, 0)
    return view


def subscribers() -> list[SubscriberView]:
    """Every account owner (non-staff), newest first, with plan + payments resolved."""
    users = (
        User.objects.filter(is_staff=False)
        .select_related("owned_company")
        .order_by("-date_joined")
    )
    users = list(users)
    stats = _email_stats([u.email for u in users])
    team = _team_stats([u.pk for u in users])
    return [_subscriber_view(user, stats, team) for user in users]


def subscriber(user_id: int) -> SubscriberView:
    user = User.objects.filter(is_staff=False).select_related("owned_company").get(pk=user_id)
    return _subscriber_view(user)


def _latest_submitted_payment(user: User) -> ManualPayment | None:
    subscription = billing.current_subscription(user)
    if subscription is None:
        return None
    return subscription.payments.filter(status=PaymentStatus.SUBMITTED).order_by("-submitted_at").first()


def approve_payment(user: User, reviewer: User) -> SubscriberView:
    """Verify the owner's latest submitted payment (activates the subscription for a month)."""
    payment = _latest_submitted_payment(user)
    if payment is None:
        raise AdminActionError("This customer has no payment waiting to be checked.")
    review_payment(payment.pk, reviewer, approve=True)
    return subscriber(user.pk)


def reject_payment(user: User, reviewer: User, reason: str) -> SubscriberView:
    """Reject the owner's latest submitted payment with a reason (they can resubmit)."""
    payment = _latest_submitted_payment(user)
    if payment is None:
        raise AdminActionError("This customer has no payment waiting to be checked.")
    review_payment(payment.pk, reviewer, approve=False, reason=reason)
    return subscriber(user.pk)


def set_blocked(user: User, blocked: bool) -> SubscriberView:
    """Block (deactivate) or unblock a customer account. A blocked owner can't sign in."""
    if user.is_staff:
        raise AdminActionError("You can't block a staff account here.")
    user.is_active = not blocked
    user.save(update_fields=["is_active"])
    if blocked:
        Token.objects.filter(user=user).delete()  # sign them out everywhere now
    return subscriber(user.pk)


def set_password(user: User, password: str) -> SubscriberView:
    """
    Give the customer a new password (passwords are stored hashed, so they can't be shown).
    Uses the same strength rules as sign-up, and signs them out of every session.
    """
    if user.is_staff:
        raise AdminActionError("You can't change a staff password here.")
    try:
        validate_password(password, user)
    except DjangoValidationError as exc:
        raise AdminActionError(" ".join(exc.messages)) from exc
    user.set_password(password)
    user.save(update_fields=["password"])
    Token.objects.filter(user=user).delete()
    return subscriber(user.pk)


@transaction.atomic
def delete_customer(user: User) -> None:
    """
    Delete a customer account and everything that hangs off it (company, subscriptions,
    payments, devices, feedback -- all cascade), plus their uploaded payment proofs.
    """
    if user.is_staff:
        raise AdminActionError("You can't delete a staff account here.")
    proofs = [p.proof for p in ManualPayment.objects.filter(subscription__owner=user) if p.proof]
    user.delete()

    def remove_files() -> None:
        for proof in proofs:
            try:
                proof.storage.delete(proof.name)
            except OSError:
                pass  # a missing file is fine; the rows are gone either way

    transaction.on_commit(remove_files)


@transaction.atomic
def change_plan(user: User, plan_code: str) -> SubscriberView:
    """
    Grant the customer a plan immediately (admin override).

    Any current pending/active subscription is cancelled first; the new one is
    made active right away (a free/trial plan as a trial, a paid plan as a
    one-month admin grant). Reuses the billing engine's activation rules.
    """
    try:
        plan = Plan.objects.get(code=plan_code, is_active=True)
    except Plan.DoesNotExist as exc:
        raise AdminActionError(f"Unknown plan '{plan_code}'.") from exc

    company = Company.objects.filter(owner=user).first()
    Subscription.objects.filter(
        owner=user, status__in=[Status.PENDING_PAYMENT, Status.ACTIVE]
    ).exclude(status=Status.CANCELLED).update(status=Status.CANCELLED)

    now = timezone.now()
    is_trial = starts_as_trial(user, plan)
    # Same money rules as a self-serve subscription: the company's country sets currency + cycle.
    due = pricing.quote(plan, company)
    money = {"currency": due.currency, "billing_cycle": due.billing_cycle}
    if plan.requires_payment and not is_trial:
        ends_at = billing.add_months(now, pricing.months_in(due.billing_cycle))
        activation = activate_plan(user, plan, source="admin", allow_paid=True, ends_at=ends_at)
        Subscription.objects.create(
            owner=user, company=company, plan=plan, status=Status.ACTIVE,
            amount_inr=due.amount, starts_at=now, ends_at=ends_at,
            activation=activation, created_at=now, **money,
        )
    else:
        activation = activate_plan(
            user, plan, source="admin", trial=is_trial,
        )
        Subscription.objects.create(
            owner=user, company=company, plan=plan, status=Status.ACTIVE,
            amount_inr=Decimal("0") if is_trial else due.amount, **money,
            is_trial=is_trial, starts_at=activation.starts_at, ends_at=activation.ends_at,
            activation=activation, created_at=now,
        )
    return _subscriber_view(User.objects.select_related("owned_company").get(pk=user.pk))


# --- Remark + custom email ------------------------------------------------------


def set_remark(user: User, text: str, staff: User) -> SubscriberView:
    """Save the team's internal note on a customer (empty text clears it). Never shown to the customer."""
    text = (text or "").strip()
    if text:
        CustomerRemark.objects.update_or_create(owner=user, defaults={"text": text, "updated_by": staff})
    else:
        CustomerRemark.objects.filter(owner=user).delete()
    return subscriber(user.pk)


ADMIN_MESSAGE_TEMPLATE = "admin_message"

# What the email editor can produce (Quill's semantic HTML), and nothing else: no scripts,
# styles, images, iframes or event handlers. Links only to http(s)/mailto.
EMAIL_TAGS = {"p", "br", "strong", "b", "em", "i", "u", "s", "a", "ul", "ol", "li", "h1", "h2", "h3", "blockquote", "code", "pre"}
EMAIL_ATTRIBUTES = {"a": {"href"}}


def clean_email_html(raw: str) -> str:
    """Allow-list clean of staff-written HTML before it goes into an email."""
    return nh3.clean(
        raw or "", tags=EMAIL_TAGS, attributes=EMAIL_ATTRIBUTES,
        url_schemes={"http", "https", "mailto"}, link_rel="noopener noreferrer",
    ).replace("&nbsp;", " ").replace("\u00a0", " ").strip()  # non-breaking spaces stop text wrapping


def html_to_text(html: str) -> str:
    """Plain-text alternative for the email (line breaks kept, bullets as '- ')."""
    text = re.sub(r"(?i)<br\s*/?>", "\n", html)
    text = re.sub(r"(?i)<li[^>]*>", "- ", text)
    text = re.sub(r"(?i)</(p|li|h[1-3]|blockquote|pre)>", "\n", text)
    text = re.sub(r"(?i)<a [^>]*href=\"([^\"]+)\"[^>]*>(.*?)</a>", r"\2 (\1)", text)
    text = html_lib.unescape(re.sub(r"<[^>]+>", "", text))
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def send_customer_email(user: User, subject: str, raw_html: str, staff: User) -> Notification:
    """
    Send the customer a custom email from the team (subject + rich-text body in the branded
    layout). Logged as a Notification; raises AdminActionError if it is empty or fails to send.
    """
    subject = " ".join((subject or "").split())
    if not subject:
        raise AdminActionError("Add a subject.")
    html = clean_email_html(raw_html)
    text = html_to_text(html)
    if not text:
        raise AdminActionError("Write a message first.")
    first_name = (user.full_name or "").split(" ")[0]
    notification = notify(
        "email", user.email, ADMIN_MESSAGE_TEMPLATE,
        {"subject": subject, "html_body": html, "text_body": text, "first_name": first_name},
        ref=user,
    )
    if notification is None or notification.status != Notification.Status.SENT:
        reason = notification.error if notification is not None else "it couldn't be logged"
        raise AdminActionError(f"The email couldn't be sent ({reason}).")
    track("admin.email_sent", {"subject": subject, "to": user.email}, actor_email=staff.email, ref=user)
    return notification


# Automatic emails worth showing next to the team's own (renewal reminders, expiry notices).
AUTOMATIC_TEMPLATES = ("renewal_reminder", "plan_expired")


def customer_emails(user: User, limit: int = 20) -> list[Notification]:
    """Emails to this customer the team should see: their custom ones + automatic renewal ones, newest first."""
    return list(
        Notification.objects.filter(
            recipient=user.email, template__in=(ADMIN_MESSAGE_TEMPLATE, *AUTOMATIC_TEMPLATES)
        ).order_by("-created_at", "-id")[:limit]
    )


# --- Plan activation: the payment review queue -------------------------------


@dataclass
class PaymentView:
    """One reported payment with its customer, plan and billing cycle (for the review queue)."""

    payment: ManualPayment
    cycle: int | None


def payments(status: str | None = None) -> list[PaymentView]:
    """Reported payments, newest first; `status` = submitted / verified / rejected (or all)."""
    rows = ManualPayment.objects.select_related(
        "subscription__owner__owned_company", "subscription__plan", "reviewed_by"
    ).order_by("-submitted_at", "-id")
    if status:
        rows = rows.filter(status=status)
    rows = list(rows)
    cycles: dict[int, int] = {}
    for owner_id in {p.subscription.owner_id for p in rows}:
        cycles.update(cycle_numbers(_owner_payments(owner_id)))
    return [PaymentView(payment=p, cycle=cycles.get(p.id)) for p in rows]


def payment_view(payment_id: int) -> PaymentView:
    payment = ManualPayment.objects.select_related(
        "subscription__owner__owned_company", "subscription__plan", "reviewed_by"
    ).get(pk=payment_id)
    cycles = cycle_numbers(_owner_payments(payment.subscription.owner_id))
    return PaymentView(payment=payment, cycle=cycles.get(payment.id))


def review_payment(payment_id: int, reviewer: User, *, approve: bool, reason: str = "") -> PaymentView:
    """
    Approve (the plan activates for a month, owner emailed) or reject (with a reason the
    owner sees; they can resubmit) one submitted payment. Nothing activates without approval.
    """
    try:
        if approve:
            billing.verify_payment(payment_id, reviewer)
        else:
            billing.reject_payment(payment_id, reviewer, reason)
    except billing.ReviewNotAllowed as exc:
        raise AdminActionError(str(exc)) from exc
    return payment_view(payment_id)


# --- Feedback -------------------------------------------------------------------


def feedback_rows() -> list[Feedback]:
    return list(Feedback.objects.select_related("owner__owned_company").all())


def feedback_summary() -> dict[str, Any]:
    """Count, average and a 1..10 histogram of every rating."""
    agg = Feedback.objects.aggregate(count=Count("id"), average=Avg("rating"))
    by_rating = dict(Feedback.objects.values_list("rating").annotate(n=Count("id")))
    return {
        "count": agg["count"] or 0,
        "average": round(float(agg["average"]), 1) if agg["average"] is not None else None,
        "distribution": [
            {"rating": r, "count": int(by_rating.get(r, 0))}
            for r in range(Feedback.RATING_MIN, Feedback.RATING_MAX + 1)
        ],
    }


# --- Dashboard metrics ------------------------------------------------------


def dashboard_metrics() -> dict[str, Any]:
    """Headline numbers + simple time series for the admin dashboard charts."""
    owners = User.objects.filter(is_staff=False)
    total_users = owners.count()
    verified_users = owners.filter(email_verified_at__isnull=False).count()
    active_subs = Subscription.objects.filter(status=Status.ACTIVE).count()
    trials = Subscription.objects.filter(status=Status.ACTIVE, is_trial=True).count()
    paying = Subscription.objects.filter(
        status=Status.ACTIVE, is_trial=False, amount_inr__gt=0
    ).count()
    pending_payments = ManualPayment.objects.filter(status=PaymentStatus.SUBMITTED).count()
    revenue = ManualPayment.objects.filter(status=PaymentStatus.VERIFIED).aggregate(
        total=Sum("amount_inr")
    )["total"] or Decimal("0")
    feedback = feedback_summary()

    return {
        "totals": {
            "users": total_users,
            "verified_users": verified_users,
            "active_subscriptions": active_subs,
            "trials": trials,
            "paying": paying,
            "pending_payments": pending_payments,
            "revenue_inr": str(revenue),
            "feedback": {"count": feedback["count"], "average": feedback["average"]},
        },
        "signups": _signups_series(days=30),
        "revenue": _revenue_series(months=6),
    }


def _signups_series(days: int) -> list[dict[str, Any]]:
    """New account sign-ups per day for the last `days` days (zero-filled)."""
    since = timezone.now() - timedelta(days=days - 1)
    start_day = since.date()
    rows = (
        User.objects.filter(is_staff=False, date_joined__date__gte=start_day)
        .annotate(day=TruncDate("date_joined"))
        .values("day")
        .annotate(count=Count("id"))
        .order_by("day")
    )
    by_day = {row["day"]: row["count"] for row in rows}
    out = []
    for offset in range(days):
        day = start_day + timedelta(days=offset)
        out.append({"date": day.isoformat(), "count": int(by_day.get(day, 0))})
    return out


def _revenue_series(months: int) -> list[dict[str, Any]]:
    """Verified-payment revenue per calendar month for the last `months` months (zero-filled)."""
    now = timezone.now()
    rows = (
        ManualPayment.objects.filter(status=PaymentStatus.VERIFIED)
        .annotate(month=TruncMonth("reviewed_at"))
        .values("month")
        .annotate(total=Sum("amount_inr"))
    )
    by_month = {
        (row["month"].year, row["month"].month): row["total"]
        for row in rows
        if row["month"] is not None
    }
    out = []
    year, month = now.year, now.month
    buckets = []
    for _ in range(months):
        buckets.append((year, month))
        month -= 1
        if month == 0:
            month = 12
            year -= 1
    for year, month in reversed(buckets):
        total = by_month.get((year, month), Decimal("0"))
        out.append({"month": f"{year:04d}-{month:02d}", "revenue_inr": str(total or Decimal("0"))})
    return out
