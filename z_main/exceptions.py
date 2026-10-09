"""DRF exception handler: the stock one, plus `retry_after` on every 429 body."""
from __future__ import annotations

import math
from typing import Any

from rest_framework.exceptions import Throttled
from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_exception_handler


def exception_handler(exc: Exception, context: dict[str, Any]) -> Response | None:
    """
    Wrap DRF's handler so throttled responses carry `retry_after` (whole seconds).

    Shape: `{"detail": "...", "retry_after": 42}`. The `Retry-After` header is
    still set by DRF.
    """
    response = drf_exception_handler(exc, context)
    if response is not None and isinstance(exc, Throttled) and isinstance(response.data, dict):
        wait = exc.wait
        response.data["retry_after"] = max(1, math.ceil(wait)) if wait is not None else 60
    return response
