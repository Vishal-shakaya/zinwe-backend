from io import StringIO
from pathlib import Path
from unittest import mock

from django.core.management import call_command
from django.test import SimpleTestCase, TestCase, override_settings
from rest_framework import status
from rest_framework.test import APITestCase


class HealthTests(APITestCase):
    def test_health_is_public_and_ok(self) -> None:
        response = self.client.get("/api/health/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        body = response.json()
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["service"], "zinwe-backend")
        self.assertEqual(body["version"], "0.1.0")
        self.assertIn("T", body["time"])

    def test_cors_header_for_allowed_origin(self) -> None:
        response = self.client.get("/api/health/", HTTP_ORIGIN="http://localhost:5173")
        self.assertEqual(response["access-control-allow-origin"], "http://localhost:5173")

    def test_no_cors_header_for_unknown_origin(self) -> None:
        response = self.client.get("/api/health/", HTTP_ORIGIN="https://evil.example")
        self.assertNotIn("access-control-allow-origin", response)


class AsgiTests(TestCase):
    """Requests through Django's ASGI handler (what Uvicorn calls), not WSGI."""

    def test_application_is_asgi_handler(self) -> None:
        from django.core.handlers.asgi import ASGIHandler

        from config.asgi import application

        self.assertIsInstance(application, ASGIHandler)

    async def test_health_over_asgi(self) -> None:
        response = await self.async_client.get("/api/health/", headers={"origin": "http://localhost:5173"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")
        self.assertEqual(response["access-control-allow-origin"], "http://localhost:5173")

    async def test_cors_preflight_over_asgi(self) -> None:
        response = await self.async_client.options(
            "/api/auth/early-access/",
            headers={
                "origin": "http://localhost:5173",
                "access-control-request-method": "POST",
                "access-control-request-headers": "content-type",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("POST", response["access-control-allow-methods"])


class ServeCommandTests(SimpleTestCase):
    """`manage.py serve` hands the right options to uvicorn.run."""

    def _run(self, *args: str, **settings_overrides: object) -> dict:
        with override_settings(**settings_overrides), mock.patch("uvicorn.run") as run:
            call_command("serve", *args, stdout=StringIO())
        app, = run.call_args.args
        self.assertEqual(app, "config.asgi:application")
        return run.call_args.kwargs

    def test_dev_defaults_reload_on_localhost(self) -> None:
        kwargs = self._run("--reload")
        self.assertEqual((kwargs["host"], kwargs["port"]), ("127.0.0.1", 8000))
        self.assertTrue(kwargs["reload"])
        self.assertNotIn("workers", kwargs)
        self.assertEqual(kwargs["lifespan"], "off")
        dirs = {Path(d).name for d in kwargs["reload_dirs"]}
        self.assertTrue({"config", "z_auth", "z_main"} <= dirs)
        self.assertNotIn(".venv", dirs)

    def test_workers_without_reload(self) -> None:
        kwargs = self._run("--no-reload", "--workers", "4", "--host", "0.0.0.0", "--port", "9000")
        self.assertEqual(kwargs["workers"], 4)
        self.assertNotIn("reload", kwargs)
        self.assertEqual((kwargs["host"], kwargs["port"]), ("0.0.0.0", 9000))

    def test_proxy_headers_follow_trust_setting(self) -> None:
        self.assertFalse(self._run("--no-reload", ZINWE_TRUST_X_FORWARDED_FOR=False)["proxy_headers"])
        self.assertTrue(self._run("--no-reload", ZINWE_TRUST_X_FORWARDED_FOR=True)["proxy_headers"])
