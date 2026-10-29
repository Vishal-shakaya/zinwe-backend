"""Thin staff-only HTTP layer for the internal admin app. All views require IsAdminUser."""
from __future__ import annotations

from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import IsAdminUser
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from z_analytics.services import track
from z_auth.models import Announcement, ManualPayment, Plan, Subscription, User
from z_auth.portal_views import invoice_pdf_response
from z_devices.models import AgentRelease
from z_devices.services import ReleaseRefused, is_valid_version, publish_release
from z_main.client import get_client_ip

from . import services
from .serializers import (
    AdminSubscriptionSerializer,
    AnnouncementSerializer,
    ApprovePaymentSerializer,
    BlockSerializer,
    ChangePlanSerializer,
    PublishReleaseSerializer,
    ReleaseRowSerializer,
    UpdateReleaseSerializer,
    FeedbackRowSerializer,
    PaymentQueueSerializer,
    PlanSummarySerializer,
    RejectPaymentSerializer,
    RemarkSerializer,
    SendEmailSerializer,
    SentEmailSerializer,
    SetPasswordSerializer,
    SubscriberSerializer,
)


class StaffView(APIView):
    """Base: staff-only, no extra throttle (internal tool)."""

    permission_classes = [IsAdminUser]


def _refused(exc: Exception) -> Response:
    return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)


def _audit(request: Request, action: str, properties: dict | None = None, ref: object = None) -> None:
    """The staff audit trail (FIX-007): an "admin.<action>" analytics event with who did it. Never raises."""
    track(f"admin.{action}", properties or {}, actor_email=request.user.email, ref=ref)


class MeView(StaffView):
    """GET /api/admin/me/ -- confirm the operator is staff; used by the app guard."""

    def get(self, request: Request) -> Response:
        user = request.user
        return Response(
            {"id": user.id, "email": user.email, "full_name": user.full_name, "is_staff": user.is_staff}
        )


class MetricsView(StaffView):
    """GET /api/admin/metrics/ -- dashboard totals + signup/revenue series."""

    def get(self, request: Request) -> Response:
        return Response(services.dashboard_metrics())


class PlanListView(StaffView):
    """GET /api/admin/plans/ -- active plans, for the plan picker."""

    def get(self, request: Request) -> Response:
        plans = Plan.objects.filter(is_active=True).order_by("sort_order", "code")
        return Response(PlanSummarySerializer(plans, many=True).data)


class PricingView(StaffView):
    """GET /api/admin/pricing/ -- every plan's prices by region (India / International) and cycle."""

    def get(self, request: Request) -> Response:
        from .pricing import pricing_overview

        return Response(pricing_overview())


class PricingDetailView(StaffView):
    """PATCH /api/admin/pricing/<code>/ -- change a plan's prices (and which plan is recommended)."""

    def patch(self, request: Request, code: str) -> Response:
        from .pricing import PricingUpdateSerializer, plan_pricing

        plan = get_object_or_404(Plan, code=code, is_active=True)
        data = PricingUpdateSerializer(data=request.data, context={"plan": plan})
        data.is_valid(raise_exception=True)
        plan = data.save()
        track("admin.pricing_changed", {"plan": plan.code, "fields": sorted(request.data.keys())}, actor_email=request.user.email)
        return Response(plan_pricing(plan, plan.subscriptions.filter(status=Subscription.Status.ACTIVE, is_trial=False).count()))


# --- Subscribers ---------------------------------------------------------------


class SubscriberListView(StaffView):
    """GET /api/admin/subscribers/ -- every account owner with plan, payments and cycles."""

    def get(self, request: Request) -> Response:
        return Response(SubscriberSerializer(services.subscribers(), many=True).data)


def _owner_or_404(pk: int) -> User:
    return get_object_or_404(User, pk=pk, is_staff=False)


class SubscriberDetailView(StaffView):
    """GET /api/admin/subscribers/<id>/ -- one owner.  DELETE -- delete the account for good."""

    def get(self, request: Request, pk: int) -> Response:
        try:
            view = services.subscriber(pk)
        except User.DoesNotExist:
            return Response({"detail": "No such customer."}, status=status.HTTP_404_NOT_FOUND)
        return Response(SubscriberSerializer(view).data)

    def delete(self, request: Request, pk: int) -> Response:
        user = _owner_or_404(pk)
        email = user.email
        try:
            services.delete_customer(user)
        except services.AdminActionError as exc:
            return _refused(exc)
        _audit(request, "customer_deleted", {"customer_id": pk, "email": email})
        return Response(status=status.HTTP_204_NO_CONTENT)


class ApprovePaymentView(StaffView):
    """POST /api/admin/subscribers/<id>/approve-payment/ -- {reference, amount} from the receipt; verify the latest payment."""

    def post(self, request: Request, pk: int) -> Response:
        user = _owner_or_404(pk)
        if services._latest_submitted_payment(user) is None:
            return _refused(services.AdminActionError("This customer has no payment waiting to be checked."))
        data = ApprovePaymentSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        try:
            view = services.approve_payment(
                user, request.user, data.validated_data["reference"], data.validated_data["amount"]
            )
        except services.AdminActionError as exc:
            return _refused(exc)
        return Response(SubscriberSerializer(view).data)


class RejectPaymentView(StaffView):
    """POST /api/admin/subscribers/<id>/reject-payment/ -- reject the latest payment."""

    def post(self, request: Request, pk: int) -> Response:
        user = _owner_or_404(pk)
        serializer = RejectPaymentSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            view = services.reject_payment(user, request.user, serializer.validated_data["reason"])
        except services.AdminActionError as exc:
            return _refused(exc)
        return Response(SubscriberSerializer(view).data)


class BlockView(StaffView):
    """
    POST /api/admin/subscribers/<id>/block/ -- {reason, email_customer}: stop sign-in (login then says the
    account is paused); the reason is recorded and, when `email_customer`, emailed (account_suspended).
    """

    def post(self, request: Request, pk: int) -> Response:
        user = _owner_or_404(pk)
        data = BlockSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        try:
            view = services.set_blocked(
                user, True, request.user, data.validated_data["reason"], data.validated_data["email_customer"]
            )
        except services.AdminActionError as exc:
            return _refused(exc)
        return Response(SubscriberSerializer(view).data)


class UnblockView(StaffView):
    """POST /api/admin/subscribers/<id>/unblock/ -- let them sign in again."""

    def post(self, request: Request, pk: int) -> Response:
        user = _owner_or_404(pk)
        try:
            view = services.set_blocked(user, False, request.user)
        except services.AdminActionError as exc:
            return _refused(exc)
        return Response(SubscriberSerializer(view).data)


class ChangePlanView(StaffView):
    """POST /api/admin/subscribers/<id>/change-plan/ -- {plan, email_customer?}: grant a plan immediately."""

    def post(self, request: Request, pk: int) -> Response:
        user = _owner_or_404(pk)
        serializer = ChangePlanSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            view = services.change_plan(
                user, serializer.validated_data["plan"], request.user, serializer.validated_data["email_customer"]
            )
        except services.AdminActionError as exc:
            return _refused(exc)
        return Response(SubscriberSerializer(view).data)


class SetPasswordView(StaffView):
    """
    POST /api/admin/subscribers/<id>/set-password/ -- {password, email_customer?}; signs the customer out
    everywhere and (by default) emails them that their password was changed (never the password itself).
    """

    def post(self, request: Request, pk: int) -> Response:
        user = _owner_or_404(pk)
        serializer = SetPasswordSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            view = services.set_password(
                user, serializer.validated_data["password"], request.user, serializer.validated_data["email_customer"]
            )
        except services.AdminActionError as exc:
            return _refused(exc)
        return Response(SubscriberSerializer(view).data)


class SendPasswordResetView(StaffView):
    """
    POST /api/admin/subscribers/<id>/send-password-reset/ -- email them a code to choose a new password
    (the website's "Forgot password" flow). The safe default: nobody on the team learns the password.
    """

    def post(self, request: Request, pk: int) -> Response:
        user = _owner_or_404(pk)
        try:
            services.send_password_reset(user, get_client_ip(request))
        except services.AdminActionError as exc:
            return _refused(exc)
        track("admin.password_reset_sent", {}, actor_email=request.user.email, ref=user)
        return Response({"sent": True, "email": user.email})


class BadgesView(StaffView):
    """GET /api/admin/badges/ -- {payments_waiting, support_waiting, forms_new} for the sidebar."""

    def get(self, request: Request) -> Response:
        return Response(services.badges())


class BillingSettingsView(StaffView):
    """GET /api/admin/billing-settings/ -- {gst_charged, seller_gstin}: whether GST is charged right now."""

    def get(self, request: Request) -> Response:
        return Response(services.billing_settings())


class RemarkView(StaffView):
    """POST /api/admin/subscribers/<id>/remark/ -- {text}; the team's internal note (empty clears it)."""

    def post(self, request: Request, pk: int) -> Response:
        user = _owner_or_404(pk)
        serializer = RemarkSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        text = serializer.validated_data["text"]
        view = services.set_remark(user, text, request.user)
        # The note itself stays out of the log (it's in CustomerRemark, with updated_by).
        _audit(request, "remark_saved" if (text or "").strip() else "remark_cleared", {"customer_id": user.pk}, user)
        return Response(SubscriberSerializer(view).data)


class CustomerEmailView(StaffView):
    """
    GET  /api/admin/subscribers/<id>/emails/ -- custom emails the team sent them, newest first.
    POST /api/admin/subscribers/<id>/emails/ -- {subject, html}; html is cleaned to a safe allow-list.
    """

    def get(self, request: Request, pk: int) -> Response:
        user = _owner_or_404(pk)
        return Response(SentEmailSerializer(services.customer_emails(user), many=True).data)

    def post(self, request: Request, pk: int) -> Response:
        user = _owner_or_404(pk)
        serializer = SendEmailSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            sent = services.send_customer_email(
                user, serializer.validated_data["subject"], serializer.validated_data["html"], request.user
            )
        except services.AdminActionError as exc:
            return _refused(exc)
        return Response(SentEmailSerializer(sent).data, status=status.HTTP_201_CREATED)


# --- Plan activation (payment review queue) -------------------------------------


class PaymentListView(StaffView):
    """GET /api/admin/payments/?status=submitted|verified|rejected -- reported payments, newest first."""

    def get(self, request: Request) -> Response:
        wanted = request.query_params.get("status") or None
        if wanted and wanted not in ManualPayment.Status.values:
            return Response({"detail": f"Unknown status '{wanted}'."}, status=status.HTTP_400_BAD_REQUEST)
        return Response(PaymentQueueSerializer(services.payments(wanted), many=True).data)


class PaymentApproveView(StaffView):
    """
    POST /api/admin/payments/<id>/approve/ -- {reference, amount}: what staff read off the receipt (both
    required, saved on the payment). The plan activates only now. A reference already used on an approved
    payment is refused.
    """

    def post(self, request: Request, pk: int) -> Response:
        payment = get_object_or_404(ManualPayment, pk=pk)
        if payment.status != ManualPayment.Status.SUBMITTED:
            return _refused(services.AdminActionError(f"Payment #{pk} is already {payment.get_status_display().lower()}."))
        data = ApprovePaymentSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        try:
            view = services.review_payment(
                pk, request.user, approve=True,
                reference=data.validated_data["reference"], amount=data.validated_data["amount"],
            )
        except services.AdminActionError as exc:
            return _refused(exc)
        return Response(PaymentQueueSerializer(view).data)


class PaymentRejectView(StaffView):
    """POST /api/admin/payments/<id>/reject/ -- {reason}; the customer sees it and can resubmit."""

    def post(self, request: Request, pk: int) -> Response:
        get_object_or_404(ManualPayment, pk=pk)
        serializer = RejectPaymentSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            view = services.review_payment(
                pk, request.user, approve=False, reason=serializer.validated_data["reason"]
            )
        except services.AdminActionError as exc:
            return _refused(exc)
        return Response(PaymentQueueSerializer(view).data)


# --- Feedback ---------------------------------------------------------------------


class FeedbackListView(StaffView):
    """GET /api/admin/feedback/ -- {summary: {count, average, distribution}, rows: [...]}."""

    def get(self, request: Request) -> Response:
        return Response(
            {
                "summary": services.feedback_summary(),
                "rows": FeedbackRowSerializer(services.feedback_rows(), many=True).data,
            }
        )


class ReleaseListView(StaffView):
    """
    GET /api/admin/releases/ -- every published Windows agent release (newest first) + PC adoption.
    POST (multipart) -- publish one: {version, notes, required, file} or {..., download_url, sha256}.
    PCs below it are offered the update on their next heartbeat; `required` pauses their work until updated.
    """

    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def get(self, request: Request) -> Response:
        return Response(ReleaseRowSerializer(AgentRelease.objects.select_related("published_by"), many=True).data)

    def post(self, request: Request) -> Response:
        data = PublishReleaseSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        v = data.validated_data
        try:
            release = publish_release(
                request.user, v["version"], v["notes"], v["required"],
                upload=v.get("file"), download_url=v["download_url"], sha256=v["sha256"],
            )
        except ReleaseRefused as exc:
            return _refused(exc)
        return Response(ReleaseRowSerializer(release).data, status=status.HTTP_201_CREATED)


class ReleaseReachView(StaffView):
    """GET /api/admin/releases/reach/?version=1.4.0 -- {older, total}: PCs that would be offered it (for the confirmation)."""

    def get(self, request: Request) -> Response:
        version = (request.query_params.get("version") or "").strip()
        if not is_valid_version(version):
            return Response({"detail": "Use numbers like 1.4.0."}, status=status.HTTP_400_BAD_REQUEST)
        return Response(services.release_reach(version))


class ReleaseDetailView(StaffView):
    """PATCH /api/admin/releases/<id>/ -- {required?, is_active?, notes?}."""

    def patch(self, request: Request, pk: int) -> Response:
        release = get_object_or_404(AgentRelease, pk=pk)
        data = UpdateReleaseSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        changed = []
        for field, value in data.validated_data.items():
            value = value.strip() if isinstance(value, str) else value
            if getattr(release, field) != value:
                setattr(release, field, value)
                changed.append(field)
        # Only what changed is written; an empty or no-change PATCH writes nothing (FIX-007).
        if changed:
            release.save(update_fields=changed)
            _audit(request, "release_updated", {"version": release.version, "fields": sorted(changed)}, release)
        return Response(ReleaseRowSerializer(release).data)


class AnnouncementListView(StaffView):
    """
    GET /api/admin/announcements/ -- every pushed message, newest first, with its live status.
    POST -- push one: {title, body?, kind, placement, audience, plan_codes?, cta_label?, cta_href?,
    starts_at?, ends_at?, is_active?, dismissible?, priority?}. Owners see it on their next page load.
    """

    def get(self, request: Request) -> Response:
        rows = Announcement.objects.select_related("created_by").order_by("-created_at", "-id")
        return Response(AnnouncementSerializer(rows, many=True).data)

    def post(self, request: Request) -> Response:
        data = AnnouncementSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        announcement = data.save(created_by=request.user)
        _audit(request, "announcement_created", {
            "id": announcement.pk, "title": announcement.title, "placement": announcement.placement,
            "audience": announcement.audience,
        }, announcement)
        return Response(AnnouncementSerializer(announcement).data, status=status.HTTP_201_CREATED)


class AnnouncementReachView(StaffView):
    """
    GET /api/admin/announcements/reach/?audience=paid&plans=startups,businesses -- {customers, total}: how
    many customers a dashboard message with this audience shows to right now.
    """

    def get(self, request: Request) -> Response:
        audience = request.query_params.get("audience") or Announcement.Audience.EVERYONE
        if audience not in Announcement.Audience.values:
            return Response({"detail": f"Unknown audience '{audience}'."}, status=status.HTTP_400_BAD_REQUEST)
        plans = [code for code in (request.query_params.get("plans") or "").split(",") if code]
        emails = [e.strip() for e in (request.query_params.get("emails") or "").split(",") if e.strip()]
        return Response(services.announcement_reach(audience, plans, emails))


class AnnouncementDetailView(StaffView):
    """PATCH /api/admin/announcements/<id>/ -- edit / switch on or off; DELETE -- remove it for good."""

    def patch(self, request: Request, pk: int) -> Response:
        announcement = get_object_or_404(Announcement, pk=pk)
        data = AnnouncementSerializer(announcement, data=request.data, partial=True)
        data.is_valid(raise_exception=True)
        announcement = data.save()
        _audit(request, "announcement_updated", {"id": pk, "fields": sorted(data.validated_data)}, announcement)
        return Response(AnnouncementSerializer(announcement).data)

    def delete(self, request: Request, pk: int) -> Response:
        announcement = get_object_or_404(Announcement, pk=pk)
        title = announcement.title
        announcement.delete()
        _audit(request, "announcement_deleted", {"id": pk, "title": title})
        return Response(status=status.HTTP_204_NO_CONTENT)


class SubscriptionListView(StaffView):
    """
    GET /api/admin/subscriptions/ -- every subscription (newest first, cancelled ones left out) with the
    customer's country, currency, cycle, the amount due next (with tax), the latest payment and invoices.
    """

    def get(self, request: Request) -> Response:
        rows = (
            Subscription.objects.exclude(status=Subscription.Status.CANCELLED)
            .select_related("owner", "company", "plan")
            .order_by("-created_at", "-id")
        )
        return Response(AdminSubscriptionSerializer(rows, many=True).data)


class SubscriptionDetailView(StaffView):
    """PATCH /api/admin/subscriptions/<id>/ -- {payment_link}: the Skydo (or other) link this customer pays with."""

    def patch(self, request: Request, pk: int) -> Response:
        subscription = get_object_or_404(Subscription.objects.select_related("owner", "company", "plan"), pk=pk)
        data = AdminSubscriptionSerializer(subscription, data={"payment_link": request.data.get("payment_link", "")}, partial=True)
        data.is_valid(raise_exception=True)
        subscription = data.save()
        _audit(request, "payment_link_set", {
            "subscription_id": subscription.pk, "customer_id": subscription.owner_id,
            "payment_link": subscription.payment_link,
        }, subscription)
        return Response(AdminSubscriptionSerializer(subscription).data)


class InvoicePdfView(StaffView):
    """GET /api/admin/payments/<id>/invoice/pdf/ -- any confirmed payment's invoice as a PDF."""

    def get(self, request: Request, pk: int):  # type: ignore[no-untyped-def]
        payment = get_object_or_404(
            ManualPayment.objects.select_related("subscription__plan", "subscription__company", "upgrade_plan"), pk=pk
        )
        if payment.status != ManualPayment.Status.VERIFIED:
            return Response({"detail": "Only a confirmed payment has an invoice."}, status=status.HTTP_409_CONFLICT)
        return invoice_pdf_response(payment)


class ReleaseNoteListView(StaffView):
    """GET /api/admin/release-notes/ -- every note (drafts too), newest first. POST -- write one (FR-21)."""

    def get(self, request: Request) -> Response:
        from z_auth.models import ReleaseNote

        from .serializers import ReleaseNoteSerializer

        rows = ReleaseNote.objects.order_by("-created_at", "-id")
        return Response(ReleaseNoteSerializer(rows, many=True).data)

    def post(self, request: Request) -> Response:
        from .serializers import ReleaseNoteSerializer

        data = ReleaseNoteSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        note = data.save(created_by=request.user)
        _audit(request, "release_note_created", {"id": note.pk, "version": note.version}, note)
        return Response(ReleaseNoteSerializer(note).data, status=status.HTTP_201_CREATED)


class ReleaseNoteDetailView(StaffView):
    """PATCH /api/admin/release-notes/<id>/ -- edit, publish (published_at) or unpublish (null); DELETE -- remove."""

    def patch(self, request: Request, pk: int) -> Response:
        from z_auth.models import ReleaseNote

        from .serializers import ReleaseNoteSerializer

        note = get_object_or_404(ReleaseNote, pk=pk)
        was_published = note.published_at is not None
        data = ReleaseNoteSerializer(note, data=request.data, partial=True)
        data.is_valid(raise_exception=True)
        note = data.save()
        props = {"id": pk, "version": note.version, "fields": sorted(data.validated_data)}
        if "published_at" in data.validated_data and was_published != (note.published_at is not None):
            _audit(request, "release_note_published" if note.published_at else "release_note_unpublished", props, note)
        else:
            _audit(request, "release_note_updated", props, note)
        return Response(ReleaseNoteSerializer(note).data)

    def delete(self, request: Request, pk: int) -> Response:
        from z_auth.models import ReleaseNote

        note = get_object_or_404(ReleaseNote, pk=pk)
        version = note.version
        note.delete()
        _audit(request, "release_note_deleted", {"id": pk, "version": version})
        return Response(status=status.HTTP_204_NO_CONTENT)


# --- Logs (FR-37) -------------------------------------------------------------------


class LogsView(StaffView):
    """
    GET /api/admin/logs/?range=24h|7d|30d&type=all|server|deploy|backup|agent|staff|error&q=&limit= -- one timeline,
    newest first: ops events (deploys, backups, API starts, 500s) merged with staff actions from the audit trail,
    agent releases and payment reviews. {env, range, type, q, now, counts{all, <type>...}, total, truncated, rows[]}.
    type=error also lists every failed job. At most `limit` rows (default 300, max 1000).
    """

    def get(self, request: Request) -> Response:
        from .logs import LogsQueryError, log_rows

        p = request.query_params
        try:
            return Response(log_rows(p.get("range"), p.get("type"), p.get("q"), p.get("limit")))
        except LogsQueryError as exc:
            return _refused(exc)


class LogsStatusView(StaffView):
    """
    GET /api/admin/logs/status/ -- this environment's health: API (release, started), database, server CPU / memory /
    disk (null where the OS doesn't say), last deploy and backup, and the unresolved failures (menu dot).
    """

    def get(self, request: Request) -> Response:
        from .logs import status as logs_status

        return Response(logs_status(request.get_host()))


class LogResolveView(StaffView):
    """POST /api/admin/logs/ops/<id>/resolve/ -- mark a failed job as handled (clears the dot); audit-logged."""

    def post(self, request: Request, pk: int) -> Response:
        from z_analytics.models import OpsEvent

        from .logs import ops_row, resolve

        event = get_object_or_404(OpsEvent, pk=pk)
        if event.unresolved:
            resolve(event, request.user.email)
            _audit(request, "log_resolved", {"id": event.pk, "what": event.what}, event)
        return Response(ops_row(event))
