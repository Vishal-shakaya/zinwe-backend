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

from django.conf import settings
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.db.models import Avg, Count, Max, Q, Sum
from django.db.models.functions import TruncDate, TruncMonth
from django.utils import timezone
from rest_framework.authtoken.models import Token

import nh3

from z_analytics.models import Event
from z_analytics.services import track
from z_auth import billing, otp, pricing
from z_auth.models import (
    Company,
    CustomerRemark,
    Feedback,
    Invoice,
    ManualPayment,
    Plan,
    Subscription,
    User,
)
from z_auth.services import activate_plan, request_password_reset, starts_as_trial
from z_devices.models import Device, Member
from z_main.refs import resolve_ref
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
    emails_total: int = 0  # every email Zinwe sent them (sign-up codes, receipts, plan emails, the team's)
    blocked: Event | None = None  # the latest "blocked" record (who, when, why) while the account is blocked

    @property
    def cycles_paid(self) -> int:
        return sum(1 for p in self.history if p.status == PaymentStatus.VERIFIED)

    @property
    def total_paid_inr(self) -> Decimal:
        return sum(
            (p.amount_inr for p in self.history if p.status == PaymentStatus.VERIFIED), Decimal("0")
        )


def _email_stats(emails: list[str]) -> dict[str, tuple[int, datetime | None]]:
    """email -> (custom emails sent by staff, when the last went), in one query for the whole table."""
    rows = (
        Notification.objects.filter(
            template=ADMIN_MESSAGE_TEMPLATE, status=Notification.Status.SENT, recipient__in=emails
        )
        .values("recipient")
        .annotate(n=Count("id"), last=Max("sent_at"))
    )
    return {r["recipient"]: (r["n"], r["last"]) for r in rows}


def _email_totals(emails: list[str]) -> dict[str, int]:
    """email -> every email Zinwe sent them (automatic and the team's), in one query for the whole table."""
    rows = (
        Notification.objects.filter(channel="email", status=Notification.Status.SENT, recipient__in=emails)
        .values("recipient")
        .annotate(n=Count("id"))
    )
    return {r["recipient"]: r["n"] for r in rows}


BLOCKED_EVENT = "admin.customer_blocked"


def _blocked_record(user: User) -> Event | None:
    if user.is_active:
        return None
    ref_type, ref_id = resolve_ref(user)
    return Event.objects.filter(name=BLOCKED_EVENT, ref_type=ref_type, ref_id=ref_id).order_by("-created_at", "-id").first()


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
    email_totals: dict[str, int] | None = None,
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
    view.emails_total = (email_totals if email_totals is not None else _email_totals([user.email])).get(user.email, 0)
    view.blocked = _blocked_record(user)
    return view


def subscribers() -> list[SubscriberView]:
    """Every account owner (non-staff), newest first, with plan + payments resolved."""
    users = (
        User.objects.filter(is_staff=False)
        .select_related("owned_company")
        .order_by("-date_joined")
    )
    users = list(users)
    emails = [u.email for u in users]
    stats = _email_stats(emails)
    totals = _email_totals(emails)
    team = _team_stats([u.pk for u in users])
    return [_subscriber_view(user, stats, team, totals) for user in users]


def subscriber(user_id: int) -> SubscriberView:
    user = User.objects.filter(is_staff=False).select_related("owned_company").get(pk=user_id)
    return _subscriber_view(user)


def _latest_submitted_payment(user: User) -> ManualPayment | None:
    subscription = billing.current_subscription(user)
    if subscription is None:
        return None
    return subscription.payments.filter(status=PaymentStatus.SUBMITTED).order_by("-submitted_at").first()


def approve_payment(user: User, reviewer: User, reference: str, amount: Decimal) -> SubscriberView:
    """Verify the owner's latest submitted payment (activates the subscription for a month)."""
    payment = _latest_submitted_payment(user)
    if payment is None:
        raise AdminActionError("This customer has no payment waiting to be checked.")
    review_payment(payment.pk, reviewer, approve=True, reference=reference, amount=amount)
    return subscriber(user.pk)


def reject_payment(user: User, reviewer: User, reason: str) -> SubscriberView:
    """Reject the owner's latest submitted payment with a reason (they can resubmit)."""
    payment = _latest_submitted_payment(user)
    if payment is None:
        raise AdminActionError("This customer has no payment waiting to be checked.")
    review_payment(payment.pk, reviewer, approve=False, reason=reason)
    return subscriber(user.pk)


def _first_name(user: User) -> str:
    return (user.full_name or "").split(" ")[0] or "there"


def set_blocked(
    user: User, blocked: bool, staff: User | None = None, reason: str = "", email_customer: bool = False,
) -> SubscriberView:
    """
    Block (deactivate) or unblock a customer account. A blocked owner can't sign in (login says the
    account is paused). Blocking needs a reason; it's recorded (who, when, why) and, when
    `email_customer`, sent to the owner in the "account paused" email.
    """
    if user.is_staff:
        raise AdminActionError("You can't block a staff account here.")
    reason = " ".join((reason or "").split())
    if blocked and not reason:
        raise AdminActionError("Say why you're blocking this account.")
    user.is_active = not blocked
    user.save(update_fields=["is_active"])
    actor = staff.email if staff else ""
    if blocked:
        Token.objects.filter(user=user).delete()  # sign them out everywhere now
        track(BLOCKED_EVENT, {"reason": reason, "emailed": email_customer}, actor_email=actor, ref=user)
        if email_customer:
            notification = notify(
                "email", user.email, "account_suspended",
                {"first_name": _first_name(user), "reason": reason, "support_email": SUPPORT_EMAIL},
                ref=user,
            )
            if notification is None or notification.status != Notification.Status.SENT:
                raise AdminActionError(
                    "The account is blocked, but the email to the customer couldn't be sent. Write to them yourself."
                )
    else:
        track("admin.customer_unblocked", {}, actor_email=actor, ref=user)
        # Always tell them: a paused customer is waiting to hear they can sign in again.
        notify(
            "email", user.email, "account_restored",
            {"first_name": _first_name(user), "login_url": f"{settings.ZINWE_APP_URL}/login",
             "support_email": SUPPORT_EMAIL},
            ref=user,
        )
    return subscriber(user.pk)


SUPPORT_EMAIL = "hello@zinwe.io"


def send_password_reset(user: User, ip: str | None = None) -> None:
    """
    Email the customer a code to set a new password themselves: the same flow as "Forgot password" on
    the website (z_auth.services.request_password_reset). Nobody on the team ever learns the password.
    """
    if user.is_staff:
        raise AdminActionError("You can't reset a staff password here.")
    if user.is_pending_verification:
        raise AdminActionError(
            "They haven't finished signing up (the email isn't verified yet), so there's no password to reset."
        )
    if not user.is_active:
        raise AdminActionError("This account is blocked. Unblock it first, or the code won't be sent.")
    try:
        request_password_reset(user.email, ip)
    except otp.OtpThrottled as exc:
        raise AdminActionError(
            f"A code went to {user.email} moments ago. Ask them to check their inbox (and spam), "
            f"or try again in {max(1, exc.retry_after)} seconds."
        ) from exc


PASSWORD_SET_SUBJECT = "Your Zinwe password was changed"


def set_password(user: User, password: str, staff: User | None = None, email_customer: bool = True) -> SubscriberView:
    """
    Give the customer a new password (passwords are stored hashed, so they can't be shown).
    Uses the same strength rules as sign-up, and signs them out of every session. The customer is
    told by email (never with the password in it), so a change they didn't ask for doesn't go unnoticed.
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
    track("admin.password_set", {"emailed": email_customer}, actor_email=staff.email if staff else "", ref=user)
    if email_customer:
        reset_url = f"{settings.ZINWE_APP_URL}/forgot-password"
        html = (
            f"<p>Hi {html_lib.escape(_first_name(user))},</p>"
            "<p>A member of the Zinwe team set a new password on your account, as you asked. "
            "You were signed out everywhere; sign in again with the password they gave you.</p>"
            f'<p>Want to choose your own? Use <a href="{reset_url}">Forgot password</a> on the sign-in page '
            "at any time.</p>"
            f'<p>Didn\'t ask for this? Reply to this email or write to <a href="mailto:{SUPPORT_EMAIL}">'
            f"{SUPPORT_EMAIL}</a> straight away.</p>"
        )
        notify(
            "email", user.email, ADMIN_MESSAGE_TEMPLATE,
            {"subject": PASSWORD_SET_SUBJECT, "html_body": html, "text_body": html_to_text(html),
             "first_name": _first_name(user)},
            ref=user,
        )
    return subscriber(user.pk)


def _invoices_of(user: User) -> list[Invoice]:
    return list(Invoice.objects.filter(payment__subscription__owner=user).order_by("issued_at", "id"))


@transaction.atomic
def delete_customer(user: User) -> None:
    """
    Delete a customer account and everything that hangs off it (company, subscriptions,
    payments, devices, feedback -- all cascade), plus their uploaded payment proofs.

    Refused once the customer has a tax invoice: GST invoices (and the payments behind them) must
    be kept for 8 years, so such an account can only be blocked.
    """
    if user.is_staff:
        raise AdminActionError("You can't delete a staff account here.")
    invoices = _invoices_of(user)
    if invoices:
        numbers = ", ".join(inv.number for inv in invoices[:5]) + (" …" if len(invoices) > 5 else "")
        raise AdminActionError(
            f"This customer has {len(invoices)} tax invoice{'s' if len(invoices) != 1 else ''} ({numbers}). "
            "Invoices and the payments behind them must be kept for 8 years, so the account can't be deleted. "
            "Block it instead."
        )
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
def change_plan(
    user: User, plan_code: str, staff: User | None = None, email_customer: bool = False,
) -> SubscriberView:
    """
    Grant the customer a plan immediately (admin override).

    Any current pending/active subscription is cancelled first; the new one is
    made active right away (a free/trial plan as a trial, a paid plan as a
    one-month admin grant). Reuses the billing engine's activation rules.
    `email_customer`: tell the owner (plan_changed_by_staff) once it's saved.
    """
    try:
        plan = Plan.objects.get(code=plan_code, is_active=True)
    except Plan.DoesNotExist as exc:
        raise AdminActionError(f"Unknown plan '{plan_code}'.") from exc

    previous = billing.current_subscription(user)
    previous_plan = previous.plan if previous and previous.status == Status.ACTIVE else None
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
    track(
        "admin.plan_changed",
        {"from": previous_plan.code if previous_plan else "", "to": plan.code, "emailed": email_customer},
        actor_email=staff.email if staff else "", ref=user,
    )
    view = _subscriber_view(User.objects.select_related("owned_company").get(pk=user.pk))
    if email_customer:
        sub = view.subscription
        context = {
            "first_name": _first_name(user),
            "plan": plan,
            "previous_plan": previous_plan,
            "downgrade": bool(previous_plan and plan.price_inr_monthly < previous_plan.price_inr_monthly),
            "ends_at": sub.ends_at if sub else None,
            "is_trial": bool(sub and sub.is_trial),
            "devices": view.device_count,
            "over_limit": max(0, view.device_count - plan.max_devices) if plan.max_devices else 0,
            "dashboard_url": f"{settings.ZINWE_APP_URL}/app",
            "billing_url": f"{settings.ZINWE_APP_URL}/app/settings/billing",
        }
        # After the commit, so the email only goes once the change is saved (and can't undo it by failing).
        transaction.on_commit(lambda: notify("email", user.email, "plan_changed_by_staff", context, ref=user))
    return view


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


def customer_emails(user: User, limit: int = 50) -> list[Notification]:
    """
    Every email Zinwe sent this customer, newest first: the team's own and the automatic ones (sign-up
    codes, receipts, plan and renewal emails), so staff don't resend what they already got. One-time
    codes are masked in the log (z_notification.services.notify `redact`).
    """
    return list(
        Notification.objects.filter(recipient=user.email, channel="email").order_by("-created_at", "-id")[:limit]
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


def normalise_reference(value: str) -> str:
    """A UTR / transaction id as typed off a receipt: no spaces, upper case (so 'abc 123' == 'ABC123')."""
    return re.sub(r"\s+", "", value or "").upper()


def duplicate_of(payment: ManualPayment) -> dict[str, Any] | None:
    """
    Another payment that used the same receipt file or the same UTR / reference (J8-03): the same
    screenshot sent again after a rejection, or one bank transfer reported for two payments.
    An approved match wins over a rejected or waiting one. None when there is no match.
    """
    refs = {normalise_reference(r) for r in (payment.staff_reference, payment.reference) if normalise_reference(r)}
    match = Q()
    if payment.proof_sha256:
        match |= Q(proof_sha256=payment.proof_sha256)
    for ref in refs:
        match |= Q(staff_reference__iexact=ref) | Q(reference__iexact=ref)
    if not match:
        return None
    others = list(
        ManualPayment.objects.filter(match).exclude(pk=payment.pk)
        .select_related("subscription__owner").order_by("-submitted_at", "-id")[:10]
    )
    if not others:
        return None
    rank = {PaymentStatus.VERIFIED: 0, PaymentStatus.SUBMITTED: 1, PaymentStatus.REJECTED: 2}
    other = min(others, key=lambda p: rank.get(p.status, 3))
    same_file = bool(payment.proof_sha256) and other.proof_sha256 == payment.proof_sha256
    return {
        "id": other.id,
        "status": other.status,
        "match": "receipt" if same_file else "reference",
        "same_customer": other.subscription.owner_id == payment.subscription.owner_id,
        "customer_email": other.subscription.owner.email,
        "submitted_at": other.submitted_at,
        "rejection_reason": other.rejection_reason,
    }


@transaction.atomic
def review_payment(
    payment_id: int, reviewer: User, *, approve: bool, reason: str = "",
    reference: str = "", amount: Decimal | None = None,
) -> PaymentView:
    """
    Approve (the plan activates for a month, owner emailed) or reject (with a reason the
    owner sees; they can resubmit) one submitted payment. Nothing activates without approval.

    Approving needs the UTR / reference and the amount staff read off the receipt (J8-01): both are
    saved on the payment with who checked it. A reference already used on an approved payment is refused.
    """
    try:
        if approve:
            reference = normalise_reference(reference)
            if not reference:
                raise AdminActionError("Type the UTR / reference you see on the receipt.")
            if amount is None or amount <= 0:
                raise AdminActionError("Type the amount you see on the receipt.")
            used = (
                ManualPayment.objects.filter(status=PaymentStatus.VERIFIED, staff_reference__iexact=reference)
                .exclude(pk=payment_id).select_related("subscription__owner").first()
            )
            if used is not None:
                raise AdminActionError(
                    f"Reference {reference} was already used for payment #{used.pk} "
                    f"({used.subscription.owner.email}, approved {used.reviewed_at:%d %b %Y}). "
                    "The same transfer can't pay twice: check the receipt, or reject this payment."
                    if used.reviewed_at else
                    f"Reference {reference} was already used for approved payment #{used.pk}."
                )
            # Stores both with staff_checked_by = reviewer, and puts the reference on the invoice (z_auth.billing).
            billing.verify_payment(payment_id, reviewer, staff_reference=reference, staff_amount=amount)
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
            "support_waiting": _support_waiting(),
            "revenue_inr": str(revenue),
            "feedback": {"count": feedback["count"], "average": feedback["average"]},
        },
        "signups": _signups_series(days=30),
        "revenue": _revenue_series(months=6),
    }


def _support_waiting() -> int:
    """Support conversations waiting for the team's answer."""
    from z_support.models import SupportTicket

    return SupportTicket.objects.filter(status=SupportTicket.Status.OPEN).count()


def badges() -> dict[str, int]:
    """What needs the team now, for the sidebar badges: payments to check and support waiting."""
    return {
        "payments_waiting": ManualPayment.objects.filter(status=PaymentStatus.SUBMITTED).count(),
        "support_waiting": _support_waiting(),
    }


def billing_settings() -> dict[str, Any]:
    """Whether GST is charged right now (the seller GSTIN is set; z_auth.pricing): drives the admin's tax wording."""
    gstin = (settings.ZINWE_INVOICE_SELLER.get("gstin") or "").strip()
    return {"gst_charged": bool(gstin), "seller_gstin": gstin}


def announcement_reach(audience: str, plan_codes: list[str]) -> dict[str, int]:
    """
    How many customers a dashboard announcement with this audience reaches now: active, verified owners
    whose plan matches (the same rule the dashboard uses, z_auth.announcements).
    """
    from z_auth.announcements import _reaches
    from z_auth.entitlements import entitlements_for
    from z_auth.models import Announcement

    probe = Announcement(audience=audience, plan_codes=plan_codes)
    owners = User.objects.filter(is_staff=False, is_active=True, email_verified_at__isnull=False)
    total = reached = 0
    for owner in owners:
        total += 1
        reached += _reaches(probe, entitlements_for(owner))
    return {"customers": reached, "total": total}


def release_reach(version: str) -> dict[str, int]:
    """PCs that would be offered `version` (connected PCs on an older one), out of all connected PCs."""
    from z_devices.models import AgentRelease
    from z_devices.services import release_adoption

    adoption = release_adoption(AgentRelease(version=version))
    return {"older": adoption["total"] - adoption["updated"], "total": adoption["total"]}


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
