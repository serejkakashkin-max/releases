import json
import re
import unittest
from pathlib import Path

from flask import Flask

from tests._support import prepare_config_import

prepare_config_import()

from routes.sup_parameters_routes import sup_parameters_bp
from services.oplot_ui_service import public_url_for
from services.sup_ui_service import build_sup_admin_ui_config


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "templates" / "sup_parameters.html"
CSS = ROOT / "static" / "css" / "oplot_sup_admin.css"
JS = ROOT / "static" / "js" / "oplot_sup_admin.js"


class SupUiConfigTests(unittest.TestCase):
    def _app(self):
        app = Flask(__name__)
        app.secret_key = "test"
        app.register_blueprint(sup_parameters_bp)
        return app

    def test_prefix_safe_url_map_aliases_and_optional_schedule_manager(self):
        app = self._app()
        with app.test_request_context(
            "/admin/sup-parameters",
            environ_base={"SCRIPT_NAME": "/releases"},
        ):
            config = build_sup_admin_ui_config(schedule_manager={"available": True})
        self.assertEqual(config["default_tab"], "maintenance")
        self.assertEqual(config["default_view"], "employees")
        self.assertEqual(config["tab_aliases"]["overview"], "employees")
        self.assertEqual(config["tab_aliases"]["automation"], "mail")
        self.assertTrue(all(url.startswith("/releases/") for url in config["urls"].values()))
        self.assertTrue(config["schedule_manager"]["available"])

    def test_builder_is_presentation_only(self):
        source = (ROOT / "services" / "sup_ui_service.py").read_text(encoding="utf-8")
        self.assertIn("public_url_for", source)
        self.assertNotIn("open(", source)
        self.assertNotIn("save_", source)
        self.assertNotIn("require_sup_admin", source)
        self.assertNotIn("csrf_protect", source)


class SupAuthContractTests(unittest.TestCase):
    def test_legacy_token_get_and_save_contracts_remain_independent(self):
        source = (ROOT / "routes" / "sup_parameters_routes.py").read_text(encoding="utf-8")
        self.assertIn("X-SUP-Admin-Token", source)
        self.assertIn("sup_admin_token", source)
        self.assertIn("expected_revision", source)
        self.assertIn("409", source)

    def test_session_guard_and_csrf_guard_remain_on_va_mutations(self):
        source = (ROOT / "routes" / "sup_parameters_routes.py").read_text(encoding="utf-8")
        self.assertIn("require_sup_admin_request", source)
        self.assertIn("csrf_protect_request", source)
        self.assertIn("X-CSRF-Token", JS.read_text(encoding="utf-8"))


class SupShellContractTests(unittest.TestCase):
    def test_shell_tabs_assets_and_workflow_hooks(self):
        source = TEMPLATE.read_text(encoding="utf-8")
        self.assertIn("extends 'layouts/oplot_base.html'", source)
        self.assertIn("oplot_show_sidebar = false", source)
        self.assertIn("oplot_topbar_variant = 'core'", source)
        self.assertIn("oplot-sup-admin", source)
        self.assertEqual(source.count("<h1"), 1)
        for tab_id in (
            "maintenance",
            "employees",
            "release-refresh",
            "mail",
            "prefixes",
            "sbertrack",
            "tools",
            "diagnostics",
        ):
            self.assertIn(f'data-tab="{tab_id}"', source)
        self.assertIn("oplot_sup_admin.css", source)
        self.assertIn("oplot_sup_admin.js", source)
        self.assertIn("oplot-sup-admin-config", source)

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
        self.assertIn("15000", script)
        self.assertIn("2000", script)
        self.assertIn("60000", script)
        self.assertIn("X-SUP-Admin-Token", script)
        self.assertIn("X-CSRF-Token", script)
        self.assertIn("expected_revision", script)
        self.assertIn("expected_etag", script)
        self.assertIn("directory_etag", script)
        self.assertIn("settings_revision", script)
        self.assertIn("settings_etag", script)

    def test_source_characterization_preserves_original_named_functions(self):
        script = JS.read_text(encoding="utf-8")
        expected = {
            "applyConfig",
            "buildPayload",
            "loadConfig",
            "saveConfig",
            "resetChanges",
            "loadEmployeeDirectory",
            "saveEmployeeDirectory",
            "loadReleaseRefreshStatus",
        }
        names = set(re.findall(r"function\s+([A-Za-z0-9_$]+)\s*\(", script))
        self.assertTrue(expected.issubset(names))


if __name__ == "__main__":
    unittest.main()
