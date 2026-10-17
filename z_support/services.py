"""
Support logic. Every new owner message emails the Zinwe team inbox; every staff reply emails
the owner (and shows in their dashboard's Support chat). Side effects run on commit.
"""
from __future__ import annotations

from django.conf import settings
from django.db import transaction
from django.db.models import Count, Q, QuerySet

from z_analytics.services import track
from z_auth.models import User
from z_notification.services import notify

from .models import SupportMessage, SupportTicket

Status = SupportTicket.Status
Sender = SupportMessage.Sender


def tickets_for_owner(owner: User) -> QuerySet:
    return (
        SupportTicket.objects.filter(owner=owner)
        .annotate(unread=Count("messages", filter=Q(messages__sender=Sender.STAFF, messages__read_by_owner=False)))
        .order_by("-updated_at", "-id")
    )


def tickets_for_staff(status: str | None = None) -> QuerySet:
    tickets = SupportTicket.objects.select_related("owner", "owner__owned_company").annotate(
        unread=Count("messages", filter=Q(messages__sender=Sender.OWNER, messages__read_by_staff=False)),
        message_count=Count("messages"),
    )
    if status:
        tickets = tickets.filter(status=status)
    return tickets.order_by("-updated_at", "-id")


def _company(owner: User) -> str:
    company = getattr(owner, "owned_company", None)
    return getattr(company, "name", "") or owner.company or ""


SUBJECT_FROM_BODY_CHARS = 60


def subject_from_body(body: str) -> str:
    """A ticket title from the first line of the message, cut on a word boundary."""
    line = next((ln.strip() for ln in body.splitlines() if ln.strip()), "")
    line = " ".join(line.split())
    if len(line) <= SUBJECT_FROM_BODY_CHARS:
        return line or "Support request"
    cut = line[:SUBJECT_FROM_BODY_CHARS].rsplit(" ", 1)[0] or line[:SUBJECT_FROM_BODY_CHARS]
    return cut.rstrip(" ,.;:-") + "…"


@transaction.atomic
def open_ticket(owner: User, subject: str, category: str, body: str, page: str = "") -> SupportTicket:
    ticket = SupportTicket.objects.create(
        owner=owner, subject=subject.strip() or subject_from_body(body), category=category, page=page[:120],
    )
    message = SupportMessage.objects.create(
        ticket=ticket, sender=Sender.OWNER, author=owner, body=body.strip(), read_by_owner=True,
    )
    transaction.on_commit(lambda: _tell_team(ticket, message, new=True))
    return ticket


@transaction.atomic
def owner_reply(owner: User, ticket: SupportTicket, body: str) -> SupportMessage:
    if ticket.owner_id != owner.id:
        raise PermissionError("Not your ticket.")
    message = SupportMessage.objects.create(
        ticket=ticket, sender=Sender.OWNER, author=owner, body=body.strip(), read_by_owner=True,
    )
    ticket.status = Status.OPEN  # back in the team's queue (reopens a closed one too)
    ticket.save(update_fields=["status", "updated_at"])
    transaction.on_commit(lambda: _tell_team(ticket, message, new=False))
    return message


@transaction.atomic
def staff_reply(staff: User, ticket: SupportTicket, body: str, close: bool = False) -> SupportMessage:
    message = SupportMessage.objects.create(
        ticket=ticket, sender=Sender.STAFF, author=staff, body=body.strip(), read_by_staff=True,
    )
    ticket.status = Status.CLOSED if close else Status.ANSWERED
    ticket.save(update_fields=["status", "updated_at"])
    mark_read_by_staff(ticket)
    transaction.on_commit(lambda: _tell_owner(ticket, message))
    return message


def set_status(ticket: SupportTicket, status: str) -> SupportTicket:
    ticket.status = status
    ticket.save(update_fields=["status", "updated_at"])
    return ticket


def mark_read_by_owner(ticket: SupportTicket) -> None:
    ticket.messages.filter(sender=Sender.STAFF, read_by_owner=False).update(read_by_owner=True)


def mark_read_by_staff(ticket: SupportTicket) -> None:
    ticket.messages.filter(sender=Sender.OWNER, read_by_staff=False).update(read_by_staff=True)


def _tell_team(ticket: SupportTicket, message: SupportMessage, new: bool) -> None:
    owner = ticket.owner
    track("support.message", {"ticket": ticket.pk, "new": new, "category": ticket.category},
          actor_email=owner.email, ref=ticket)
    notify(
        "email",
        settings.ZINWE_TEAM_NOTIFY_EMAIL,
        "support_team",
        {
            "ticket": ticket, "message": message, "owner": owner, "company": _company(owner), "new": new,
            "admin_url": f"{settings.ZINWE_ADMIN_URL}/support?ticket={ticket.pk}",
        },
        ref=ticket,
    )


def _tell_owner(ticket: SupportTicket, message: SupportMessage) -> None:
    owner = ticket.owner
    notify(
        "email",
        owner.email,
        "support_reply",
        {
            "first_name": owner.get_short_name() if owner.full_name else "there",
            "ticket": ticket, "message": message,
            "dashboard_url": f"{settings.ZINWE_APP_URL}/app?support={ticket.pk}",
        },
        ref=ticket,
    )


# --- Public contact form (website "Talk to us") ------------------------------------------------


def send_contact_message(data: dict, ip: str | None = None, user_agent: str = "") -> None:
    """
    A visitor wrote to us from the website contact form (J1-01): email the team inbox (reply-to = the
    visitor, so "Reply" answers them) and send the visitor a short "we got your message". Never raises.
    """
    context = {
        "name": data["name"],
        "first_name": (data["name"].split() or ["there"])[0],
        "email": data["email"],
        "company": data.get("company") or "",
        "phone": data.get("phone") or "",
        "topic": data.get("topic") or "",
        "message": data["message"],
        "ip": ip or "",
        "user_agent": user_agent,
    }

    def _send() -> None:
        track("contact.sent", {"topic": context["topic"]}, actor_email=data["email"])
        notify(
            "email", settings.ZINWE_TEAM_NOTIFY_EMAIL, "contact_team", context,
            ref=("z_support.contact", data["email"]), reply_to=[data["email"]],
        )
        notify("email", data["email"], "contact_confirmation", context, ref=("z_support.contact", data["email"]))

    transaction.on_commit(_send)
