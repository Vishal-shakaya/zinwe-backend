from rest_framework.permissions import IsAdminUser
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from .services import build_summary


class SummaryView(APIView):
    """GET /api/analytics/summary/ -- staff only."""

    permission_classes = [IsAdminUser]

    def get(self, request: Request) -> Response:
        return Response(build_summary())
