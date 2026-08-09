from __future__ import annotations

import json
import re
import unittest
from unittest import mock

from flask import Flask

from tests._support import PROJECT_ROOT, prepare_config_import

prepare_config_import()

from routes.dashboard_routes import dashboard_bp
from services.duty_ui_service import (
    build_assignment_center_ui_config,
    build_duty_dashboard_ui_config,
)
from services.oplot_ui_service import register_oplot_ui


DASHBOARD_TEMPLATE = PROJECT_ROOT / "templates" / "dashboard.html"
ASSIGNMENT_TEMPLATE = PROJECT_ROOT / "templates" / "release_assignment_center.html"
SCHEDULE_TEMPLATE = PROJECT_ROOT / "templates" / "release_monitor_duty_schedule.html"
DASHBOARD_JS = PROJECT_ROOT / "static" / "js" / "oplot_duty_dashboard.js"
ASSIGNMENT_JS = PROJECT_ROOT / "static" / "js" / "release_assignment_center.js"
SCHEDULE_JS = PROJECT_ROOT / "static" / "js" / "release_monitor_duty_schedule.js"


def build_app(config: dict | None = None) -> Flask:
    app = Flask(
        __name__,
        template_folder=str(PROJECT_ROOT / "templates"),
        static_folder=str(PROJECT_ROOT / "static"),
    )
    app.config.update(TESTING=True, SECRET_KEY="duty-ui-tests")
    if config:
        app.config.update(config)
    app.add_url_rule("/", endpoint="main.index", view_func=lambda: "home")
    app.register_blueprint(dashboard_bp)
    register_oplot_ui(app)
    return app


class DutyPresentationConfigTests(unittest.TestCase):
    def test_configs_use_registered_endpoints_and_preserve_settings(self):
        app = build_app()
        with app.test_request_context("/dashboard"):
            dashboard = build_duty_dashboard_ui_config(hidden_tasks={"SUP-1": {}})
            assignment = build_assignment_center_ui_config()
        self.assertEqual(3_600_000, dashboard["settings"]["page_reload_ms"])
        self.assertEqual(1_000, dashboard["settings"]["approval_delay_ms"])
        self.assertEqual(15_000, assignment["settings"]["poll_interval_ms"])
        self.assertTrue(assignment["settings"]["gigachat_enabled"])
        self.assertEqual("/dashboard/refresh", dashboard["urls"]["refresh"])
        self.assertEqual("/dashboard/release-monitor/assignment-center/data", assignment["urls"]["data"])
        self.assertEqual({"SUP-1": {}}, dashboard["data"]["hidden_tasks"])

    def test_configs_are_prefix_safe_without_double_prefix(self):
        cases = (
            ({}, "/dashboard/refresh"),
            ({"headers": {"X-Forwarded-Prefix": "/proxy"}}, "/proxy/dashboard/refresh"),
            ({"environ_overrides": {"SCRIPT_NAME": "/script"}}, "/script/dashboard/refresh"),
        )
        app = build_app()
        for request_options, expected in cases:
            with self.subTest(expected=expected), app.test_request_context("/dashboard", **request_options):
                self.assertEqual(expected, build_duty_dashboard_ui_config()["urls"]["refresh"])


class DutyTemplateContractTests(unittest.TestCase):
    def test_shell_contracts_and_page_specific_assets(self):
        dashboard = DASHBOARD_TEMPLATE.read_text(encoding="utf-8")
        assignment = ASSIGNMENT_TEMPLATE.read_text(encoding="utf-8")
        schedule = SCHEDULE_TEMPLATE.read_text(encoding="utf-8")
        for source, body_class in (
            (dashboard, "oplot-duty-dashboard"),
            (assignment, "oplot-assignment-center"),
            (schedule, "oplot-duty-schedule"),
        ):
            self.assertIn("extends 'layouts/oplot_base.html'", source)
            self.assertIn("oplot_show_sidebar = false", source)
            self.assertIn("oplot_topbar_variant = 'core'", source)
            self.assertIn("oplot_show_breadcrumbs = false", source)
            self.assertIn(body_class, source)
            self.assertNotIn("base_styles.html", source)
            self.assertNotIn("bootstrap.bundle", source)
            self.assertNotRegex(source, r"https?://|//cdn")
        self.assertNotIn("duty/_topbar_back.html", dashboard)
        self.assertIn("duty/_topbar_back.html", assignment)
        self.assertIn("duty/_topbar_back.html", schedule)

    def test_dashboard_business_contract_is_external_and_endpoint_backed(self):
        template = DASHBOARD_TEMPLATE.read_text(encoding="utf-8")
        script = DASHBOARD_JS.read_text(encoding="utf-8")
        self.assertIn("defer", template)
        self.assertIn("oplot_duty_dashboard.js", template)
        self.assertIn("oplot-duty-dashboard-config", template)
        self.assertNotIn("location.hostname", script)
        self.assertNotIn("BASE_PATH +", script)
        self.assertNotIn("{{", script)
        self.assertIn("window.initOplotDutyDashboard", script)
        self.assertIn("initializationState !== 'not_started'", script)
        for handler in (
            "openTrashPanel", "closeTrashPanel", "restoreAllTasks", "hideTask",
            "refreshData", "handleSearch", "handleSort", "toggleColumn",
        ):
            self.assertRegex(script, rf"\b{handler},")
        for interval in ("3600000", "1000"):
            self.assertIn(interval, script)
        self.assertIn("trashReturnFocus", script)

    def test_assignment_and_schedule_keep_behavioral_constants(self):
        assignment = ASSIGNMENT_JS.read_text(encoding="utf-8")
        schedule = SCHEDULE_JS.read_text(encoding="utf-8")
        self.assertIn("15000", assignment)
        self.assertIn("releaseAssignmentCenterNotifications", assignment)
        self.assertIn("config.urls.reviewer", assignment)
        self.assertIn("window.initOplotAssignmentCenter", assignment)
        self.assertIn("GIGACHAT_ENABLED", assignment)
        self.assertIn("Ручное назначение остаётся доступно", assignment)
        self.assertNotIn("BASE_PATH", assignment)
        self.assertIn("window.initOplotDutySchedule", schedule)
        self.assertIn("availableScheduleMonths", schedule)
        self.assertIn("data-autoplan-hint", SCHEDULE_TEMPLATE.read_text(encoding="utf-8"))
        self.assertNotIn("localStorage.getItem('theme')", schedule)

    def test_css_is_namespaced_and_contains_local_scroll_contracts(self):
        common_css = (PROJECT_ROOT / "static" / "css" / "oplot.css").read_text(encoding="utf-8")
        for token in (
            "--oplot-dna-canvas-gradient",
            "--oplot-dna-panel-gradient",
            "--oplot-dna-panel-gradient-strong",
            "--oplot-dna-line",
            "--oplot-dna-blue",
            "--oplot-dna-cyan",
            "--oplot-dna-violet",
        ):
            self.assertIn(token, common_css)
        checks = (
            ("dashboard.css", ".oplot-duty-dashboard"),
            ("release_assignment_center.css", ".oplot-assignment-center"),
            ("release_monitor_duty_schedule.css", ".oplot-duty-schedule"),
        )
        for filename, namespace in checks:
            source = (PROJECT_ROOT / "static" / "css" / filename).read_text(encoding="utf-8")
            self.assertIn(namespace, source)
            self.assertIn("@scope", source)
            self.assertIn("var(--oplot-dna-", source)
        schedule = (PROJECT_ROOT / "static" / "css" / "release_monitor_duty_schedule.css").read_text(encoding="utf-8")
        self.assertIn("overflow:auto", schedule.replace(" ", ""))
        self.assertIn("position:sticky", schedule.replace(" ", ""))

    def test_schedule_legend_is_horizontal_wrapping_and_atomic(self):
        schedule_css = (PROJECT_ROOT / "static" / "css" / "release_monitor_duty_schedule.css").read_text(encoding="utf-8")
        compact_css = re.sub(r"\s+", "", schedule_css)
        legend_rule = re.search(r"\.oplot-duty-schedule\.legend\{([^}]*)\}", compact_css)
        item_rule = re.search(r"\.oplot-duty-schedule\.legend-item\{([^}]*)\}", compact_css)
        self.assertIsNotNone(legend_rule)
        self.assertIsNotNone(item_rule)
        self.assertIn("display:flex", legend_rule.group(1))
        self.assertIn("flex-direction:row", legend_rule.group(1))
        self.assertIn("flex-wrap:wrap", legend_rule.group(1))
        self.assertIn("width:100%", legend_rule.group(1))
        self.assertIn("height:auto", legend_rule.group(1))
        self.assertNotIn("flex-direction:column", legend_rule.group(1))
        self.assertIn("display:inline-flex", item_rule.group(1))
        self.assertIn("flex:00auto", item_rule.group(1))
        self.assertIn("white-space:nowrap", item_rule.group(1))
        self.assertNotIn("overflow-x", item_rule.group(1))
        schedule_root = re.search(r"\.oplot-duty-schedule\{([^}]*)\}", compact_css)
        self.assertIsNotNone(schedule_root)
        self.assertIn("overflow-x:hidden", schedule_root.group(1))
        self.assertIn(".oplot-duty-schedule .legend-item span:last-child { display: inline; }", schedule_css)
        template = SCHEDULE_TEMPLATE.read_text(encoding="utf-8")
        self.assertIn("shift.short_name or shift.code", template)
        self.assertIn("{{ shift.name }}", template)
        self.assertNotRegex(template, r"(?i)<(?:input|button)[^>]+(?:save|publish|delete)")


class DutyRouteSmokeTests(unittest.TestCase):
    def setUp(self):
        self.app = build_app()
        self.client = self.app.test_client()

    def _patches(self):
        return (
            mock.patch("routes.dashboard_routes.get_dashboard_data", return_value={}),
            mock.patch("routes.dashboard_routes.get_hidden_tasks", return_value={}),
            mock.patch("routes.dashboard_routes.prune_hidden_tasks"),
            mock.patch("routes.dashboard_routes.get_dashboard_primary_display_names", return_value=[]),
            mock.patch("routes.dashboard_routes.is_maintenance_enabled", return_value=False),
            mock.patch("routes.dashboard_routes.get_duty_schedule_provider_status", return_value={"status": "unavailable", "authoritative": False}),
            mock.patch("routes.dashboard_routes.get_duty_schedule_months", return_value={"months": []}),
            mock.patch("routes.dashboard_routes.get_duty_schedule_month", return_value={"warnings": [], "employees": [], "days": [], "shifts": []}),
        )

    def test_three_pages_render_no_sidebar_core_shell(self):
        patches = self._patches()
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7]:
            for path, body_class in (
                ("/dashboard", "oplot-duty-dashboard"),
                ("/dashboard/release-monitor/assignment-center", "oplot-assignment-center"),
                ("/dashboard/release-monitor/duty-schedule", "oplot-duty-schedule"),
            ):
                with self.subTest(path=path):
                    response = self.client.get(path)
                    self.assertEqual(200, response.status_code)
                    text = response.get_data(as_text=True)
                    self.assertIn("oplot-shell oplot-shell--no-sidebar", text)
                    self.assertIn("oplot-topbar oplot-topbar--core", text)
                    self.assertIn(body_class, text)
                    self.assertEqual(1, len(re.findall(r"<h1\b", text)))
                    self.assertIn("css/oplot_stage9_dna.css", text)
                    self.assertIn("css/oplot_stage9_refinement.css", text)
                    if path == "/dashboard":
                        self.assertIn("css/oplot_stage9_duty.css", text)
                    else:
                        self.assertNotIn("css/oplot_stage9_duty.css", text)
                    self.assertNotIn("css/oplot_stage9_home.css", text)

    def test_schedule_invalid_query_falls_back_without_mutation_controls(self):
        patches = self._patches()
        with patches[4], patches[5], patches[6], patches[7]:
            response = self.client.get("/dashboard/release-monitor/duty-schedule?year=bad&month=99")
        self.assertEqual(200, response.status_code)
        text = response.get_data(as_text=True)
        self.assertIn("Некорректный месяц", text)
        self.assertNotRegex(text, r">\s*(Сохранить|Опубликовать)\s*<")

    def test_assignment_center_renders_disabled_ai_without_disabling_manual_workflow(self):
        with (
            mock.patch("routes.dashboard_routes.is_maintenance_enabled", return_value=False),
            mock.patch("routes.dashboard_routes.is_gigachat_enabled", return_value=False),
        ):
            response = self.client.get("/dashboard/release-monitor/assignment-center")
        self.assertEqual(200, response.status_code)
        text = response.get_data(as_text=True)
        self.assertIn('"gigachat_enabled": false', text)
        self.assertIn('id="assignmentAiBtn"', text)
        self.assertIn('id="assignmentReleaseList"', text)


if __name__ == "__main__":
    unittest.main()
