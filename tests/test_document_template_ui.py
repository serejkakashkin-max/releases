from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path

from tests._support import create_docx, prepare_config_import

prepare_config_import()

from services.document_template_csrf_service import (
    CSRF_COOKIE_NAME,
    DOCUMENT_TEMPLATE_ACTOR,
)
from services.document_template_read_service import build_document_whitelist
from services.release_template_catalog_service import clear_template_catalog_cache
from services.release_ui_service import build_release_navigation
from tests.test_document_template_routes import build_test_app


class DocumentTemplateUiTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "doc_templates"
        create_docx(
            self.root / "CAT" / "Комплект PL (12345)" / "Очень длинное имя рабочего шаблона для проверки переноса.docx"
        )
        clear_template_catalog_cache()
        self.app = build_test_app(self.root, enabled=False)
        self.app.add_url_rule(
            "/dashboard/release-monitor/duty-schedule",
            endpoint="dashboard.release_monitor_duty_schedule_page",
            view_func=lambda: "duty",
        )
        self.app.add_url_rule(
            "/dashboard/release-monitor/assignment-center",
            endpoint="dashboard.release_monitor_assignment_center_page",
            view_func=lambda: "assignment",
        )
        self.app.add_url_rule(
            "/dashboard/release-monitor/current-week",
            endpoint="dashboard.current_week_release_monitor_page",
            view_func=lambda: "week",
        )
        self.client = self.app.test_client()
        self.document_id = next(iter(build_document_whitelist(self.root)))

    def tearDown(self):
        clear_template_catalog_cache()
        self.temporary.cleanup()

    def test_catalog_uses_core_shell_without_sidebar_or_auth_controls(self):
        response = self.client.get("/dashboard/release-monitor/document-templates/")
        html = response.get_data(as_text=True)
        self.assertEqual(200, response.status_code)
        self.assertIn("oplot-topbar-variant-core", html)
        self.assertIn("oplot-shell--no-sidebar", html)
        self.assertIn("oplot-dtc oplot-dtc--catalog", html)
        self.assertNotIn("oplot-sidebar", html)
        self.assertNotIn("Текущий пользователь", html)
        self.assertNotIn("Выйти", html)
        self.assertEqual(1, len(re.findall(r"<h1\b", html)))
        self.assertIn("Центр шаблонов", html)
        self.assertIn('href="/"', html)
        self.assertIn("К блоку релизов", html)
        self.assertLess(html.index("К блоку релизов"), html.index("data-oplot-theme-toggle"))
        self.assertNotIn("oplot-breadcrumbs", html)
        self.assertNotIn("Active root защищён", html)
        self.assertNotIn("/admin/document-templates/", html)

    def test_catalog_preserves_document_action_order_and_viewer_assets(self):
        html = self.client.get("/dashboard/release-monitor/document-templates/").get_data(as_text=True)
        positions = [html.index(label) for label in ("Просмотреть", "Скачать", "Заменить", "История")]
        self.assertEqual(positions, sorted(positions))
        self.assertIn("htmx.min.js", html)
        self.assertIn("jszip.min.js", html)
        self.assertIn("docx-preview.min.js", html)
        self.assertIn("overflow-wrap: anywhere", (Path(__file__).parents[1] / "static/css/document_templates.css").read_text(encoding="utf-8"))
        self.assertNotIn("publish-confirm-modal", html)
        self.assertNotIn(">Опубликовать<", html)
        self.assertIn("Проверить и заменить", html)
        self.assertIn('id="oplot-dtc-variants-map"', html)

    def test_dtc_css_and_filter_script_keep_modal_and_variant_layout_stable(self):
        css = (Path(__file__).parents[1] / "static/css/document_templates.css").read_text(encoding="utf-8")
        script = (Path(__file__).parents[1] / "static/js/document_templates.js").read_text(encoding="utf-8")
        self.assertIn("scrollbar-gutter: stable", css)
        self.assertIn(".oplot-dtc.modal-open", css)
        self.assertRegex(css, r"\.oplot-dtc\.modal-open\s*\{[^}]*padding-right:\s*0\s*!important")
        self.assertIn("--oplot-bg: #030b1a", css)
        self.assertIn("rgba(46, 116, 255, .18)", css)
        self.assertIn("rgba(0, 201, 190, .13)", css)
        self.assertIn("--oplot-dtc-table-head", css)
        self.assertIn("syncVariantFilter", script)
        self.assertIn('event.target.id === "category-filter"', script)

    def test_csrf_form_cookie_identity_header_support_and_secure_policy(self):
        response = self.client.get("/dashboard/release-monitor/document-templates/")
        html = response.get_data(as_text=True)
        form_token = re.search(r'name="_csrf_token" value="([A-Za-z0-9_-]+)"', html).group(1)
        cookie = self.client.get_cookie(CSRF_COOKIE_NAME, path="/dashboard/release-monitor/document-templates/")
        self.assertEqual(form_token, cookie.value)
        rejected = self.client.post(
            f"/dashboard/release-monitor/document-templates/documents/{self.document_id}/candidates",
            data={"_csrf_token": "wrong"},
        )
        self.assertEqual(403, rejected.status_code)
        accepted_guard = self.client.post(
            f"/dashboard/release-monitor/document-templates/documents/{self.document_id}/candidates",
            headers={"X-CSRF-Token": form_token},
        )
        self.assertEqual(400, accepted_guard.status_code)

        secure_app = build_test_app(self.root)
        secure_app.config["SESSION_COOKIE_SECURE"] = True
        secure_header = secure_app.test_client().get("/dashboard/release-monitor/document-templates/").headers.get("Set-Cookie", "")
        self.assertIn("Secure", secure_header)
        self.assertIn("HttpOnly", secure_header)
        self.assertIn("SameSite=Lax", secure_header)
        self.assertIn("Path=/dashboard/release-monitor/document-templates/", secure_header)

    def test_release_navigation_keeps_templates_fourth_even_with_old_flag_false(self):
        with self.app.test_request_context("/release-monitor"):
            items = build_release_navigation("dashboard.release_monitor_page")
        self.assertEqual(
            ["duty-schedule", "assignment-center", "current-week", "document-templates"],
            [item["id"] for item in items],
        )

    def test_actor_is_centralized_and_old_auth_endpoints_are_absent(self):
        self.assertEqual("Пользователь Oplot", DOCUMENT_TEMPLATE_ACTOR)
        self.assertEqual(404, self.client.get("/dashboard/release-monitor/document-templates/login").status_code)
        self.assertEqual(404, self.client.post("/dashboard/release-monitor/document-templates/session/login").status_code)
        self.assertEqual(404, self.client.post("/dashboard/release-monitor/document-templates/session/logout").status_code)
        self.assertEqual(404, self.client.get("/admin/document-templates/").status_code)

    def test_operational_configuration_error_uses_the_same_core_shell(self):
        missing = Path(self.temporary.name) / "missing-root"
        app = build_test_app(missing)
        response = app.test_client().get("/dashboard/release-monitor/document-templates/")
        html = response.get_data(as_text=True)
        self.assertEqual(503, response.status_code)
        self.assertIn("oplot-dtc--configuration-error", html)
        self.assertIn("oplot-shell--no-sidebar", html)
        self.assertIn("oplot-topbar-variant-core", html)
        self.assertEqual(1, len(re.findall(r"<h1\b", html)))
        self.assertNotIn(str(missing), html)


if __name__ == "__main__":
    unittest.main()
