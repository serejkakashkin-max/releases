from __future__ import annotations

import re
import unittest
from datetime import datetime, timedelta
from unittest import mock

from flask import Flask

from tests._support import PROJECT_ROOT, prepare_config_import

prepare_config_import()

from routes.dashboard_routes import dashboard_bp
from services.release_report_service import ReleaseReportService


def current_week_item(
    key: str,
    *,
    day: int = 1,
    cancelled: bool = False,
    unnumbered: bool = False,
    final: bool = False,
    summary: str | None = None,
) -> dict:
    start = datetime.now().replace(hour=9, minute=0, second=0, microsecond=0)
    start -= timedelta(days=start.weekday())
    start += timedelta(days=day)
    return {
        "release_number": day + 1,
        "release_key": key,
        "release_url": f"https://jira.example.invalid/browse/{key}",
        "zni_key": f"ZNI-{day + 1}",
        "zni_url": "",
        "rov_key": f"ROV-{day + 1}",
        "rov_url": "",
        "release_summary": summary or f"Release {key}",
        "release_name_lines": [summary or f"Release {key}"],
        "release_version": "1.0",
        "release_status": "Готов",
        "manual_system_name": "EMRM",
        "deployment_start": start.strftime("%d.%m.%Y"),
        "deployment_end": start.strftime("%d.%m.%Y"),
        "deployment_start_iso": start.isoformat(),
        "deployment_end_iso": start.isoformat(),
        "is_cancelled": cancelled,
        "is_unnumbered": unnumbered,
        "is_final": final,
    }


def build_app() -> Flask:
    app = Flask(__name__)
    app.config.update(TESTING=True, SECRET_KEY="current-week-tests")
    app.register_blueprint(dashboard_bp)
    return app


class CurrentWeekDataContractTests(unittest.TestCase):
    def setUp(self):
        self.service = ReleaseReportService()

    def test_period_filter_sort_cancelled_unnumbered_and_installed_rules(self):
        late = current_week_item("REL-2", day=3, final=True)
        early = current_week_item("REL-1", day=1)
        cancelled = current_week_item("REL-C", day=2, cancelled=True)
        unnumbered = current_week_item("REL-U", day=2, unnumbered=True)
        report = self.service.generate_current_week_plan_report(
            [late, cancelled, unnumbered, early]
        )
        self.assertEqual(["REL-1", "REL-2"], [item["release_key"] for item in report["items"]])
        self.assertEqual(2, report["statistics"]["total"])
        self.assertEqual(1, report["statistics"]["installed"])
        self.assertEqual(1, report["statistics"]["hidden_by_default"])
        self.assertEqual("current_week_plan", report["period"]["mode"])

    def test_reroll_hotfix_system_and_status_statistics_are_preserved(self):
        reroll = current_week_item("REL-R", day=1)
        reroll["release_type"] = "reroll"
        hotfix = current_week_item("REL-H", day=2)
        hotfix["release_type"] = "hotfix"
        report = self.service.generate_current_week_plan_report([hotfix, reroll])
        self.assertEqual(1, report["statistics"]["rerolls"])
        self.assertEqual(1, report["statistics"]["hotfixes"])
        self.assertEqual(2, report["statistics"]["systems"]["EMRM"])
        self.assertEqual(2, report["statistics"]["statuses"]["Готов"])


class CurrentWeekHtmlContractTests(unittest.TestCase):
    def setUp(self):
        self.service = ReleaseReportService()

    def _html(self, items):
        return self.service.generate_current_week_plan_html(
            self.service.generate_current_week_plan_report(items)
        )

    def test_standalone_oplot_dna_and_exact_ten_column_geometry(self):
        text = self._html([current_week_item("REL-1")])
        self.assertTrue(text.lower().startswith("<!doctype html>"))
        self.assertIn("--cw-bg: #030b1a", text)
        self.assertIn("rgba(75, 151, 230, 0.38)", text)
        self.assertIn("data-theme-icon=\"sun\"", text)
        self.assertIn("data-theme-icon=\"moon\"", text)
        self.assertIn("localStorage.getItem('theme')", text)
        self.assertIn("localStorage.setItem('theme', theme)", text)
        self.assertIn("week-table-scroll", text)
        thead = re.search(r"<thead>(.*?)</thead>", text, re.S)
        self.assertIsNotNone(thead)
        self.assertEqual(10, len(re.findall(r"<th(?:\s|>)", thead.group(1))))
        for external in ("/static/", "cdn", "bootstrap-icons", "class=\"bi ", "http://"):
            self.assertNotIn(external, text.lower())

    def test_empty_many_rows_long_names_escaping_and_filter_hooks(self):
        empty = self._html([])
        self.assertIn('colspan="10"', empty)
        long_value = "Очень длинное русское название " * 12 + "<script>alert(1)</script>"
        many = self._html(
            [current_week_item(f"REL-{index}", day=index % 5, summary=long_value) for index in range(24)]
        )
        self.assertNotIn("<script>alert(1)</script>", many)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", many)
        self.assertEqual(24, many.count('class="week-row-number"'))
        for hook in (
            "data-filter-type",
            "data-system",
            "data-status",
            "showFinalRows",
            "clearReportFilter",
            "window.location.reload()",
        ):
            self.assertIn(hook, many)


class CurrentWeekRouteContractTests(unittest.TestCase):
    def setUp(self):
        self.app = build_app()
        self.client = self.app.test_client()
        self.items = [current_week_item("REL-1")]

    def test_live_get_headers_and_autonomous_body(self):
        with mock.patch(
            "routes.dashboard_routes.get_release_monitor_snapshot",
            return_value={"items": self.items},
        ):
            response = self.client.get("/dashboard/release-monitor/current-week")
        self.assertEqual(200, response.status_code)
        self.assertEqual("text/html; charset=utf-8", response.headers["Content-Type"])
        self.assertIn("no-store", response.headers["Cache-Control"])
        self.assertNotIn("/static/", response.get_data(as_text=True))

    def test_current_and_legacy_post_save_only_mocked_report(self):
        for path in (
            "/dashboard/release-monitor/report/current-week",
            "/dashboard/release-monitor/report/current-week-legacy",
        ):
            with self.subTest(path=path), mock.patch(
                "routes.dashboard_routes.get_release_monitor_snapshot",
                return_value={"items": self.items},
            ), mock.patch(
                "routes.dashboard_routes.save_report_to_disk", return_value="fixture-report"
            ) as save:
                response = self.client.post(path)
                self.assertEqual(200, response.status_code)
                payload = response.get_json()
                self.assertTrue(payload["success"])
                self.assertEqual(1, payload["report_summary"]["total"])
                self.assertIn("fixture-report", payload["download_url"])
                saved_html = save.call_args.args[0]
                self.assertNotIn("/static/", saved_html)
                self.assertNotIn("bootstrap-icons", saved_html.lower())


if __name__ == "__main__":
    unittest.main()
