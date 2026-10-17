"""Staff side of the website forms inbox, mounted at /api/admin/forms/ (the admin app's Forms page)."""
from __future__ import annotations

from django.shortcuts import get_object_or_404
from rest_framework.permissions import IsAdminUser
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from . import services
from .models import FormSubmission
from .serializers import SubmissionSerializer, UpdateSubmissionSerializer


class StaffFormsView(APIView):
    """
    GET /api/admin/forms/?kind=contact&status=new|handled&q=text -- newest first (up to 500), plus
    {kinds: [{kind, label, total, new}], counts: {new, handled}} for the tabs.
    """

    permission_classes = [IsAdminUser]

    def get(self, request: Request) -> Response:
        kind = (request.query_params.get("kind") or "").strip() or None
        wanted = request.query_params.get("status") or None
        if wanted not in FormSubmission.Status.values:
            wanted = None
        rows = services.submissions_for_staff(kind, wanted, request.query_params.get("q") or "")
        return Response(
            {
                "kinds": services.kinds_with_counts(),
                "counts": services.status_counts(kind),
                "rows": SubmissionSerializer(rows[: services.STAFF_LIST_LIMIT], many=True).data,
            }
        )


class StaffFormView(APIView):
    """GET /api/admin/forms/<id>/ -- one submission. PATCH {status?, note?} -- mark handled / reopen, internal note."""

    permission_classes = [IsAdminUser]

    def get(self, request: Request, pk: int) -> Response:
        return Response(SubmissionSerializer(get_object_or_404(FormSubmission, pk=pk)).data)

    def patch(self, request: Request, pk: int) -> Response:
        sub = get_object_or_404(FormSubmission.objects.select_related("handled_by"), pk=pk)
        data = UpdateSubmissionSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        v = data.validated_data
        sub = services.update_submission(sub, request.user, status=v.get("status"), note=v.get("note"))
        return Response(SubmissionSerializer(sub).data)
