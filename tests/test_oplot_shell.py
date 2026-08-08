from __future__ import annotations

import os
import re
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


def build_app(*, schedule_manager: bool = False) -> Flask:
    app = Flask(__name__, template_folder=str(PROJECT_ROOT / "templates"), static_folder=str(PROJECT_ROOT / "static"))
    app.config.update(TESTING=True, SECRET_KEY="shell-test")
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
        with app.test_request_context("/dashboard/release-monitor/document-templates"):
            groups = build_oplot_navigation("document_templates.history")
        self.assertEqual(["main", "documents", "management", "support"], [group["id"] for group in groups])
        self.assertTrue(_item(groups, "release-monitor")["active"])
        self.assertNotIn("document-templates", {item["id"] for group in groups for item in group["items"]})
        self.assertFalse(_item(groups, "home")["active"])
        self.assertEqual(1, sum(item["active"] for group in groups for item in group["items"]))
        self.assertEqual("schedule-manager", _item(groups, "schedule-manager")["id"])

    def test_missing_optional_endpoint_is_safely_skipped(self):
        app = build_app()
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
        self.assertNotIn("oplot-shell--no-sidebar", text)
        self.assertNotIn("oplot-topbar--compact", text)
        self.assertNotIn("oplot-topbar--core", text)
        self.assertIn('class="oplot-topbar__context"', text)
        self.assertIn("oplot-page-header", text)
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
        self.assertIn('classList.add("oplot-theme-switching")', source)
        self.assertIn('classList.remove("oplot-theme-switching")', source)
        self.assertIn("requestAnimationFrame", source)
        common_css = (PROJECT_ROOT / "static" / "css" / "oplot.css").read_text(encoding="utf-8")
        self.assertIn("html.oplot-theme-switching .oplot-body *", common_css)
        self.assertIn("transition: none !important", common_css)

    def test_theme_toggle_relies_on_single_themechange_sync(self):
        source = (PROJECT_ROOT / "static" / "js" / "oplot.js").read_text(encoding="utf-8")
        click_handler = re.search(
            r'button\.addEventListener\("click", function \(\) \{(.*?)\n\s*\}\);',
            source,
            re.S,
        )
        self.assertIsNotNone(click_handler)
        self.assertIn("window.OplotTheme.toggle()", click_handler.group(1))
        self.assertNotIn("syncThemeControls()", click_handler.group(1))
        self.assertEqual(1, source.count('document.addEventListener("oplot:themechange", syncThemeControls)'))

    def test_stage9_assets_are_profiled_without_duplicates(self):
        def render_profile(profile: str, *, duty: bool = False) -> str:
            with self.app.test_request_context("/shell"):
                return render_template(
                    "oplot_shell_fixture.html",
                    oplot_stage9_profile=profile,
                    oplot_stage9_duty=duty,
                )

        expected = {
            "data": ("oplot_stage9_dna.css", "oplot_stage9_refinement.css"),
            "home": ("oplot_stage9_dna.css", "oplot_stage9_home.css"),
            "none": ("oplot_stage9_dna.css",),
        }
        for profile, assets in expected.items():
            with self.subTest(profile=profile):
                text = render_profile(profile)
                stage9_assets = re.findall(r"css/(oplot_stage9_[^\"?]+\.css)", text)
                self.assertEqual(list(assets), stage9_assets)
                self.assertEqual(len(stage9_assets), len(set(stage9_assets)))
                self.assertNotIn("oplot_stage9_duty.css", stage9_assets)

        duty_text = render_profile("data", duty=True)
        duty_assets = re.findall(r"css/(oplot_stage9_[^\"?]+\.css)", duty_text)
        self.assertEqual(
            ["oplot_stage9_dna.css", "oplot_stage9_refinement.css", "oplot_stage9_duty.css"],
            duty_assets,
        )

    def test_stage9_canvas_and_large_surface_performance_contract(self):
        dna = (PROJECT_ROOT / "static" / "css" / "oplot_stage9_dna.css").read_text(encoding="utf-8")
        home = (PROJECT_ROOT / "static" / "css" / "oplot_stage9_home.css").read_text(encoding="utf-8")
        release = (PROJECT_ROOT / "static" / "css" / "oplot_release.css").read_text(encoding="utf-8")
        self.assertIn(".oplot-body .oplot-shell", dna)
        self.assertIn(".oplot-body .oplot-page", dna)
        self.assertRegex(dna, r"\.oplot-body \.oplot-page\s*\{[^}]*background:\s*transparent")
        self.assertNotIn(".oplot-body:not(.oplot-home) .oplot-page", dna)
        self.assertIn("html[data-theme=\"dark\"] .oplot-body.oplot-home", home)
        home_assistant_rules = re.findall(r"\.oplot-home-assistant\s*\{([^}]*)\}", home)
        self.assertTrue(home_assistant_rules)
        self.assertTrue(any("backdrop-filter: none" in rule for rule in home_assistant_rules))
        self.assertRegex(release, r"\.oplot-release \.oplot-page\s*\{[^}]*background:\s*transparent")
        for selector in (".oplot-release .oplot-page__header", ".oplot-release .oplot-release-menu", ".oplot-release .release-monitor-section"):
            rules = re.findall(re.escape(selector) + r"\s*\{([^}]*)\}", release)
            self.assertTrue(rules, selector)
            self.assertTrue(any("backdrop-filter: none" in rule for rule in rules), selector)

    def test_shared_dna_keeps_text_selection_visible_in_both_themes(self):
        dna = (PROJECT_ROOT / "static" / "css" / "oplot_stage9_dna.css").read_text(encoding="utf-8")
        self.assertIn("--oplot-selection-bg: #1769c2", dna)
        self.assertIn("--oplot-selection-bg: #63b7f5", dna)
        self.assertIn("--oplot-selection-text: #06111e", dna)
        selection_rule = re.search(r"\.oplot-body \*::selection \{([^}]*)\}", dna)
        self.assertIsNotNone(selection_rule)
        self.assertIn("background: var(--oplot-selection-bg)", selection_rule.group(1))
        self.assertIn("color: var(--oplot-selection-text)", selection_rule.group(1))

    def test_data_pages_reuse_shared_canvas_and_avoid_large_backdrops(self):
        page_roots = {
            "document_templates.css": ".oplot-dtc",
            "dashboard.css": ".oplot-duty-dashboard",
            "release_assignment_center.css": ".oplot-assignment-center",
            "release_monitor_duty_schedule.css": ".oplot-duty-schedule",
            "oplot_mpr.css": ".oplot-mpr",
            "oplot_current_week.css": ".oplot-current-week",
            "oplot_sup_admin.css": ".oplot-sup-admin",
        }
        sources = {}
        for filename, selector in page_roots.items():
            source = (PROJECT_ROOT / "static" / "css" / filename).read_text(encoding="utf-8")
            sources[filename] = source
            rules = re.findall(re.escape(selector) + r"\s*\{([^}]*)\}", source)
            self.assertTrue(rules, selector)
            self.assertTrue(any("background: transparent" in rule for rule in rules), selector)

        dtc = sources["document_templates.css"]
        self.assertRegex(dtc, r"\.oplot-dtc \.oplot-page\s*\{[^}]*background:\s*transparent")
        for selector in (
            ".oplot-dtc .oplot-page-header",
            ".oplot-dtc .oplot-dtc-filters",
            ".oplot-dtc .template-kit-card",
        ):
            rule = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", dtc)
            self.assertIsNotNone(rule, selector)
            self.assertNotIn("backdrop-filter", rule.group(1), selector)

        current_week = sources["oplot_current_week.css"]
        table_header = re.search(r"\.oplot-current-week th\s*\{([^}]*)\}", current_week)
        self.assertIsNotNone(table_header)
        self.assertNotIn("backdrop-filter", table_header.group(1))

        sup = sources["oplot_sup_admin.css"]
        self.assertRegex(sup, r"\.oplot-sup-admin \.oplot-page\s*\{[^}]*background:\s*transparent")


if __name__ == "__main__":
    unittest.main()
