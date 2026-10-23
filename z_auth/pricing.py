"""
What a customer pays, and why: one place that turns (plan, company) into a quote.

- **India** (company country India): INR, billed monthly, 18% GST on top — CGST 9% + SGST 9% when the
  customer's state is the seller's (settings.ZINWE_GST["seller_state"]), IGST 18% otherwise. GST is only
  charged when the seller GSTIN is set (settings.ZINWE_INVOICE_SELLER["gstin"]).
- **Everywhere else** (international): USD, no GST — an export of services under a Letter of Undertaking (LUT).
  A plan with no USD price falls back to the INR quote.
- **Monthly or yearly** (2026-10-23): every owner chooses at checkout. A year costs the plan's yearly price
  (`price_inr_yearly` / `price_usd_yearly`, set on the admin app's Pricing page), or 12 x the monthly price when
  none is set. Without a choice, India starts monthly and international yearly (`cycle_for`).

The quote is shown on every payment screen, prefilled as the amount to pay, and frozen on the invoice.
Amounts are Decimals rounded to 0.01 (half up), each tax line separately, as on a GST invoice.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from django.conf import settings

from .models import Company, Plan, Subscription

INR = Subscription.Currency.INR
USD = Subscription.Currency.USD
MONTHLY = Subscription.BillingCycle.MONTHLY
YEARLY = Subscription.BillingCycle.YEARLY

INDIA_NAMES = {"india", "in", "ind", "bharat"}
CENT = Decimal("0.01")


def money(value: Any) -> Decimal:
    return Decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)


def is_india(company: Company | None) -> bool:
    """A company in India (or with no company yet: the home market) is billed in INR with GST."""
    if company is None:
        return True
    return (company.country or "").strip().lower() in INDIA_NAMES


def _same_state(a: str, b: str) -> bool:
    norm = lambda s: " ".join((s or "").lower().replace("nct of", "").replace("&", "and").split())  # noqa: E731
    return bool(norm(a)) and norm(a) == norm(b)


@dataclass
class TaxLine:
    label: str  # "CGST", "SGST", "IGST"
    rate: Decimal  # percent, e.g. 9
    amount: Decimal


@dataclass
class Quote:
    plan_code: str
    plan_name: str
    currency: str  # "INR" | "USD"
    billing_cycle: str  # "monthly" | "yearly"
    months: int
    list_amount: Decimal  # before the discount, for the whole cycle
    discount_amount: Decimal
    amount: Decimal  # taxable value for the whole cycle (what the plan costs)
    taxes: list[TaxLine] = field(default_factory=list)
    tax_total: Decimal = Decimal("0.00")
    total: Decimal = Decimal("0.00")  # what the customer pays
    region: str = "IN"  # "IN" | "INTL"
    place_of_supply: str = ""
    export: bool = False
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        """JSON-safe: Decimals as "0.00" strings."""
        data = asdict(self)
        for key in ("list_amount", "discount_amount", "amount", "tax_total", "total"):
            data[key] = str(data[key])
        data["taxes"] = [
            {"label": t.label, "rate": str(t.rate.normalize()), "amount": str(t.amount)} for t in self.taxes
        ]
        return data


def currency_for(company: Company | None, plan: Plan | None = None) -> str:
    if is_india(company):
        return INR
    if plan is not None and plan.requires_payment and not plan.price_usd_monthly:
        return INR  # not sold in USD yet
    return USD


def cycle_for(currency: str) -> str:
    return YEARLY if currency == USD else MONTHLY


def months_in(cycle: str) -> int:
    return 12 if cycle == YEARLY else 1


def valid_cycle(cycle: Any) -> str | None:
    """"monthly" / "yearly", or None for anything else (the caller's default then applies)."""
    return cycle if cycle in (MONTHLY, YEARLY) else None


def _monthly_prices(plan: Plan, currency: str) -> tuple[Decimal, Decimal]:
    """(list price, price) per month in `currency`; list = price when there's no discount."""
    if currency == USD:
        price = plan.price_usd_monthly or Decimal("0")
        list_price = plan.list_price_usd_monthly or Decimal("0")
    else:
        price = plan.price_inr_monthly or Decimal("0")
        list_price = plan.list_price_inr_monthly or Decimal("0")
    if list_price <= price:
        list_price = price
    return money(list_price), money(price)


def cycle_prices(plan: Plan, currency: str, cycle: str) -> tuple[Decimal, Decimal]:
    """
    (list price, price) for one whole billing cycle in `currency`. A year is the plan's yearly price when set,
    else 12 months; its list price is 12 x the monthly list price (what the discount is shown against).
    """
    list_month, price_month = _monthly_prices(plan, currency)
    if cycle != YEARLY:
        return list_month, price_month
    yearly = plan.price_usd_yearly if currency == USD else plan.price_inr_yearly
    price = money(yearly) if yearly is not None and yearly > 0 else money(price_month * 12)
    return max(money(list_month * 12), price), price


def yearly_saving_percent(plan: Plan, currency: str) -> int:
    """How much less a year costs than 12 monthly payments, in whole percent (0 when it doesn't)."""
    _, month = _monthly_prices(plan, currency)
    _, year = cycle_prices(plan, currency, YEARLY)
    if month <= 0 or year >= month * 12:
        return 0
    return int(((1 - year / (month * 12)) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def quote(
    plan: Plan,
    company: Company | None,
    currency: str | None = None,
    cycle: str | None = None,
    cycle_amount: Decimal | None = None,
) -> Quote:
    """
    What `company` pays for one billing cycle of `plan` (currency/cycle default from the country).
    `cycle_amount` replaces the catalogue price (a subscriber's locked-in price), before tax.
    """
    currency = currency or currency_for(company, plan)
    cycle = valid_cycle(cycle) or cycle_for(currency)
    months = months_in(cycle)
    list_amount, amount = cycle_prices(plan, currency, cycle)
    if cycle_amount is not None:
        amount = money(cycle_amount)
        list_amount = max(list_amount, amount)
    q = Quote(
        plan_code=plan.code, plan_name=plan.name, currency=currency, billing_cycle=cycle, months=months,
        list_amount=list_amount, discount_amount=money(list_amount - amount), amount=amount,
        region="IN" if currency == INR else "INTL",
    )
    gstin = (settings.ZINWE_INVOICE_SELLER.get("gstin") or "").strip()
    gst = settings.ZINWE_GST
    if currency == INR:
        q.place_of_supply = (company.state if company else "") or gst["seller_state"]
        if gstin and amount > 0:
            rate = Decimal(gst["rate"])
            if _same_state(q.place_of_supply, gst["seller_state"]):
                half = rate / 2
                q.taxes = [TaxLine("CGST", half, money(amount * half / 100)), TaxLine("SGST", half, money(amount * half / 100))]
            else:
                q.taxes = [TaxLine("IGST", rate, money(amount * rate / 100))]
        elif not gstin:
            q.note = "GST not charged."
    else:
        q.export = True
        q.place_of_supply = (company.country if company else "") or "Outside India"
        lut = (gst.get("lut_arn") or "").strip()
        q.note = "Export of services: supply meant for export under LUT without payment of integrated tax" + (
            f" (LUT ARN {lut})." if lut else "."
        )
    q.tax_total = money(sum((t.amount for t in q.taxes), Decimal("0")))
    q.total = money(q.amount + q.tax_total)
    return q


def quote_for_subscription(subscription: Subscription, plan: Plan | None = None, cycle: str | None = None) -> Quote:
    """
    The next payment on `subscription` (or an upgrade to `plan`), in the subscription's own currency, for one
    billing cycle: `cycle` when the owner chooses one at checkout, else the subscription's own.
    Renewing the same plan on the same cycle keeps the price the owner signed up at (Terms: "you keep the price you
    signed up at until we tell you otherwise"); an upgrade or a change of cycle is priced from today's catalogue.
    """
    target = plan or subscription.plan
    cycle = valid_cycle(cycle) or subscription.billing_cycle
    locked = None
    if (
        target.pk == subscription.plan_id
        and cycle == subscription.billing_cycle
        and not subscription.is_trial
        and (subscription.amount_inr or 0) > 0
    ):
        locked = subscription.amount_inr
    return quote(target, subscription.company, subscription.currency, cycle, cycle_amount=locked)


def money_text(amount: Decimal | str, currency: str) -> str:
    """₹1,145.78 / $144.00 — for emails."""
    value = Decimal(amount)
    symbol = "₹" if currency == INR else "$"
    return f"{symbol}{value:,.2f}"
