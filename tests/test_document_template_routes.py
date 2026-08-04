from __future__ import annotations

import re
import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path

from flask import Flask

from tests._support import PROJECT_ROOT, create_docx, prepare_config_import

prepare_config_import()

from routes.document_template_routes import document_template_bp
from services.document_template_read_service import build_document_whitelist
from services.oplot_ui_service import register_oplot_ui
from services.release_template_catalog_service import clear_template_catalog_cache


class _AssetUrlParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.urls = []

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        for name in ("src", "href", "action", "hx-get"):
            if values.get(name):
                self.urls.append(values[name])


def build_test_app(root: Path, *, enabled=None, static_folder=None) -> Flask:
    app = Flask(
        __name__,
        template_folder=str(PROJECT_ROOT / "templates"),
        static_folder=str(static_folder or PROJECT_ROOT / "static"),
    )
    app.config.update(
        TESTING=True,
        DOCUMENT_TEMPLATE_CENTER_ROOT=root,
        DOCUMENT_TEMPLATE_CENTER_RUNTIME_ROOT=root.parent / "runtime",
    )
    if enabled is not None:
        app.config["DOCUMENT_TEMPLATE_CENTER_ENABLED"] = enabled
    app.add_url_rule("/", endpoint="main.index", view_func=lambda: "home")
    app.add_url_rule("/help", endpoint="main.help_page", view_func=lambda: "help")
    app.add_url_rule("/dashboard", endpoint="dashboard.dashboard", view_func=lambda: "dashboard")
    app.add_url_rule("/release-monitor", endpoint="dashboard.release_monitor_page", view_func=lambda: "monitor")
    app.add_url_rule("/mpr", endpoint="mpr.mpr_page", view_func=lambda: "mpr")
    app.register_blueprint(document_template_bp)
    register_oplot_ui(app)
    return app


class DocumentTemplateRouteTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "doc_templates"
        self.payload = create_docx(self.root / "CAT" / "Комплект PL (12345)" / "Точный файл.docx")
        create_docx(self.root / "OTHER" / "Другой BH (54321)" / "Иной.docx")
        clear_template_catalog_cache()
        self.app = build_test_app(self.root)
        self.client = self.app.test_client()
        self.document_id = next(
            item.document_id
            for item in build_document_whitelist(self.root).values()
            if item.filename == "Точный файл.docx"
        )

    def tearDown(self):
        clear_template_catalog_cache()
        self.temporary.cleanup()

    def test_full_page_and_htmx_partial(self):
        full = self.client.get("/dashboard/release-monitor/document-templates")
        partial = self.client.get("/dashboard/release-monitor/document-templates?q=Комплект", headers={"HX-Request": "true"})
        self.assertEqual(200, full.status_code)
        self.assertIn(b"<!doctype html>", full.data.lower())
        self.assertIn("HX-Request", full.headers.get("Vary", ""))
        self.assertEqual(200, partial.status_code)
        self.assertNotIn(b"<html", partial.data.lower())
        self.assertIn("template-filter-form", partial.get_data(as_text=True))

    def test_search_filters_and_pagination_query_are_rendered(self):
        response = self.client.get("/dashboard/release-monitor/document-templates", query_string={
            "q": "Иной",
            "category": "OTHER",
            "ke": "54321",
            "variant": "BH",
            "page": "1",
        })
        text = response.get_data(as_text=True)
        self.assertEqual(200, response.status_code)
        self.assertIn("Другой BH (54321)", text)
        self.assertNotIn("Комплект PL (12345)</h3>", text)
        self.assertIn('value="Иной"', text)

    def test_preview_and_download_headers_and_payload(self):
        preview = self.client.get(f"/dashboard/release-monitor/document-templates/documents/{self.document_id}/preview")
        download = self.client.get(f"/dashboard/release-monitor/document-templates/documents/{self.document_id}/download")
        self.assertEqual(self.payload, preview.data)
        self.assertEqual(self.payload, download.data)
        self.assertEqual("application/vnd.openxmlformats-officedocument.wordprocessingml.document", preview.mimetype)
        self.assertTrue(preview.headers["Content-Disposition"].startswith("inline"))
        self.assertTrue(download.headers["Content-Disposition"].startswith("attachment"))
        self.assertIn("filename*=UTF-8''", download.headers["Content-Disposition"])
        for response in (preview, download):
            self.assertEqual("no-store", response.headers["Cache-Control"])
            self.assertEqual("nosniff", response.headers["X-Content-Type-Options"])

    def test_malformed_missing_and_path_values_return_404(self):
        values = ("dt1_" + "0" * 64, "..%2Fsecret.docx", "C:%5Csecret.docx", "%5C%5Cserver%5Cshare%5Cfile.docx")
        for value in values:
            with self.subTest(value=value):
                response = self.client.get(f"/dashboard/release-monitor/document-templates/documents/{value}/preview")
                self.assertEqual(404, response.status_code)
                self.assertNotIn(str(self.root), response.get_data(as_text=True))

    def test_document_removed_after_catalog_returns_404(self):
        document = build_document_whitelist(self.root)[self.document_id]
        document.path.unlink()
        response = self.client.get(f"/dashboard/release-monitor/document-templates/documents/{self.document_id}/download")
        self.assertEqual(404, response.status_code)

    def test_feature_flag_no_longer_blocks_routes_and_old_auth_routes_are_gone(self):
        app = build_test_app(self.root, enabled=False)
        client = app.test_client()
        for path in (
            "/dashboard/release-monitor/document-templates",
            f"/dashboard/release-monitor/document-templates/documents/{self.document_id}/preview",
            f"/dashboard/release-monitor/document-templates/documents/{self.document_id}/download",
            f"/dashboard/release-monitor/document-templates/documents/{self.document_id}/history",
        ):
            with self.subTest(path=path):
                self.assertEqual(200, client.get(path).status_code)
        self.assertEqual(404, client.get("/dashboard/release-monitor/document-templates/login").status_code)
        self.assertEqual(404, client.post("/dashboard/release-monitor/document-templates/session/login").status_code)
        self.assertEqual(404, client.post("/dashboard/release-monitor/document-templates/session/logout").status_code)
        self.assertEqual(404, client.get("/admin/document-templates/").status_code)

    def test_public_urls_honor_forwarded_prefix_and_have_no_external_origins(self):
        response = self.client.get("/dashboard/release-monitor/document-templates", headers={"X-Forwarded-Prefix": "/oplot"})
        text = response.get_data(as_text=True)
        self.assertIn('/oplot/static/vendor/tabler/1.4.0/tabler.min.css', text)
        self.assertIn('/oplot/dashboard/release-monitor/document-templates', text)
        parser = _AssetUrlParser()
        parser.feed(text)
        self.assertTrue(parser.urls)
        self.assertFalse([value for value in parser.urls if re.match(r"^(?:https?:)?//", value)])

    def test_script_name_is_used_for_public_urls(self):
        response = self.client.get("/dashboard/release-monitor/document-templates", environ_overrides={"SCRIPT_NAME": "/base"})
        self.assertIn('/base/static/js/document_templates.js', response.get_data(as_text=True))

    def test_missing_vendor_assets_return_diagnostic_503(self):
        missing_static = Path(self.temporary.name) / "empty-static"
        missing_static.mkdir()
        app = build_test_app(self.root, static_folder=missing_static)
        client = app.test_client()
        response = client.get("/dashboard/release-monitor/document-templates")
        self.assertEqual(503, response.status_code)
        self.assertIn("vendor manifest", response.get_data(as_text=True))
        self.assertNotIn(str(missing_static), response.get_data(as_text=True))
        partial = client.get("/dashboard/release-monitor/document-templates", headers={"HX-Request": "true"})
        self.assertEqual(503, partial.status_code)
        self.assertNotIn("<html", partial.get_data(as_text=True).lower())
        self.assertIn("vendor manifest", partial.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
