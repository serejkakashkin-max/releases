from __future__ import annotations

import os
import unittest
from html.parser import HTMLParser
from pathlib import Path
from unittest import mock

from flask import Flask, render_template
from jinja2 import ChoiceLoader, FileSystemLoader

from tests._support import PROJECT_ROOT, prepare_config_import

prepare_config_import()

from services import oplot_ui_service
from services.oplot_ui_service import NavigationItem, build_oplot_navigation, register_oplot_ui


class _UrlParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.urls: list[str] = []

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        for name in ("src", "href", "action", "hx-get", "hx-post"):
            if values.get(name):
                self.urls.append(values[name])


def build_app(*, templates_enabled: bool = True, schedule_manager: bool = False) -> Flask:
    app = Flask(__name__, template_folder=str(PROJECT_ROOT / "templates"), static_folder=str(PROJECT_ROOT / "static"))
    app.config.update(TESTING=True, SECRET_KEY="shell-test", DOCUMENT_TEMPLATE_CENTER_ENABLED=templates_enabled)
    app.jinja_loader = ChoiceLoader([
        app.jinja_loader,
        FileSystemLoader(str(PROJECT_ROOT / "tests" / "fixtures" / "templates")),
    ])
    routes = (
        ("/", "main.index"), ("/help", "main.help_page"), ("/dashboard", "dashboard.dashboard"),
        ("/release-monitor", "dashboard.release_monitor_page"), ("/mpr", "mpr.mpr_page"),
        ("/dashboard/release-monitor/assignment-center", "dashboard.release_monitor_assignment_center_page"),
        ("/dashboard/release-monitor/duty-schedule", "dashboard.release_monitor_duty_schedule_page"),
        ("/admin/sup-parameters", "sup_parameters.sup_parameters_page"),
    )
    for index, (path, endpoint) in enumerate(routes):
        app.add_url_rule(path, endpoint=endpoint, view_func=lambda value=index: str(value))
    if schedule_manager:
        app.add_url_rule("/admin/va/schedule-manager/", endpoint="va_schedule_manager.web.index", view_func=lambda: "schedule")

    @app.get("/shell", endpoint="document_templates.index")
    def fixture():
        return render_template("oplot_shell_fixture.html")

    register_oplot_ui(app)
    return app


def _item(groups, item_id):
    return next(item for group in groups for item in group["items"] if item["id"] == item_id)


class OplotNavigationTests(unittest.TestCase):
    def test_groups_order_ids_and_route_aware_active_state(self):
        app = build_app(schedule_manager=True)
        with app.test_request_context("/admin/document-templates"):
            groups = build_oplot_navigation("document_templates.history")
        self.assertEqual(["main", "documents", "management", "support"], [group["id"] for group in groups])
        self.assertTrue(_item(groups, "document-templates")["active"])
        self.assertFalse(_item(groups, "home")["active"])
        self.assertEqual(1, sum(item["active"] for group in groups for item in group["items"]))
        self.assertEqual("schedule-manager", _item(groups, "schedule-manager")["id"])

    def test_feature_flag_and_missing_endpoint_are_safely_skipped(self):
        app = build_app(templates_enabled=False)
        with app.test_request_context("/"):
            groups = build_oplot_navigation("main.index")
        ids = {item["id"] for group in groups for item in group["items"]}
        self.assertNotIn("document-templates", ids)
        self.assertNotIn("schedule-manager", ids)

    def test_failing_availability_predicate_is_safely_skipped(self):
        failing = NavigationItem("broken", "Broken", "main.index", "help", "support", ("never",), availability=lambda: 1 / 0)
        app = build_app()
        with mock.patch.object(oplot_ui_service, "_NAVIGATION", oplot_ui_service._NAVIGATION + (failing,)):
            with app.test_request_context("/"):
                groups = build_oplot_navigation("main.index")
        self.assertNotIn("broken", {item["id"] for group in groups for item in group["items"]})

    def test_public_urls_honor_all_prefix_sources_without_duplication(self):
        app = build_app()
        cases = (
            ({"headers": {"X-Forwarded-Prefix": "/proxy"}}, "/proxy/"),
            ({"environ_overrides": {"SCRIPT_NAME": "/script"}}, "/script/"),
        )
        for request_options, expected in cases:
            with self.subTest(expected=expected), app.test_request_context("/shell", **request_options):
                self.assertEqual(expected, _item(build_oplot_navigation("main.index"), "home")["url"])
        with mock.patch.dict(os.environ, {"BASE_PATH": "/base"}, clear=False):
            with app.test_request_context("/shell"):
                self.assertEqual("/base/", _item(build_oplot_navigation("main.index"), "home")["url"])
        with app.test_request_context("/shell", headers={"X-Forwarded-Prefix": "/proxy"}):
            self.assertNotIn("/proxy/proxy/", _item(build_oplot_navigation("main.index"), "home")["url"])


class OplotLayoutTests(unittest.TestCase):
    def setUp(self):
        self.app = build_app()
        self.client = self.app.test_client()

    def test_fixture_renders_shell_components_and_active_navigation(self):
        text = self.client.get("/shell").get_data(as_text=True)
        for value in ("oplot-sidebar", "oplot-topbar", "oplot-breadcrumbs", "oplot-operation-indicator", "oplot-toast-region", "fixture-modal", "fixture-confirm"):
            self.assertIn(value, text)
        self.assertIn("Очень длинное русское название", text)
        self.assertIn("Отсутствующий раздел", text)
        self.assertNotIn('href="None"', text)
        self.assertIn('aria-current="page"', text)

    def test_common_layout_has_only_common_assets_and_no_external_origins(self):
        text = self.client.get("/shell", headers={"X-Forwarded-Prefix": "/oplot"}).get_data(as_text=True)
        self.assertIn("/oplot/static/vendor/tabler/1.4.0/tabler.min.css", text)
        self.assertNotIn("htmx.min.js", text)
        self.assertNotIn("jszip.min.js", text)
        self.assertNotIn("docx-preview.min.js", text)
        parser = _UrlParser(); parser.feed(text)
        self.assertTrue(parser.urls)
        self.assertFalse([url for url in parser.urls if url.startswith(("http://", "https://", "//"))])

    def test_theme_bootstrap_precedes_css_and_uses_shared_storage_contract(self):
        text = self.client.get("/shell").get_data(as_text=True)
        self.assertLess(text.index("js/oplot-theme.js"), text.index("tabler.min.css"))
        source = (PROJECT_ROOT / "static" / "js" / "oplot-theme.js").read_text(encoding="utf-8")
        self.assertIn('var STORAGE_KEY = "theme"', source)
        self.assertIn('setAttribute("data-theme"', source)
        self.assertIn('setAttribute("data-bs-theme"', source)
        self.assertIn("oplot:themechange", source)


if __name__ == "__main__":
    unittest.main()
