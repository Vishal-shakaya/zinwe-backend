"""The admin's Documentation tab on a server: staff only, and only what was uploaded."""
from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path

from django.test import override_settings
from rest_framework import status
from rest_framework.test import APITestCase

from z_auth.models import User

INDEX = "/api/admin/docs/"
PAGE = "/api/admin/docs/00-overview/"


class DocsTests(APITestCase):
    def setUp(self) -> None:
        self.folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.folder, ignore_errors=True)
        (self.folder / "index.json").write_text(json.dumps({"pages": [{"slug": "00-overview"}]}), "utf-8")
        (self.folder / "page-00-overview.json").write_text(json.dumps({"html": "<h1>Hi</h1>"}), "utf-8")
        (self.folder.parent / "outside.json").write_text("{}", "utf-8")
        self.addCleanup((self.folder.parent / "outside.json").unlink, missing_ok=True)
        patcher = override_settings(DOCS_DIR=str(self.folder))
        patcher.enable()
        self.addCleanup(patcher.disable)
        self.staff = User.objects.create_superuser("ops@zinwe.local", "ops-12345678")

    def test_staff_read_the_index_and_a_page(self) -> None:
        self.client.force_authenticate(self.staff)
        self.assertEqual(self.client.get(INDEX).json()["pages"][0]["slug"], "00-overview")
        page = self.client.get(PAGE)
        self.assertEqual(page.json()["html"], "<h1>Hi</h1>")
        self.assertEqual(page["Cache-Control"], "no-store")
        self.assertTrue(self.client.get("/api/admin/me/").json()["has_docs"])

    def test_nobody_else_can(self) -> None:
        for url in (INDEX, PAGE):
            self.assertEqual(self.client.get(url).status_code, status.HTTP_401_UNAUTHORIZED)
        owner = User.objects.create_user("owner@example.com", "owner-12345678")
        self.client.force_authenticate(owner)
        for url in (INDEX, PAGE):
            self.assertEqual(self.client.get(url).status_code, status.HTTP_403_FORBIDDEN)

    def test_an_unknown_page_is_not_found(self) -> None:
        self.client.force_authenticate(self.staff)
        self.assertEqual(self.client.get("/api/admin/docs/nope/").status_code, status.HTTP_404_NOT_FOUND)

    def test_only_rendered_pages_can_be_asked_for(self) -> None:
        self.client.force_authenticate(self.staff)
        for slug in ("..%2Foutside", "..%2F..%2Fetc%2Fpasswd", "index"):
            self.assertEqual(self.client.get(f"/api/admin/docs/{slug}/").status_code, status.HTTP_404_NOT_FOUND)

    def test_a_server_without_docs_says_so(self) -> None:
        self.client.force_authenticate(self.staff)
        with override_settings(DOCS_DIR=""):
            self.assertEqual(self.client.get(INDEX).status_code, status.HTTP_404_NOT_FOUND)
            self.assertFalse(self.client.get("/api/admin/me/").json()["has_docs"])
