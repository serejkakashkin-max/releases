import unittest
from datetime import datetime
from unittest.mock import patch

from flask import Flask

from routes.dashboard_routes import dashboard_bp
from services import release_monitor_confluence_notification_service as service


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text
        self.reason = text

    @property
    def ok(self):
        return 200 <= self.status_code < 300

    def json(self):
        return self._payload


class ReleaseMonitorConfluenceNotificationTests(unittest.TestCase):
    @staticmethod
    def _item(row_key="SMECSC-100::SMECSC-101", **overrides):
        item = {
            "row_key": row_key,
            "release_key": row_key.split("::", 1)[0],
            "rov_key": row_key.split("::", 1)[1],
            "release_summary": "Тестовый релиз",
            "release_status": "Формирование",
            "deployment_start": "12.06.2026",
            "deployment_start_iso": "2026-06-12",
            "ke": "CI12345678",
            "release_version": "D-01.001.00-test-1",
            "psi_owner": "Фисан К.Ю.",
            "psi_owner_source": "duty_schedule",
            "psi_responsibles": [],
            "is_current_week_assignment_scope": True,
            "is_missing_week_responsible": True,
            "is_cancelled": False,
            "is_final": False,
            "is_reroll": False,
            "release_url": "https://jira.example/browse/SMECSC-100",
            "rov_url": "https://jira.example/browse/SMECSC-101",
        }
        item.update(overrides)
        return item

    @staticmethod
    def _page(storage_html=""):
        return {
            "page_id": "18655871248",
            "title": "Информирование о новых релизах",
            "version": 4,
            "storage_html": storage_html,
        }

    def test_selection_excludes_completed_cancelled_and_assigned_rows(self):
        active = self._item()
        active_reroll = self._item(
            "EMRM-100::EMRM-102",
            release_key="EMRM-100",
            rov_key="EMRM-102",
            is_reroll=True,
            is_final=False,
        )
        final = self._item("SMECSC-200::SMECSC-201", is_final=True)
        cancelled = self._item("SMECSC-300::SMECSC-301", is_cancelled=True)
        assigned = self._item(
            "SMECSC-400::SMECSC-401",
            psi_responsibles=["Кашкин С.Н."],
            is_missing_week_responsible=False,
        )
        outside_week = self._item(
            "SMECSC-500::SMECSC-501",
            is_current_week_assignment_scope=False,
            is_missing_week_responsible=False,
        )

        selected = service.select_unassigned_current_week_items(
            [active, active_reroll, final, cancelled, assigned, outside_week]
        )

        self.assertEqual(
            [item["row_key"] for item in selected],
            [active["row_key"], active_reroll["row_key"]],
        )

    def test_rendered_page_round_trips_row_keys_and_places_new_rows_first(self):
        old_item = self._item(
            "SMECSC-100::SMECSC-101",
            deployment_start="10.06.2026",
            deployment_start_iso="2026-06-10",
        )
        new_item = self._item(
            "EMRM-500::EMRM-501",
            release_key="EMRM-500",
            rov_key="EMRM-501",
            deployment_start="13.06.2026",
            deployment_start_iso="2026-06-13",
        )

        storage = service.build_unassigned_release_page_storage(
            [old_item, new_item],
            new_row_keys={new_item["row_key"]},
            snapshot_meta={"accepted_at": "2026-06-12T08:00:00"},
            updated_at=datetime(2026, 6, 12, 12, 30, 0),
        )
        initialized, row_keys = service.extract_report_state(storage)

        self.assertTrue(initialized)
        self.assertEqual(row_keys, {old_item["row_key"], new_item["row_key"]})
        self.assertLess(storage.index("EMRM-500"), storage.index("SMECSC-100"))
        self.assertIn("Новый", storage)
        self.assertIn("Подтвержденный снимок", storage)

    def test_unchanged_row_key_set_skips_put_even_when_fields_changed(self):
        previous_item = self._item()
        previous_storage = service.build_unassigned_release_page_storage(
            [previous_item],
            new_row_keys={previous_item["row_key"]},
        )
        current_item = self._item(
            deployment_start="14.06.2026",
            release_version="D-99.999.99-changed",
            psi_owner="Другой дежурный",
        )

        with (
            patch.object(service, "_get_page_id", return_value="18655871248"),
            patch.object(
                service,
                "get_release_monitor_snapshot",
                return_value={"items": [current_item], "meta": {}},
            ),
            patch.object(
                service,
                "_fetch_page",
                return_value=self._page(previous_storage),
            ),
            patch.object(service, "_put_page") as put_page,
        ):
            result = service.sync_unassigned_release_confluence_page()

        self.assertFalse(result["updated"])
        self.assertEqual(result["message"], "Список не изменился, уведомление не отправлялось.")
        put_page.assert_not_called()

    def test_first_empty_report_is_written_once(self):
        with (
            patch.object(service, "_get_page_id", return_value="18655871248"),
            patch.object(
                service,
                "get_release_monitor_snapshot",
                return_value={"items": [], "meta": {}},
            ),
            patch.object(service, "_fetch_page", return_value=self._page("")),
            patch.object(
                service,
                "_put_page",
                return_value=FakeResponse(200, {"version": {"number": 5}}),
            ) as put_page,
        ):
            result = service.sync_unassigned_release_confluence_page()

        self.assertTrue(result["updated"])
        self.assertEqual(result["rows_count"], 0)
        rendered_storage = put_page.call_args.args[1]
        self.assertIn("На текущей неделе нет релизов", rendered_storage)

    def test_added_and_removed_rows_update_page(self):
        previous_item = self._item("SMECSC-100::SMECSC-101")
        current_item = self._item(
            "EMRM-500::EMRM-501",
            release_key="EMRM-500",
            rov_key="EMRM-501",
        )
        previous_storage = service.build_unassigned_release_page_storage(
            [previous_item],
            new_row_keys=set(),
        )

        with (
            patch.object(service, "_get_page_id", return_value="18655871248"),
            patch.object(
                service,
                "get_release_monitor_snapshot",
                return_value={"items": [current_item], "meta": {}},
            ),
            patch.object(
                service,
                "_fetch_page",
                return_value=self._page(previous_storage),
            ),
            patch.object(
                service,
                "_put_page",
                return_value=FakeResponse(200, {"version": {"number": 5}}),
            ) as put_page,
        ):
            result = service.sync_unassigned_release_confluence_page()

        self.assertTrue(result["updated"])
        self.assertEqual(result["new_rows_count"], 1)
        self.assertEqual(result["removed_rows_count"], 1)
        self.assertIn("Новый", put_page.call_args.args[1])

    def test_put_uses_minor_edit_false(self):
        page = self._page("")
        response = FakeResponse(200, {"version": {"number": 5}})

        with (
            patch.object(service, "_confluence_headers", return_value={"Authorization": "Bearer test"}),
            patch.object(service.requests, "put", return_value=response) as request_put,
        ):
            service._put_page(page, "<p>test</p>", "Тестовое обновление")

        payload = request_put.call_args.kwargs["json"]
        self.assertIs(payload["version"]["minorEdit"], False)
        self.assertEqual(payload["version"]["number"], 5)

    def test_version_conflict_refetches_and_retries_once(self):
        item = self._item()
        pages = [self._page(""), {**self._page(""), "version": 5}]
        responses = [
            FakeResponse(409, text="version conflict"),
            FakeResponse(200, {"version": {"number": 6}}),
        ]

        with (
            patch.object(service, "_get_page_id", return_value="18655871248"),
            patch.object(
                service,
                "get_release_monitor_snapshot",
                return_value={"items": [item], "meta": {}},
            ),
            patch.object(service, "_fetch_page", side_effect=pages) as fetch_page,
            patch.object(service, "_put_page", side_effect=responses) as put_page,
        ):
            result = service.sync_unassigned_release_confluence_page()

        self.assertTrue(result["updated"])
        self.assertEqual(fetch_page.call_count, 2)
        self.assertEqual(put_page.call_count, 2)


class ReleaseMonitorConfluenceNotificationRouteTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.register_blueprint(dashboard_bp)
        self.client = self.app.test_client()

    @patch("routes.dashboard_routes.sync_unassigned_release_confluence_page")
    def test_endpoint_returns_sync_result(self, sync_page):
        sync_page.return_value = {
            "updated": True,
            "rows_count": 3,
            "new_rows_count": 1,
            "removed_rows_count": 0,
            "page_url": "https://confluence.example/page",
            "message": "Страница обновлена.",
        }

        response = self.client.post(
            "/dashboard/release-monitor/confluence-unassigned-sync"
        )
        payload = response.get_json()

        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["success"])
        self.assertEqual(payload["rows_count"], 3)
        sync_page.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
