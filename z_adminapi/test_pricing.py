"""The admin app's Plans & pricing page: prices by region (India / International) and cycle (monthly / yearly)."""
from __future__ import annotations

from rest_framework import status

from z_auth.models import Plan

from .tests import AdminApiTestCase

URL = "/api/admin/pricing/"


class PricingTests(AdminApiTestCase):
    def test_overview(self) -> None:
        body = self.client.get(URL).json()
        plans = {p["code"]: p for p in body["plans"]}
        self.assertEqual(list(plans), ["free", "startups", "teams", "businesses"])
        inr = plans["startups"]["regions"]["INR"]
        self.assertEqual(inr["monthly"], {"list": "1349.00", "price": "971.00", "discount_percent": 28})
        self.assertEqual((inr["yearly"]["price"], inr["yearly"]["custom"], inr["yearly"]["saving_percent"]), ("9710.00", True, 17))
        self.assertEqual(inr["yearly"]["months_charged"], "10.0")
        self.assertEqual(plans["startups"]["regions"]["USD"]["monthly"]["discount_percent"], 25)
        self.assertTrue(plans["free"]["is_free"])

    def test_change_prices(self) -> None:
        response = self.client.patch(
            f"{URL}startups/",
            {
                "INR": {"list_monthly": "1500", "discount_percent": 20, "yearly_price": "12000"},
                "USD": {"list_monthly": "20", "discount_percent": 25, "yearly_price": None},
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.content)
        plan = Plan.objects.get(code="startups")
        self.assertEqual((plan.price_inr_monthly, plan.price_inr_yearly), (1200, 12000))
        self.assertEqual((plan.price_usd_monthly, plan.list_price_usd_monthly, plan.price_usd_yearly), (15, 20, None))
        usd = response.json()["regions"]["USD"]["yearly"]
        self.assertEqual((usd["price"], usd["custom"], usd["saving_percent"]), ("180.00", False, 0))

    def test_rules(self) -> None:
        too_dear = self.client.patch(
            f"{URL}startups/", {"INR": {"list_monthly": "1000", "discount_percent": 0, "yearly_price": "12001"}}, format="json",
        )
        self.assertEqual(too_dear.status_code, status.HTTP_400_BAD_REQUEST)
        free = self.client.patch(f"{URL}free/", {"INR": {"list_monthly": "100", "discount_percent": 0}}, format="json")
        self.assertEqual(free.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(Plan.objects.get(code="free").price_inr_monthly, 0)

    def test_one_recommended_plan(self) -> None:
        self.client.patch(f"{URL}teams/", {"is_recommended": True}, format="json")
        self.assertEqual(list(Plan.objects.filter(is_recommended=True).values_list("code", flat=True)), ["teams"])

    def test_staff_only(self) -> None:
        self.as_user(self.make_owner())
        self.assertEqual(self.client.get(URL).status_code, status.HTTP_403_FORBIDDEN)
