"""Helpers for loose "related object" references (ref_type + ref_id strings)."""
from __future__ import annotations

from typing import Any, Union

from django.db import models

# A model instance, or an explicit ("app_label.model", id) pair.
Ref = Union[models.Model, tuple[str, Any], None]


def resolve_ref(ref: Ref) -> tuple[str, str]:
    """Return (ref_type, ref_id) strings for a model instance or a (type, id) pair."""
    if ref is None:
        return "", ""
    if isinstance(ref, models.Model):
        return ref._meta.label_lower, str(ref.pk)
    ref_type, ref_id = ref
    return str(ref_type)[:100], str(ref_id)[:64]
