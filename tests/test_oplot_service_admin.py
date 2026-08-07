from __future__ import annotations

import hashlib
import os
import re
import subprocess
import unittest
from unittest import mock

from flask import Flask

from tests._support import PROJECT_ROOT, prepare_config_import

prepare_config_import()

from routes.sup_admin_session_routes import sup_admin_session_bp
from routes.sup_parameters_routes import sup_parameters_bp
from services.oplot_ui_service import register_oplot_ui
from services.sup_ui_service import build_sup_admin_ui_config


TEMPLATE = PROJECT_ROOT / "templates" / "sup_parameters.html"
CSS = PROJECT_ROOT / "static" / "css" / "oplot_sup_admin.css"
JS = PROJECT_ROOT / "static" / "js" / "oplot_sup_admin.js"
BASE_COMMIT = "a43be38dd87d43ad44a0b19da1b5ecc8e9fc9208"


def build_app(*, with_schedule_manager: bool = False) -> Flask:
    app = Flask(
        __name__,
        template_folder=str(PROJECT_ROOT / "templates"),
        static_folder=str(PROJECT_ROOT / "static"),
    )
    app.config.update(TESTING=True, SECRET_KEY="sup-admin-tests")
    app.add_url_rule("/", endpoint="main.index", view_func=lambda: "home")
    app.add_url_rule(
        "/release-monitor",
        endpoint="dashboard.release_monitor_page",
        view_func=lambda: "release monitor",
    )
    if with_schedule_manager:
        app.add_url_rule(
            "/admin/va/schedule-manager/",
            endpoint="va_schedule_manager.web.index",
            view_func=lambda: "schedule manager",
        )
    app.register_blueprint(sup_admin_session_bp)
    app.register_blueprint(sup_parameters_bp)
    register_oplot_ui(app)
    return app


class SupUiConfigTests(unittest.TestCase):
    def test_prefix_safe_url_map_aliases_and_optional_schedule_manager(self):
        app = build_app(with_schedule_manager=True)
        cases = (
            ({}, "/admin/sup-parameters/data"),
            ({"headers": {"X-Forwarded-Prefix": "/proxy"}}, "/proxy/admin/sup-parameters/data"),
            ({"environ_overrides": {"SCRIPT_NAME": "/script"}}, "/script/admin/sup-parameters/data"),
        )
        empty_env = {
            "BASE_PATH": "",
            "PUBLIC_BASE_PATH": "",
            "APP_BASE_PATH": "",
            "APPLICATION_ROOT": "",
        }
        with mock.patch.dict(os.environ, empty_env, clear=False):
            for options, expected in cases:
                with self.subTest(expected=expected), app.test_request_context(
                    "/admin/sup-parameters", **options
                ):
                    config = build_sup_admin_ui_config(
                        schedule_manager_metadata={"loaded": True, "status": "loaded"}
                    )
                    self.assertEqual(expected, config["urls"]["data"])
                    self.assertEqual("employees", config["default_tab"])
                    self.assertEqual("employees", config["default_view"])
                    self.assertEqual(
                        {"overview": "employees", "automation": "mail"},
                        config["tab_aliases"],
                    )
                    self.assertTrue(config["schedule_manager"]["url"].endswith("/admin/va/schedule-manager/"))

            with mock.patch.dict(os.environ, {"BASE_PATH": "/base"}, clear=False):
                with app.test_request_context("/admin/sup-parameters"):
                    config = build_sup_admin_ui_config()
                    self.assertEqual("/base/admin/sup-parameters/data", config["urls"]["data"])
                with app.test_request_context(
                    "/admin/sup-parameters",
                    environ_overrides={"SCRIPT_NAME": "/base"},
                ):
                    config = build_sup_admin_ui_config()
                    self.assertEqual("/base/admin/sup-parameters/data", config["urls"]["data"])

    def test_builder_is_presentation_only(self):
        source = (PROJECT_ROOT / "services" / "sup_ui_service.py").read_text(encoding="utf-8")
        self.assertIn("public_url_for", source)
        for forbidden in (
            "BASE_PATH",
            "location.hostname",
            "CompetencyRepository",
            "EmployeeSettingsRepository",
            "save_sup_parameters",
            "start_release_monitor_refresh",
        ):
            self.assertNotIn(forbidden, source)


class SupShellContractTests(unittest.TestCase):
    def setUp(self):
        self.app = build_app()

    def test_shell_tabs_assets_and_workflow_hooks(self):
        with mock.patch(
            "routes.sup_parameters_routes.get_va_schedule_manager_metadata",
            return_value={"status": "disabled", "loaded": False},
        ):
            response = self.app.test_client().get("/admin/sup-parameters")
        self.assertEqual(200, response.status_code)
        text = response.get_data(as_text=True)
        self.assertIn("oplot-shell oplot-shell--no-sidebar", text)
        self.assertIn("oplot-topbar--core", text)
        self.assertIn("oplot-sup-admin", text)
        self.assertEqual(1, len(re.findall(r"<h1(?:\s|>)", text)))
        self.assertNotIn("oplot-breadcrumbs", text)
        self.assertIn('href="/"', text)
        self.assertIn("data-oplot-theme-toggle", text)
        self.assertNotIn("oplot-home-utility", text)
        self.assertIn("css/oplot_sup_admin.css", text)
        self.assertIn("js/oplot_sup_admin.js", text)
        self.assertIn("defer", text)
        self.assertIn('type="application/json" id="oplot-sup-admin-config"', text)

        tabs = re.findall(r'data-tab="([^"]+)"', text)
        self.assertEqual(
            [
                "maintenance",
                "employees",
                "release-refresh",
                "mail",
                "prefixes",
                "sbertrack",
                "tools",
                "diagnostics",
            ],
            tabs,
        )
        for element_id in (
            "statusBox",
            "tokenBox",
            "appBox",
            "saveBtn",
            "reloadBtn",
            "releaseRefreshStateBadge",
            "directoryEmployeeList",
            "modalBackdrop",
            "competencyModalBackdrop",
        ):
            self.assertIn(f'id="{element_id}"', text)

    def test_template_has_no_legacy_shell_or_inline_business_assets(self):
        source = TEMPLATE.read_text(encoding="utf-8")
        self.assertIn("extends 'layouts/oplot_base.html'", source)
        self.assertIn("oplot_show_sidebar = false", source)
        self.assertIn("oplot_topbar_variant = 'core'", source)
        self.assertNotIn("<!doctype html>", source.lower())
        self.assertNotRegex(source, r"<style\b")
        self.assertNotIn("location.hostname", source)
        self.assertNotIn("BASE_PATH", source)
        executable = re.findall(
            r'<script(?![^>]*type="application/json")[^>]*>(.*?)</script>',
            source,
            re.S,
        )
        self.assertFalse(any(block.strip() for block in executable))

    def test_css_is_scoped_and_uses_oplot_dna(self):
        css = CSS.read_text(encoding="utf-8")
        self.assertIn("var(--oplot-dna-canvas)", css)
        self.assertIn("var(--oplot-dna-panel)", css)
        self.assertIn("prefers-reduced-motion", css)
        self.assertIn("overflow-x: hidden", css)
        theme_scoped = re.compile(
            r'^html\[data-theme="(?:light|dark)"\]\s+\.oplot-sup-admin(?:\b|\s|[.:#\[])'
        )
        for line in css.splitlines():
            stripped = line.strip()
            if stripped.endswith("{") and not (
                stripped.startswith((".oplot-sup-admin", "@media", "@keyframes"))
                or theme_scoped.match(stripped)
            ):
                self.fail(f"Unscoped selector: {stripped}")
        self.assertNotRegex(
            css,
            r"(?m)^\s*(?::root|body|\[data-theme|\.card|\.btn|\.modal|\.table|\.form-control|input|select|textarea|button|label)\b",
        )


class SupJavascriptContractTests(unittest.TestCase):
    def test_config_polling_query_auth_and_payload_contracts(self):
        script = JS.read_text(encoding="utf-8")
        self.assertIn('"use strict"', script)
        self.assertIn("function initOplotSupAdminPage", script)
        self.assertIn('dataset.oplotSupAdminInitialized === "true"', script)
        self.assertIn("JSON.parse", script)
        self.assertIn("new URL(value, window.location.origin)", script)
        self.assertIn("root.inert = true", script)
        self.assertNotIn("location.hostname", script)
        self.assertNotIn("BASE_PATH", script)
        self.assertIn('headers.set("X-SUP-Admin-Token", getToken())', script)
        self.assertIn('headers.set("X-CSRF-Token", csrfToken)', script)
        self.assertIn("nextDelay = payload.refresh?.state === \"refreshing\" ? 2000 : 15000", script)
        self.assertIn("Math.min(60000", script)
        self.assertIn('body: JSON.stringify({ mode })', script)
        self.assertIn('JSON.stringify({ revision: state.revision, config: collectConfig() })', script)
        for key in ("expected_revision", "expected_etag", "employees", "directory_etag", "settings_revision", "settings_etag", "competency"):
            self.assertIn(key, script)
        for query_key in ('params.get("tab")', 'params.get("view")', 'params.get("token")'):
            self.assertIn(query_key, script)

    def test_source_characterization_preserves_original_named_functions(self):
        try:
            original = subprocess.check_output(
                ["git", "show", f"{BASE_COMMIT}:templates/sup_parameters.html"],
                cwd=PROJECT_ROOT,
            ).decode("utf-8")
        except (OSError, subprocess.CalledProcessError):
            self.skipTest("baseline commit unavailable")
        block = original.split("<script>", 1)[1].split("</script>", 1)[0]
        self.assertEqual(
            "25b51d2e2ccf6f598d6a9dea6d7da01fcf60c0f1140d97ad0185a862f3db9e16",
            hashlib.sha256(block.encode("utf-8")).hexdigest(),
        )
        old_functions = set(
            re.findall(r"(?m)^\s*(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(", block)
        )
        new_functions = set(
            re.findall(r"(?m)^\s*(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(", JS.read_text(encoding="utf-8"))
        )
        self.assertEqual(107, len(old_functions))
        self.assertTrue(old_functions.issubset(new_functions))


class SupAuthContractTests(unittest.TestCase):
    def setUp(self):
        self.app = build_app()
        self.client = self.app.test_client()

    def test_legacy_token_get_and_save_contracts_remain_independent(self):
        self.assertEqual(403, self.client.get("/admin/sup-parameters/data").status_code)
        with (
            mock.patch("routes.sup_parameters_routes._configured_token", return_value="secret"),
            mock.patch("routes.sup_parameters_routes.get_sup_parameters_data", return_value={"success": True}),
        ):
            response = self.client.get(
                "/admin/sup-parameters/data", headers={"X-SUP-Admin-Token": "secret"}
            )
            self.assertEqual(200, response.status_code)
        with (
            mock.patch("routes.sup_parameters_routes._configured_token", return_value="secret"),
            mock.patch("routes.sup_parameters_routes.save_sup_parameters", return_value={"success": True}) as save,
        ):
            response = self.client.post(
                "/admin/sup-parameters/save",
                headers={"X-SUP-Admin-Token": "secret"},
                json={"config": {"maintenance": {}}, "revision": "rev-1"},
            )
            self.assertEqual(200, response.status_code)
            save.assert_called_once_with({"maintenance": {}}, "rev-1")

    def test_session_guard_and_csrf_guard_remain_on_va_mutations(self):
        self.assertEqual(
            403,
            self.client.get(
                "/admin/sup-parameters/va-schedule-manager",
                headers={"Accept": "application/json"},
            ).status_code,
        )
        with mock.patch(
            "routes.sup_parameters_routes.require_sup_admin_request", return_value=None
        ):
            response = self.client.put(
                "/admin/sup-parameters/va-schedule-manager/employees/user/settings",
                json={
                    "directory_etag": "d",
                    "settings_revision": 1,
                    "settings_etag": "s",
                    "settings": {
                        "status": "active",
                        "role": "member",
                        "competencies": [],
                        "overtime_ready": False,
                    },
                },
            )
        self.assertEqual(403, response.status_code)
        self.assertIn("CSRF", response.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
