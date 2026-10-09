"""The owner's company: one per owner in v1."""
from __future__ import annotations

from typing import Any

from django.db import IntegrityError, transaction

from z_analytics.services import track

from .models import Company, User


class CompanyExists(Exception):
    message = "Your company is already registered."

    def __str__(self) -> str:
        return self.message


def get_company(user: User) -> Company | None:
    """The user's company, or None."""
    return Company.objects.filter(owner=user).first()


def _sync_legacy_name(user: User, name: str) -> None:
    """Keep the old free-text `User.company` in step with the Company name."""
    if user.company != name:
        user.company = name
        user.save(update_fields=["company"])


def register_company(user: User, data: dict[str, Any]) -> Company:
    """Create the user's company from validated data. Raises `CompanyExists`."""
    if Company.objects.filter(owner=user).exists():
        raise CompanyExists()
    try:
        with transaction.atomic():
            company = Company.objects.create(owner=user, **data)
            _sync_legacy_name(user, company.name)
    except IntegrityError as exc:  # a concurrent POST won the OneToOne race
        raise CompanyExists() from exc
    transaction.on_commit(
        lambda: track(
            "company.registered",
            {"industry": company.industry, "team_size": company.team_size},
            actor_email=user.email,
            ref=company,
        )
    )
    return company


@transaction.atomic
def update_company(company: Company, data: dict[str, Any]) -> Company:
    """Apply a partial update from validated data."""
    for field, value in data.items():
        setattr(company, field, value)
    company.save()
    _sync_legacy_name(company.owner, company.name)
    return company
