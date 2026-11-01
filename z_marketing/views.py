"""Marketing endpoints. Staff: /api/admin/marketing/... Public: /api/marketing/unsubscribe/<token>/ (in every email)."""
from __future__ import annotations

from typing import Any

from django.conf import settings
from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.html import escape
from django.views.decorators.csrf import csrf_exempt
from rest_framework import serializers, status
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import AllowAny, IsAdminUser
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle
from rest_framework.views import APIView

from z_analytics.services import track

from . import outreach
from .models import Audience, Campaign, CampaignEmail, Contact, Suppression
from .outreach import OutreachError


def _chips(max_items: int = 40) -> serializers.ListField:
    return serializers.ListField(child=serializers.CharField(max_length=240), required=False, max_length=max_items)


class AudienceSerializer(serializers.ModelSerializer):
    segments = _chips()
    triggers = _chips()
    words = _chips()
    objections = _chips()
    not_for = _chips()
    problems = _chips()
    solutions = _chips()
    offers = _chips()
    channels = _chips()

    class Meta:
        model = Audience
        fields = [
            "id", "name", "country", "decider", "setup", "today", "confidence", "segments", "triggers", "words",
            "objections", "not_for", "problems", "solutions", "offers", "channels", "summary",
        ]

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        merged = {k: attrs.get(k, getattr(self.instance, k, None)) for k in ("segments", "problems", "channels")}
        for field, label in (("segments", "segment"), ("problems", "problem"), ("channels", "channel")):
            if not merged[field]:
                raise serializers.ValidationError({field: f"Add at least one {label}."})
        return attrs


class EmailStepSerializer(serializers.ModelSerializer):
    class Meta:
        model = CampaignEmail
        fields = ["n", "stage", "when", "week", "subject", "body"]
        read_only_fields = ["n", "stage", "when", "week"]


class CampaignInput(serializers.ModelSerializer):
    class Meta:
        model = Campaign
        fields = ["name", "audience", "goal", "theme", "problem", "offer", "daily_limit"]
        extra_kwargs = {"name": {"required": False}, "audience": {"required": False}}

    def validate_daily_limit(self, value: int) -> int:
        if value not in outreach.DAILY_LIMITS:
            raise serializers.ValidationError("Pick 10, 20 or 25 a day.")
        return value


class ContactSerializer(serializers.ModelSerializer):
    stage = serializers.SerializerMethodField()
    events = serializers.SerializerMethodField()

    class Meta:
        model = Contact
        fields = [
            "id", "first_name", "firm", "email", "city", "status", "stage", "next_email", "next_date", "last_email",
            "opted_in_at", "proof", "installed", "reason", "events",
        ]

    def get_stage(self, obj: Contact) -> str:
        return outreach.stage_of(obj, self.context["stages"])

    def get_events(self, obj: Contact) -> list[dict[str, str]]:
        return [{"at": e.at.isoformat(), "text": e.text} for e in obj.events.all()]


def _sender() -> dict[str, Any]:
    return {
        "from": settings.ZINWE_OUTREACH_FROM,
        "reply_to": settings.ZINWE_OUTREACH_REPLY_TO,
        "domain": outreach.sender_domain(),
        "mailbox_ready": outreach.mailbox_ready(),
        "smtp_host": settings.EMAIL_OUTREACH_HOST,
        "newsletter_from": settings.ZINWE_NEWSLETTER_FROM,
        "postal_address": settings.ZINWE_OUTREACH_POSTAL_ADDRESS,
        "limits": outreach.DAILY_LIMITS,
    }


def _summary(c: Campaign) -> dict[str, Any]:
    return {
        "id": c.id, "name": c.name, "audience": c.audience_id, "audience_name": c.audience.name, "goal": c.goal,
        "theme": c.theme, "status": c.status, "counts": outreach.counts(c),
    }


def _detail(c: Campaign) -> dict[str, Any]:
    due = outreach.due(c)
    room = max(0, c.daily_limit - outreach.sent_today(c))
    return {
        **_summary(c), "problem": c.problem, "offer": c.offer, "pause_reason": c.pause_reason,
        "daily_limit": c.daily_limit, "sent_today": outreach.sent_today(c), "ready_today": min(len(due), room),
        "dns_result": c.dns_result, "dns_checked_at": c.dns_checked_at, "rules_checked_at": c.rules_checked_at,
        "rules_checked_by": c.rules_checked_by, "test_sent_at": c.test_sent_at,
        "checks": outreach.checks(c), "report": outreach.report(c),
        "emails": EmailStepSerializer(c.emails.all(), many=True).data,
    }


class Staff(APIView):
    permission_classes = [IsAdminUser]

    def refused(self, exc: Exception) -> Response:
        return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

    def audit(self, request: Request, action: str, ref: object, **properties: Any) -> None:
        track(f"admin.marketing_{action}", properties, actor_email=request.user.email, ref=ref)


class AudienceListView(Staff):
    def get(self, request: Request) -> Response:
        return Response(AudienceSerializer(Audience.objects.all(), many=True).data)

    def post(self, request: Request) -> Response:
        data = AudienceSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        return Response(AudienceSerializer(data.save()).data, status=status.HTTP_201_CREATED)


class AudienceDetailView(Staff):
    def put(self, request: Request, pk: int) -> Response:
        data = AudienceSerializer(get_object_or_404(Audience, pk=pk), data=request.data)
        data.is_valid(raise_exception=True)
        return Response(AudienceSerializer(data.save()).data)


class CampaignListView(Staff):
    def get(self, request: Request) -> Response:
        rows = Campaign.objects.select_related("audience")
        return Response({"sender": _sender(), "campaigns": [_summary(c) for c in rows]})

    def post(self, request: Request) -> Response:
        """A new draft starts with a copy of the first campaign's emails, ready to edit."""
        data = CampaignInput(data=request.data)
        data.is_valid(raise_exception=True)
        if "audience" not in data.validated_data or not data.validated_data.get("name"):
            return self.refused(Exception("Add a name and pick an audience."))
        campaign = data.save()
        source = Campaign.objects.exclude(pk=campaign.pk).order_by("id").first()
        if source is not None:
            CampaignEmail.objects.bulk_create(
                CampaignEmail(campaign=campaign, n=s.n, stage=s.stage, when=s.when, week=s.week, subject=s.subject, body=s.body)
                for s in source.emails.all()
            )
        return Response(_detail(campaign), status=status.HTTP_201_CREATED)


class CampaignDetailView(Staff):
    def get(self, request: Request, pk: int) -> Response:
        return Response(_detail(get_object_or_404(Campaign.objects.select_related("audience"), pk=pk)))

    def patch(self, request: Request, pk: int) -> Response:
        campaign = get_object_or_404(Campaign, pk=pk)
        data = CampaignInput(campaign, data=request.data, partial=True)
        data.is_valid(raise_exception=True)
        return Response(_detail(data.save()))


class EmailStepView(Staff):
    def put(self, request: Request, pk: int, n: int) -> Response:
        step = get_object_or_404(CampaignEmail, campaign_id=pk, n=n)
        data = EmailStepSerializer(step, data=request.data, partial=True)
        data.is_valid(raise_exception=True)
        data.save()
        return Response(data.data)


class CampaignActionView(Staff):
    """POST .../campaigns/<id>/<action>/: launch, pause, test, dns, rules, send."""

    def post(self, request: Request, pk: int, action: str) -> Response:
        campaign = get_object_or_404(Campaign.objects.select_related("audience"), pk=pk)
        actor = request.user.email
        try:
            if action == "launch":
                outreach.launch(campaign, actor)
            elif action == "pause":
                outreach.pause(campaign, "Paused by staff.")
            elif action == "test":
                outreach.send_test(campaign, int(request.data.get("n", 1)), actor)
            elif action == "dns":
                outreach.check_dns(campaign)
            elif action == "rules":
                done = bool(request.data.get("checked"))
                campaign.rules_checked_at = timezone.now() if done else None
                campaign.rules_checked_by = actor if done else ""
                campaign.save(update_fields=["rules_checked_at", "rules_checked_by"])
            elif action == "send":
                approved = outreach.approve(campaign, int(request.data.get("expected", -1)), actor)
                self.audit(request, "outreach_approved", campaign, emails=approved)
                return Response({"approved": approved, **_detail(campaign)})
            else:
                return self.refused(Exception("That action isn't available."))
        except (OutreachError, ValueError) as exc:
            return self.refused(exc)
        self.audit(request, f"campaign_{action}", campaign)
        return Response(_detail(campaign))


class ContactListView(Staff):
    def get(self, request: Request, pk: int) -> Response:
        campaign = get_object_or_404(Campaign, pk=pk)
        stages = {e.n: e.stage for e in campaign.emails.all()}
        rows = campaign.contacts.prefetch_related("events")[:2000]
        return Response(ContactSerializer(rows, many=True, context={"stages": stages}).data)


class ContactImportView(Staff):
    def post(self, request: Request, pk: int) -> Response:
        campaign = get_object_or_404(Campaign, pk=pk)
        results = outreach.import_contacts(campaign, str(request.data.get("text", "")), request.user.email)
        self.audit(request, "contacts_added", campaign, added=sum(r["result"] == "good" for r in results))
        return Response({"results": results})


class ContactActView(Staff):
    def post(self, request: Request, pk: int) -> Response:
        contact = get_object_or_404(Contact.objects.select_related("campaign"), pk=pk)
        try:
            outreach.act(contact, str(request.data.get("action", "")), request.user.email, str(request.data.get("note", "")))
        except OutreachError as exc:
            return self.refused(exc)
        stages = {e.n: e.stage for e in contact.campaign.emails.all()}
        return Response(ContactSerializer(contact, context={"stages": stages}).data)


class BlockedView(Staff):
    def get(self, request: Request) -> Response:
        rows = Suppression.objects.all()[:2000]
        return Response([{"email": r.email, "reason": r.reason, "created_at": r.created_at} for r in rows])


class UnsubscribeThrottle(AnonRateThrottle):
    rate = "30/minute"


PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="robots" content="noindex">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Zinwe</title></head>
<body style="font:16px/1.5 Arial,Helvetica,sans-serif;color:#16181b;max-width:34rem;margin:15vh auto;padding:0 20px;">{}</body></html>"""


class UnsubscribeView(APIView):
    """GET shows a button, so a mail scanner opening the link changes nothing. POST unsubscribes: the button, or the
    RFC 8058 one-click request a mail client sends."""

    permission_classes = [AllowAny]
    authentication_classes: list = []
    parser_classes = [JSONParser, FormParser, MultiPartParser]
    throttle_classes = [UnsubscribeThrottle]

    def get(self, request: Request, token: str) -> HttpResponse:
        contact = Contact.objects.filter(token=token).first()
        if contact is None or contact.status == Contact.Status.SUPPRESSED:
            return self._page("<p>You won't get any more emails from us.</p>")
        return self._page(
            f"<p>Stop emails to <b>{escape(contact.email)}</b>?</p>"
            '<form method="post"><button type="submit" style="font:inherit;padding:10px 16px;">Unsubscribe</button></form>'
        )

    def post(self, request: Request, token: str) -> HttpResponse:
        outreach.unsubscribe(token)
        return self._page("<p>Done. You won't get any more emails from us.</p>")

    def _page(self, body: str) -> HttpResponse:
        return HttpResponse(PAGE.format(body))


unsubscribe_view = csrf_exempt(UnsubscribeView.as_view())
