from __future__ import annotations

import unittest
from unittest import mock

from flask import Flask

from tests._support import PROJECT_ROOT, prepare_config_import

prepare_config_import()

from routes.dashboard_routes import dashboard_bp
from routes.main_routes import main_bp
from routes.mpr_routes import mpr_bp
from routes.sup_parameters_routes import sup_parameters_bp


def build_app() -> Flask:
    app = Flask(
        __name__,
        template_folder=str(PROJECT_ROOT / "templates"),
        static_folder=str(PROJECT_ROOT / "static"),
    )
    app.config.update(TESTING=True, SECRET_KEY="legacy-smoke")
    for blueprint in (main_bp, dashboard_bp, mpr_bp, sup_parameters_bp):
        app.register_blueprint(blueprint)
    return app


class LegacyGetSmokeTests(unittest.TestCase):
    def test_important_legacy_pages_keep_their_standalone_layout(self):
        monitor_model = {
            "snapshot": {"items": [], "summary": {}, "meta": {}},
            "template_hints": {},
            "reviewer_options": [],
            "sms_profile_availability": {},
        }
        patches = (
            mock.patch(
                "routes.main_routes.get_stats",
                return_value={
                    "total_combined": 0,
                    "release": {"total": 0, "last_30_days": 0, "percentage": 0},
                    "sms": {"total": 0, "last_30_days": 0, "percentage": 0},
                },
            ),
            mock.patch("routes.main_routes.is_maintenance_enabled", return_value=False),
            mock.patch("routes.dashboard_routes.get_dashboard_data", return_value={}),
            mock.patch("routes.dashboard_routes.get_hidden_tasks", return_value={}),
            mock.patch("routes.dashboard_routes.prune_hidden_tasks"),
            mock.patch("routes.dashboard_routes.get_dashboard_primary_display_names", return_value=[]),
            mock.patch("routes.dashboard_routes.is_maintenance_enabled", return_value=False),
            mock.patch(
                "routes.dashboard_routes._build_consistent_release_monitor_model",
                return_value=(monitor_model, {"view_revision": "smoke", "updated_at": "now"}),
            ),
            mock.patch("routes.dashboard_routes.get_duty_schedule_provider_status", return_value={}),
            mock.patch("routes.dashboard_routes.get_duty_schedule_months", return_value={"months": []}),
            mock.patch("routes.dashboard_routes.get_duty_schedule_month", return_value={"warnings": [], "weeks": []}),
            mock.patch("routes.mpr_routes.list_mpr_templates", return_value=[]),
            mock.patch("routes.sup_parameters_routes._configured_token", return_value=""),
        )
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7], patches[8], patches[9], patches[10], patches[11], patches[12]:
            client = build_app().test_client()
            for path in (
                "/",
                "/help",
                "/dashboard",
                "/release-monitor",
                "/mpr",
                "/dashboard/release-monitor/assignment-center",
                "/dashboard/release-monitor/duty-schedule",
                "/admin/sup-parameters",
            ):
                with self.subTest(path=path):
                    response = client.get(path)
                    self.assertEqual(200, response.status_code)
                    text = response.get_data(as_text=True)
                    self.assertIn("<html", text.lower())
                    self.assertNotIn("oplot-shell", text)


if __name__ == "__main__":
    unittest.main()
