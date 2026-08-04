from __future__ import annotations

import os
import re
import unittest
from html.parser import HTMLParser
from unittest import mock

from flask import Flask, render_template_string

from tests._support import PROJECT_ROOT, prepare_config_import

prepare_config_import()

from routes.main_routes import main_bp
from services import oplot_ui_service
from services.oplot_ui_service import build_oplot_home_actions, build_oplot_navigation, register_oplot_ui


STATS = {
    "total_combined": 17,
    "release": {"total": 11, "last_30_days": 4, "percentage": 65},
    "sms": {"total": 6, "last_30_days": 2, "percentage": 35},
}


class _MarkupParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids: list[str] = []
        self.urls: list[str] = []
        self.section_ids: list[str] = []
        self.toc_targets: list[str] = []
        self.h1_count = 0
        self._in_toc = False

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if values.get("id"):
            self.ids.append(values["id"])
        if tag == "section" and values.get("id"):
            self.section_ids.append(values["id"])
        if tag == "h1":
            self.h1_count += 1
        if tag == "nav" and values.get("aria-label") == "Оглавление":
            self._in_toc = True
        for name in ("href", "src", "action"):
            value = values.get(name)
            if value:
                self.urls.append(value)
                if self._in_toc and name == "href" and value.startswith("#"):
                    self.toc_targets.append(value[1:])

    def handle_endtag(self, tag):
        if tag == "nav" and self._in_toc:
            self._in_toc = False


def build_app(*, templates_enabled: bool = True, omitted: set[str] | None = None) -> Flask:
    omitted = omitted or set()
    app = Flask(__name__, template_folder=str(PROJECT_ROOT / "templates"), static_folder=str(PROJECT_ROOT / "static"))
    app.config.update(TESTING=True, SECRET_KEY="core-pages-test", DOCUMENT_TEMPLATE_CENTER_ENABLED=templates_enabled)
    app.register_blueprint(main_bp)
    routes = (
        ("/release-monitor", "dashboard.release_monitor_page"),
        ("/dashboard", "dashboard.dashboard"),
        ("/dashboard/refresh", "dashboard.refresh_dashboard"),
        ("/mpr", "mpr.mpr_page"),
        ("/dashboard/release-monitor/assignment-center", "dashboard.release_monitor_assignment_center_page"),
        ("/dashboard/release-monitor/duty-schedule", "dashboard.release_monitor_duty_schedule_page"),
        ("/admin/document-templates/", "document_templates.index"),
        ("/admin/sup-parameters", "sup_parameters.sup_parameters_page"),
        ("/admin/va/schedule-manager/", "va_schedule_manager.web.index"),
    )
    for index, (path, endpoint) in enumerate(routes):
        if endpoint in omitted:
            continue
        app.add_url_rule(path, endpoint=endpoint, view_func=lambda value=index: str(value), methods=["GET", "POST"])
    register_oplot_ui(app)
    return app


def render_page(app: Flask, path: str = "/", **request_options) -> str:
    with mock.patch("routes.main_routes.get_stats", return_value=STATS), mock.patch(
        "routes.main_routes.is_maintenance_enabled", return_value=False
    ):
        response = app.test_client().get(path, **request_options)
    if response.status_code != 200:
        raise AssertionError(response.get_data(as_text=True))
    return response.get_data(as_text=True)


class HomeActionTests(unittest.TestCase):
    def test_order_groups_uniqueness_and_navigation_owned_metadata(self):
        app = build_app()
        with app.test_request_context("/"):
            groups = build_oplot_home_actions()
            navigation = {item["id"]: item for group in build_oplot_navigation("main.index") for item in group["items"]}
        self.assertEqual(["primary"], [group["id"] for group in groups])
        ids = [item["id"] for group in groups for item in group["items"]]
        self.assertEqual(
            ["release-monitor", "duty-dashboard", "mpr"],
            ids,
        )
        self.assertEqual(len(ids), len(set(ids)))
        self.assertFalse({"home", "help", "sup-parameters", "schedule-manager"} & set(ids))
        for item in (item for group in groups for item in group["items"]):
            self.assertEqual(navigation[item["id"]]["label"], item["label"])
            self.assertEqual(navigation[item["id"]]["icon"], item["icon"])
            self.assertEqual(navigation[item["id"]]["url"], item["url"])
        self.assertFalse({"label", "icon", "endpoint"} & set(oplot_ui_service.HomeAction.__dataclass_fields__))

    def test_feature_flag_does_not_add_templates_and_missing_endpoint_hides_action(self):
        app = build_app(templates_enabled=True, omitted={"dashboard.release_monitor_page"})
        with app.test_request_context("/"):
            ids = {item["id"] for group in build_oplot_home_actions() for item in group["items"]}
        self.assertNotIn("document-templates", ids)
        self.assertNotIn("release-monitor", ids)

    def test_urls_honor_prefix_sources_without_duplication(self):
        app = build_app()
        cases = (
            ({"headers": {"X-Forwarded-Prefix": "/proxy"}}, "/proxy/dashboard"),
            ({"environ_overrides": {"SCRIPT_NAME": "/script"}}, "/script/dashboard"),
        )
        for request_options, expected in cases:
            with self.subTest(expected=expected), app.test_request_context("/", **request_options):
                item = next(item for group in build_oplot_home_actions() for item in group["items"] if item["id"] == "duty-dashboard")
                self.assertEqual(expected, item["url"])
                self.assertEqual(expected + "/refresh", item["refresh_url"])
        with mock.patch.dict(os.environ, {"BASE_PATH": "/base"}, clear=False):
            with app.test_request_context("/"):
                item = next(item for group in build_oplot_home_actions() for item in group["items"] if item["id"] == "duty-dashboard")
                self.assertEqual("/base/dashboard", item["url"])
                self.assertNotIn("/base/base/", item["url"])


class CorePageTests(unittest.TestCase):
    def setUp(self):
        self.app = build_app()

    def test_home_uses_shell_and_preserves_route_data_contract(self):
        with mock.patch("routes.main_routes.get_stats", return_value=STATS) as get_stats, mock.patch(
            "routes.main_routes.is_maintenance_enabled", side_effect=lambda scope: scope == "chatbot"
        ) as maintenance:
            response = self.app.test_client().get("/")
        self.assertEqual(200, response.status_code)
        text = response.get_data(as_text=True)
        parser = _MarkupParser(); parser.feed(text)
        self.assertIn("oplot-shell", text)
        self.assertIn("oplot-shell--no-sidebar", text)
        self.assertNotIn('class="oplot-sidebar"', text)
        self.assertNotIn('class="oplot-page-header"', text)
        self.assertEqual(1, parser.h1_count)
        self.assertIn("<h1>AI-агент команды OPLOT</h1>", text)
        self.assertIn("Статистика", text)
        self.assertIn('id="home-usage-modal"', text)
        self.assertIn('id="home-version-modal"', text)
        self.assertIn("История версий Oplot", text)
        self.assertIn('id="embeddedChatBot"', text)
        self.assertIn("css/chatbot.css", text)
        self.assertIn("js/chatbot.js", text)
        self.assertIn('data-refresh-url="/dashboard/refresh"', text)
        self.assertIn('id="link-sandbox"', text)
        self.assertIn('href="/releases/sandbox/"', text)
        self.assertEqual(1, text.count("Песочница"))
        self.assertNotIn("Эксперименты", text)
        self.assertNotIn("Основные сценарии", text)
        self.assertNotIn("Управление", text)
        self.assertNotIn("Рабочий навигатор", text)
        self.assertNotIn("oplot-home-service-line", text)
        self.assertNotIn("<footer", text.lower())
        self.assertNotIn("toggleTheme", text)
        self.assertNotIn("htmx.min.js", text)
        self.assertNotIn("jszip.min.js", text)
        self.assertNotIn("docx-preview.min.js", text)
        action_ids = [value for value in parser.ids if value.startswith("home-action-")]
        self.assertEqual(
            ["home-action-release-monitor", "home-action-duty-dashboard", "home-action-mpr"],
            action_ids,
        )
        self.assertEqual(len(action_ids), len(set(action_ids)))
        self.assertNotIn("home-action-assignment-center", text)
        self.assertNotIn("home-action-duty-schedule", text)
        self.assertNotIn("home-action-document-templates", text)
        self.assertIn('data-home-icon="release"', text)
        self.assertIn('data-home-icon="dashboard"', text)
        self.assertIn('data-home-icon="mpr"', text)
        self.assertIn('src="/static/img/oplot-home-release.svg"', text)
        self.assertIn('src="/static/img/oplot-home-dashboard.svg"', text)
        self.assertIn('src="/static/img/oplot-home-mpr.svg"', text)
        self.assertEqual(3, text.count("oplot-home-action-label"))
        dashboard_card = re.search(
            r'<a id="home-action-duty-dashboard"[\s\S]*?href="([^"]+)"[\s\S]*?data-home-dashboard-link[\s\S]*?data-refresh-url="([^"]+)"',
            text,
        )
        self.assertIsNotNone(dashboard_card)
        self.assertEqual(("/dashboard", "/dashboard/refresh"), dashboard_card.groups())
        self.assertLess(text.index('class="oplot-home-utility-row"'), text.index("<h1>AI-агент команды OPLOT</h1>"))
        self.assertLess(text.index("<h1>AI-агент команды OPLOT</h1>"), text.index('class="oplot-home-actions"'))
        self.assertLess(text.index('class="oplot-home-actions"'), text.index('id="chatbotAgentShell"'))
        utility_start = text.index('class="oplot-home-utility-row"')
        utility_end = text.index("</div>", utility_start)
        utility_markup = text[utility_start:utility_end]
        self.assertIn("Статистика", utility_markup)
        self.assertIn("data-bs-target=\"#home-version-modal\"", utility_markup)
        self.assertIn('id="link-sandbox"', utility_markup)
        self.assertIn("data-oplot-theme-toggle", utility_markup)
        self.assertIn("theme-label", utility_markup)
        self.assertIn("oplot-home-theme-icon--dark", utility_markup)
        self.assertIn("oplot-home-theme-icon--light", utility_markup)
        self.assertLess(utility_markup.index("Статистика"), utility_markup.index('data-bs-target="#home-version-modal"'))
        self.assertLess(utility_markup.index('data-bs-target="#home-version-modal"'), utility_markup.index('id="link-sandbox"'))
        self.assertLess(utility_markup.index('id="link-sandbox"'), utility_markup.index("data-oplot-theme-toggle"))
        menu_start = text.index('class="oplot-home-actions"')
        menu_end = text.index("</section>", menu_start)
        self.assertNotIn('id="link-sandbox"', text[menu_start:menu_end])
        home_css = (PROJECT_ROOT / "static" / "css" / "oplot_home.css").read_text(encoding="utf-8")
        self.assertIn(".oplot-home .oplot-topbar .oplot-theme-toggle { display: none; }", home_css)
        self.assertIn("background: rgba(5, 20, 46, .72);", home_css)
        self.assertIn("@supports ((backdrop-filter: blur(1px))", home_css)
        chatbot_source = (PROJECT_ROOT / "static" / "js" / "chatbot.js").read_text(encoding="utf-8")
        self.assertIn('id="chat-suggestions"', chatbot_source)
        self.assertIn("chat-suggestion-btn", chatbot_source)
        self.assertFalse([url for url in parser.urls if url.startswith(("http://", "https://", "//"))])
        get_stats.assert_called_once_with()
        self.assertEqual([mock.call("index"), mock.call("chatbot")], maintenance.call_args_list)

    def test_home_urls_are_prefix_safe(self):
        text = render_page(self.app, "/", headers={"X-Forwarded-Prefix": "/oplot"})
        self.assertIn('href="/oplot/dashboard"', text)
        self.assertIn('data-refresh-url="/oplot/dashboard/refresh"', text)
        self.assertIn('id="link-sandbox" class="oplot-home-utility-chip oplot-home-sandbox-link" href="/oplot/releases/sandbox/"', text)
        self.assertIn('src="/oplot/static/js/oplot_home.js"', text)
        self.assertNotIn("/oplot/oplot/", text)

    def test_sandbox_url_honors_public_prefix_without_duplication(self):
        cases = (
            ({}, "/releases/sandbox/"),
            ({"headers": {"X-Forwarded-Prefix": "/proxy"}}, "/proxy/releases/sandbox/"),
            ({"environ_overrides": {"SCRIPT_NAME": "/script"}}, "/script/releases/sandbox/"),
            ({"headers": {"X-Forwarded-Prefix": "/releases"}}, "/releases/sandbox/"),
        )
        for request_options, expected in cases:
            with self.subTest(expected=expected):
                text = render_page(self.app, **request_options)
                self.assertIn(f'id="link-sandbox" class="oplot-home-utility-chip oplot-home-sandbox-link" href="{expected}"', text)

        with mock.patch.dict(os.environ, {"BASE_PATH": "/base"}, clear=False):
            text = render_page(self.app)
        self.assertIn('href="/base/releases/sandbox/"', text)
        self.assertNotIn("/base/base/", text)

    def test_help_shell_toc_content_and_assets(self):
        text = render_page(self.app, "/help", headers={"X-Forwarded-Prefix": "/oplot"})
        parser = _MarkupParser(); parser.feed(text)
        expected = ["start", "bot", "docs", "release-block", "week-control", "reports", "issues", "screens", "commands"]
        self.assertIn("oplot-shell", text)
        self.assertIn("oplot-shell--no-sidebar", text)
        self.assertNotIn('class="oplot-sidebar"', text)
        self.assertIn('class="oplot-page-header"', text)
        self.assertEqual(1, parser.h1_count)
        self.assertEqual(expected, parser.section_ids)
        self.assertEqual(expected, parser.toc_targets)
        self.assertEqual(len(parser.section_ids), len(set(parser.section_ids)))
        self.assertIn("Как работать с Oplot", text)
        self.assertIn("Сформируй документы по EMRM-12345", text)
        self.assertIn("Выгрузи таблицу релизов в Confluence", text)
        self.assertIn('src="/oplot/static/img/help/home.png"', text)
        self.assertIn('src="/oplot/static/img/help/dashboard.png"', text)
        self.assertIn('href="/oplot/"', text)
        self.assertIn('aria-current="page"', text)
        self.assertNotIn("toggleTheme", text)
        self.assertNotIn("base_styles", text)
        self.assertNotIn("docx-preview", text)
        self.assertNotIn("htmx.min.js", text)
        self.assertFalse([url for url in parser.urls if url.startswith(("http://", "https://", "//"))])
        help_css = (PROJECT_ROOT / "static" / "css" / "oplot_help.css").read_text(encoding="utf-8")
        self.assertRegex(help_css, r"\.oplot-help-layout\s*\{[^}]*margin-inline:\s*auto")

    def test_maintenance_partial_default_and_shell_modes(self):
        template = "{% with maintenance_enabled=true, maintenance_scope='test' %}{% include 'maintenance_gate.html' %}{% endwith %}"
        shell_template = "{% with maintenance_enabled=true, maintenance_scope='test', maintenance_external_assets=true %}{% include 'maintenance_gate.html' %}{% endwith %}"
        with self.app.test_request_context("/"):
            legacy = render_template_string(template)
            shell = render_template_string(shell_template)
        self.assertIn("applyMaintenanceGate", legacy)
        self.assertIn("<style>", legacy)
        self.assertIn("app-maintenance-gate", shell)
        self.assertNotIn("applyMaintenanceGate", shell)
        self.assertNotIn("<style>", shell)
        self.assertNotIn("<script>", shell)

    def test_home_maintenance_uses_external_assets_and_keeps_gate(self):
        with mock.patch("routes.main_routes.get_stats", return_value=STATS), mock.patch(
            "routes.main_routes.is_maintenance_enabled", side_effect=lambda scope: scope == "index"
        ):
            text = self.app.test_client().get("/").get_data(as_text=True)
        self.assertIn("app-maintenance-gate", text)
        self.assertIn('data-maintenance-enabled="true"', text)
        self.assertNotIn("applyMaintenanceGate", text)


if __name__ == "__main__":
    unittest.main()
