from __future__ import annotations

import hashlib
import re
import os
import unittest
from html.parser import HTMLParser
from unittest import mock

from flask import Flask

from tests._support import PROJECT_ROOT, prepare_config_import

prepare_config_import()

from routes.dashboard_routes import dashboard_bp
from services.oplot_ui_service import build_oplot_navigation, register_oplot_ui
from services.release_ui_service import build_release_navigation


TEMPLATE_PATH = PROJECT_ROOT / "templates" / "release_monitor.html"


class _ReleaseMarkupParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids: list[str] = []
        self.headers: list[str] = []
        self._in_header = False
        self._header_parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if values.get("id"):
            self.ids.append(values["id"])
        if tag == "th":
            self._in_header = True
            self._header_parts = []

    def handle_data(self, data):
        if self._in_header:
            value = " ".join(data.split())
            if value:
                self._header_parts.append(value)

    def handle_endtag(self, tag):
        if tag == "th" and self._in_header:
            self.headers.append(" ".join(self._header_parts))
            self._in_header = False


def _build_app(*, document_templates_enabled: bool = True, include_document_templates: bool = True) -> Flask:
    app = Flask(
        __name__,
        template_folder=str(PROJECT_ROOT / "templates"),
        static_folder=str(PROJECT_ROOT / "static"),
    )
    app.config.update(
        TESTING=True,
        SECRET_KEY="release-block-characterization",
        DOCUMENT_TEMPLATE_CENTER_ENABLED=document_templates_enabled,
    )
    app.add_url_rule("/", endpoint="main.index", view_func=lambda: "home")
    if include_document_templates:
        app.add_url_rule(
            "/admin/document-templates/",
            endpoint="document_templates.index",
            view_func=lambda: "templates",
        )
    app.register_blueprint(dashboard_bp)
    register_oplot_ui(app)
    return app


def _render_release_monitor():
    model = {
        "snapshot": {"items": [], "summary": {}, "meta": {"years": [2026], "current_year": 2026}},
        "template_hints": {},
        "reviewer_options": [],
        "sms_profile_availability": {},
    }
    with mock.patch(
        "routes.dashboard_routes._build_consistent_release_monitor_model",
        return_value=(model, {"view_revision": "characterization", "updated_at": "2026-08-04T10:00:00Z"}),
    ) as build_model, mock.patch(
        "routes.dashboard_routes.is_maintenance_enabled", return_value=False
    ) as maintenance:
        response = _build_app().test_client().get("/release-monitor")
    return response, build_model, maintenance


class ReleaseMonitorCharacterizationTests(unittest.TestCase):
    def test_endpoint_url_provider_contract_and_table_structure(self):
        response, build_model, maintenance = _render_release_monitor()
        self.assertEqual(200, response.status_code)
        self.assertEqual("/release-monitor", next(rule.rule for rule in _build_app().url_map.iter_rules() if rule.endpoint == "dashboard.release_monitor_page"))
        build_model.assert_called_once()
        maintenance.assert_called_once_with("release_monitor")

        parser = _ReleaseMarkupParser()
        parser.feed(response.get_data(as_text=True))
        self.assertEqual(
            [
                "№",
                "Название релиза",
                "№ ЗНИ",
                "КЭ",
                "ID релиза",
                "ID РОВ",
                "Дата начала внедрения",
                "Дата окончания внедрения",
                "Ответственный за ПСИ/Проверки",
                "Документы",
            ],
            parser.headers,
        )

    def test_dom_filter_modal_and_field_contracts(self):
        response, _build_model, _maintenance = _render_release_monitor()
        text = response.get_data(as_text=True)
        parser = _ReleaseMarkupParser()
        parser.feed(text)
        for value in (
            "releaseMonitorSection", "releaseMonitorBody", "releaseTableWrap",
            "releaseSearchInput", "releaseYearFilter", "releaseViewFilter",
            "releaseWorkMarkFilter", "releaseWorkMarkModeBtn", "releaseSmsVisibleBtn",
            "releaseDocumentModal", "releaseDateOverrideModal", "releaseManualCreateModal",
            "releaseManualOverrideModal", "releaseSmsModal", "smsTemplateEditorModal",
        ):
            self.assertIn(value, parser.ids)
        for dynamic_id in ("releaseResponsibleFilter", "releaseStatusFilter"):
            self.assertIn(dynamic_id, text)
        for field_name in (
            "release_key", "release_type", "release_summary", "deployment_start",
            "deployment_end", "rov_key", "release_status", "rov_status", "system_name",
            "ke_id", "release_version", "ke", "release_dist_url", "zni_key",
        ):
            self.assertIn(f"name: '{field_name}'", text)

    def test_action_methods_polling_and_existing_transitions_are_stable(self):
        source = TEMPLATE_PATH.read_text(encoding="utf-8")
        post_paths = (
            "/dashboard/release-monitor/work-mark",
            "/dashboard/release-monitor/date-override",
            "/dashboard/release-monitor/manual-release/lookup",
            "/dashboard/release-monitor/manual-release",
            "/release/monitor-init",
            "/dashboard/release-monitor/manual-distribution",
            "/release/monitor-generate",
            "/sms/release-monitor/generate",
            "/sms/templates/",
            "/dashboard/release-monitor/order",
            "/dashboard/release-monitor/zni",
            "/dashboard/release-monitor/reviewer",
            "/dashboard/release-monitor/rollout-notes",
            "/dashboard/release-monitor/confluence-sync",
        )
        for path in post_paths:
            match = re.search(re.escape(path) + r"[\s\S]{0,260}?method:\s*'POST'", source)
            self.assertIsNotNone(match, path)
        self.assertIn("/dashboard/release-monitor/status?compact=1", source)
        self.assertIn("releaseMonitorGlobalRefreshing ? 2000 : 15000", source)
        self.assertIn("Math.min(60000", source)
        self.assertIn('id="oplot-release-config"', source)
        self.assertNotIn("location.hostname", source)
        self.assertIn("dashboard.release_monitor_duty_schedule_page", (PROJECT_ROOT / "services" / "release_ui_service.py").read_text(encoding="utf-8"))
        self.assertRegex(source, r"window\.open\(BASE_PATH \+ '/dashboard/release-monitor/current-week', '_blank'")
        self.assertRegex(source, r"window\.open\(BASE_PATH \+ '/dashboard/release-monitor/assignment-center', '_blank'")

    def test_release_monitor_does_not_load_document_template_viewer_assets(self):
        response, _build_model, _maintenance = _render_release_monitor()
        text = response.get_data(as_text=True)
        self.assertNotIn("htmx.min.js", text)
        self.assertNotIn("jszip.min.js", text)
        self.assertNotIn("docx-preview.min.js", text)
        self.assertNotIn("document_templates.js", text)


class ReleaseBlockMigrationTests(unittest.TestCase):
    def test_release_page_shell_header_menu_and_useful_links(self):
        response, _build_model, _maintenance = _render_release_monitor()
        text = response.get_data(as_text=True)
        self.assertIn("oplot-shell oplot-shell--no-sidebar", text)
        self.assertNotIn('class="oplot-sidebar', text)
        self.assertIn("<h1>Блок релизов</h1>", text)
        self.assertIn("oplot_release.css", text)
        self.assertNotIn("bootstrap.bundle.min.js", text)
        self.assertNotIn("base_styles.html", text)
        self.assertIn('class="oplot-topbar oplot-topbar--compact"', text)
        self.assertIn('class="oplot-topbar__brand" href="/">OPLOT</a>', text)
        self.assertNotIn('class="oplot-topbar__context"', text)
        self.assertNotIn('class="oplot-breadcrumbs"', text)
        self.assertNotRegex(text, r'<(?:script|link|img)\b[^>]*(?:src|href)=["\'](?:https?:)?//')

        menu_labels = ("График дежурств", "Центр назначений", "Релизы текущей недели", "Центр шаблонов")
        positions = [text.index(label) for label in menu_labels]
        self.assertEqual(positions, sorted(positions))
        self.assertIn('id="releaseMonitorAssignmentCenterBtn"', text)
        self.assertIn('href="/dashboard/release-monitor/assignment-center"', text)
        self.assertIn('target="_blank" rel="noopener"', text)

        resource_labels = (
            "Ссылка на отчет АПТБ",
            "Ссылки на ПСИ Jenkins",
            "Таблица в Confluence(экспорт)",
            "Отправка SMS по результатам внедрения",
        )
        resource_positions = [text.index(label) for label in resource_labels]
        self.assertEqual(resource_positions, sorted(resource_positions))

    def test_internal_navigation_order_targets_and_feature_flag(self):
        app = _build_app()
        with app.test_request_context("/release-monitor"):
            items = build_release_navigation("dashboard.release_monitor_page")
        self.assertEqual(
            ["duty-schedule", "assignment-center", "current-week", "document-templates"],
            [item["id"] for item in items],
        )
        self.assertEqual(
            ["График дежурств", "Центр назначений", "Релизы текущей недели", "Центр шаблонов"],
            [item["label"] for item in items],
        )
        self.assertEqual(["_self", "_blank", "_blank", "_self"], [item["target"] for item in items])

        disabled = _build_app(document_templates_enabled=False)
        with disabled.test_request_context("/release-monitor"):
            self.assertNotIn("document-templates", [item["id"] for item in build_release_navigation()])
        missing = _build_app(include_document_templates=False)
        with missing.test_request_context("/release-monitor"):
            self.assertNotIn("document-templates", [item["id"] for item in build_release_navigation()])

    def test_global_navigation_uses_release_context_for_document_templates(self):
        app = _build_app()
        with app.test_request_context("/admin/document-templates/"):
            groups = build_oplot_navigation("document_templates.index")
        items = [item for group in groups for item in group["items"]]
        self.assertNotIn("document-templates", [item["id"] for item in items])
        release_item = next(item for item in items if item["id"] == "release-monitor")
        self.assertTrue(release_item["active"])

    def test_document_template_templates_use_release_block_context(self):
        for name in ("index.html", "candidate.html", "history.html", "configuration_error.html"):
            with self.subTest(template=name):
                source = (PROJECT_ROOT / "templates" / "document_templates" / name).read_text(encoding="utf-8")
                self.assertIn("'Блок релизов','endpoint':'dashboard.release_monitor_page'", source)
                self.assertIn("{% set oplot_context_label = 'Блок релизов' %}", source)

    def test_shell_menu_columns_and_prefix_safe_urls(self):
        cases = (
            ({}, "/dashboard/release-monitor/duty-schedule", "/"),
            ({"headers": {"X-Forwarded-Prefix": "/proxy"}}, "/proxy/dashboard/release-monitor/duty-schedule", "/proxy/"),
            ({"environ_overrides": {"SCRIPT_NAME": "/script"}}, "/script/dashboard/release-monitor/duty-schedule", "/script/"),
        )
        for request_kwargs, expected, expected_home in cases:
            with self.subTest(expected=expected):
                with mock.patch(
                    "routes.dashboard_routes._build_consistent_release_monitor_model",
                    return_value=(
                        {
                            "snapshot": {"items": [], "summary": {}, "meta": {"years": [2026], "current_year": 2026}},
                            "template_hints": {},
                            "reviewer_options": [],
                            "sms_profile_availability": {},
                        },
                        {"view_revision": "prefix-test", "updated_at": "2026-08-04T10:00:00Z"},
                    ),
                ), mock.patch("routes.dashboard_routes.is_maintenance_enabled", return_value=False):
                    response = _build_app().test_client().get("/release-monitor", **request_kwargs)
                text = response.get_data(as_text=True)
                self.assertEqual(200, response.status_code)
                self.assertIn('class="oplot-shell oplot-shell--no-sidebar"', text)
                self.assertNotIn('class="oplot-sidebar', text)
                self.assertIn(f'href="{expected}"', text)
                self.assertIn(f'class="oplot-topbar__brand" href="{expected_home}"', text)
                self.assertEqual(10, len(_ReleaseMarkupParser_with_headers(text)))
                self.assertNotIn("htmx.min.js", text)
                self.assertNotIn("docx-preview.min.js", text)
        with mock.patch.dict(os.environ, {"BASE_PATH": "/base"}, clear=False), mock.patch(
            "routes.dashboard_routes._build_consistent_release_monitor_model",
            return_value=(
                {
                    "snapshot": {"items": [], "summary": {}, "meta": {"years": [2026], "current_year": 2026}},
                    "template_hints": {},
                    "reviewer_options": [],
                    "sms_profile_availability": {},
                },
                {"view_revision": "base-path-test", "updated_at": "2026-08-04T10:00:00Z"},
            ),
        ), mock.patch("routes.dashboard_routes.is_maintenance_enabled", return_value=False):
            text = _build_app().test_client().get("/release-monitor").get_data(as_text=True)
        self.assertIn('href="/base/dashboard/release-monitor/duty-schedule"', text)
        self.assertIn('class="oplot-topbar__brand" href="/base/"', text)
        self.assertIn('"public_base": "/base"', text)
        self.assertNotIn("/base/base/", text)

    def test_business_sensitive_inline_javascript_is_unchanged(self):
        source = TEMPLATE_PATH.read_text(encoding="utf-8")
        match = re.search(
            r"<script>\s*let currentReleaseYearFilter[\s\S]*?</script>",
            source,
        )
        self.assertIsNotNone(match)
        normalized = match.group(0).replace("\r\n", "\n").strip()
        self.assertEqual(
            "512d78e7f474b932d245ce087dd4c5745a99650fc113caafb6914ec2b388b908",
            hashlib.sha256(normalized.encode("utf-8")).hexdigest(),
        )

    def test_compact_shell_is_release_monitor_only(self):
        release_source = TEMPLATE_PATH.read_text(encoding="utf-8")
        self.assertIn("{% set oplot_topbar_variant = 'compact' %}", release_source)
        self.assertIn("{% set oplot_show_breadcrumbs = false %}", release_source)
        for relative in (
            "templates/index.html",
            "templates/help.html",
            "templates/document_templates/index.html",
            "templates/document_templates/candidate.html",
            "templates/document_templates/history.html",
        ):
            with self.subTest(template=relative):
                source = (PROJECT_ROOT / relative).read_text(encoding="utf-8")
                self.assertNotIn("oplot_topbar_variant = 'compact'", source)
                self.assertNotIn("oplot_show_breadcrumbs = false", source)


def _ReleaseMarkupParser_with_headers(text: str) -> list[str]:
    parser = _ReleaseMarkupParser()
    parser.feed(text)
    return parser.headers


if __name__ == "__main__":
    unittest.main()
