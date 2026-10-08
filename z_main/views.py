from django.conf import settings
from django.utils import timezone
from rest_framework.permissions import AllowAny
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView


class HealthView(APIView):
    """GET /api/health/ -- public liveness probe."""

    permission_classes = [AllowAny]
    authentication_classes: list = []

    def get(self, request: Request) -> Response:
        return Response(
            {
                "status": "ok",
                "service": "zinwe-backend",
                "version": settings.ZINWE_VERSION,
                "time": timezone.now().isoformat(),
            }
        )
