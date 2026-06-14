import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from flask import Flask

from routes.dashboard_routes import dashboard_bp
from services import release_monitor_backup_service as backup_service
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
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.state_file_patcher = patch.object(
            service,
            "NOTIFY_STATE_FILE",
            root / "release_monitor_unassigned_notify_state.json",
        )
        self.lock_file_patcher = patch.object(
            service,
            "AUTO_SYNC_LOCK_FILE",
            root / "release_monitor_unassigned_notify_state.lock",
        )
        self.state_file_patcher.start()
        self.lock_file_patcher.start()
        service._last_observed_auto_sync_enabled = None
        service._queued_auto_sync_job = None
        service._auto_sync_worker_thread = None

    def tearDown(self):
        self.lock_file_patcher.stop()
        self.state_file_patcher.stop()
        self.temp_dir.cleanup()

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
    def _snapshot(*items):
        return {
            "items": list(items),
            "meta": {
                "data_revision": "20260612093000",
                "accepted_at": "2026-06-12T09:30:00",
            },
        }

    @staticmethod
    def _page(storage_html=""):
        return {
            "page_id": "18655871248",
            "title": "Информирование о новых релизах",
            "version": 4,
            "storage_html": storage_html,
        }

    def _read_state(self):
        with service.NOTIFY_STATE_FILE.open("r", encoding="utf-8") as handle:
            return json.load(handle)

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
            patch.object(service, "_fetch_page", side_effect=pages) as fetch_page,
            patch.object(service, "_put_page", side_effect=responses) as put_page,
        ):
            result = service._sync_unassigned_release_confluence_page(
                self._snapshot(item),
                new_row_keys={item["row_key"]},
            )

        self.assertTrue(result["updated"])
        self.assertEqual(fetch_page.call_count, 2)
        self.assertEqual(put_page.call_count, 2)

    def test_disabled_flag_does_not_create_or_change_state(self):
        with (
            patch.object(service, "is_automation_enabled", return_value=False),
            patch.object(service, "_sync_unassigned_release_confluence_page") as sync_page,
        ):
            result = service._run_unassigned_auto_sync(
                self._snapshot(self._item()),
                refresh_mode="silent",
            )

        self.assertEqual(result["result"], "disabled")
        self.assertFalse(service.NOTIFY_STATE_FILE.exists())
        sync_page.assert_not_called()

    def test_status_polling_does_not_create_baseline_or_state(self):
        with patch.object(service, "is_automation_enabled", return_value=True):
            status = service.get_unassigned_auto_sync_status()

        self.assertEqual(status["status"], "waiting_refresh")
        self.assertFalse(service.NOTIFY_STATE_FILE.exists())

    def test_first_successful_refresh_creates_baseline_without_confluence(self):
        item = self._item()
        with (
            patch.object(service, "is_automation_enabled", return_value=True),
            patch.object(service, "_current_week_key", return_value="2026-W24"),
            patch.object(service, "_sync_unassigned_release_confluence_page") as sync_page,
        ):
            result = service._run_unassigned_auto_sync(
                self._snapshot(item),
                refresh_mode="silent",
            )

        state = self._read_state()
        self.assertEqual(result["result"], "baseline_created")
        self.assertEqual(state["week_key"], "2026-W24")
        self.assertEqual(state["notified_row_keys"], [item["row_key"]])
        self.assertEqual(state["active_row_keys"], [item["row_key"]])
        sync_page.assert_not_called()

    def test_new_row_after_baseline_updates_page_and_notified_state(self):
        old_item = self._item()
        new_item = self._item("EMRM-500::EMRM-501")
        with (
            patch.object(service, "is_automation_enabled", return_value=True),
            patch.object(service, "_current_week_key", return_value="2026-W24"),
        ):
            service._run_unassigned_auto_sync(
                self._snapshot(old_item),
                refresh_mode="silent",
            )
            with patch.object(
                service,
                "_sync_unassigned_release_confluence_page",
                return_value={"updated": True},
            ) as sync_page:
                result = service._run_unassigned_auto_sync(
                    self._snapshot(old_item, new_item),
                    refresh_mode="quick",
                )

        state = self._read_state()
        self.assertEqual(result["result"], "updated")
        self.assertEqual(result["new_rows_count"], 1)
        self.assertEqual(
            set(state["notified_row_keys"]),
            {old_item["row_key"], new_item["row_key"]},
        )
        self.assertEqual(state["pending_row_keys"], [])
        sync_page.assert_called_once()
        self.assertEqual(
            sync_page.call_args.kwargs["new_row_keys"],
            {new_item["row_key"]},
        )

    def test_week_change_creates_new_baseline_without_put(self):
        old_item = self._item()
        new_week_item = self._item("EMRM-500::EMRM-501")
        with patch.object(service, "is_automation_enabled", return_value=True):
            with patch.object(service, "_current_week_key", return_value="2026-W24"):
                service._run_unassigned_auto_sync(
                    self._snapshot(old_item),
                    refresh_mode="silent",
                )
            with (
                patch.object(service, "_current_week_key", return_value="2026-W25"),
                patch.object(service, "_sync_unassigned_release_confluence_page") as sync_page,
            ):
                result = service._run_unassigned_auto_sync(
                    self._snapshot(new_week_item),
                    refresh_mode="full",
                )

        state = self._read_state()
        self.assertEqual(result["result"], "weekly_baseline_created")
        self.assertEqual(state["week_key"], "2026-W25")
        self.assertEqual(state["notified_row_keys"], [new_week_item["row_key"]])
        sync_page.assert_not_called()

    def test_confluence_error_keeps_new_row_pending(self):
        old_item = self._item()
        new_item = self._item("EMRM-500::EMRM-501")
        with (
            patch.object(service, "is_automation_enabled", return_value=True),
            patch.object(service, "_current_week_key", return_value="2026-W24"),
        ):
            service._run_unassigned_auto_sync(
                self._snapshot(old_item),
                refresh_mode="silent",
            )
            with patch.object(
                service,
                "_sync_unassigned_release_confluence_page",
                side_effect=ValueError("Confluence unavailable"),
            ):
                result = service._run_unassigned_auto_sync(
                    self._snapshot(old_item, new_item),
                    refresh_mode="silent",
                )

        state = self._read_state()
        self.assertEqual(result["result"], "error")
        self.assertEqual(state["pending_row_keys"], [new_item["row_key"]])
        self.assertEqual(state["notified_row_keys"], [old_item["row_key"]])
        self.assertIn("Confluence unavailable", state["last_error"])

    def test_throttle_preserves_pending_error_until_real_retry(self):
        old_item = self._item()
        new_item = self._item("EMRM-500::EMRM-501")
        with (
            patch.object(service, "is_automation_enabled", return_value=True),
            patch.object(service, "_current_week_key", return_value="2026-W24"),
        ):
            service._run_unassigned_auto_sync(
                self._snapshot(old_item),
                refresh_mode="silent",
            )
            with patch.object(
                service,
                "_sync_unassigned_release_confluence_page",
                side_effect=ValueError("Confluence unavailable"),
            ):
                service._run_unassigned_auto_sync(
                    self._snapshot(old_item, new_item),
                    refresh_mode="silent",
                )
            with patch.object(service, "_sync_unassigned_release_confluence_page") as sync_page:
                result = service._run_unassigned_auto_sync(
                    self._snapshot(old_item, new_item),
                    refresh_mode="silent",
                )

        state = self._read_state()
        self.assertEqual(result["result"], "throttled")
        self.assertEqual(state["pending_row_keys"], [new_item["row_key"]])
        self.assertIn("Confluence unavailable", state["last_error"])
        sync_page.assert_not_called()

    def test_removed_or_assigned_row_does_not_trigger_page_update(self):
        first = self._item()
        second = self._item("EMRM-500::EMRM-501")
        second_assigned = dict(
            second,
            psi_responsibles=["Кашкин С.Н."],
            is_missing_week_responsible=False,
        )
        with (
            patch.object(service, "is_automation_enabled", return_value=True),
            patch.object(service, "_current_week_key", return_value="2026-W24"),
        ):
            service._run_unassigned_auto_sync(
                self._snapshot(first, second),
                refresh_mode="silent",
            )
            with patch.object(service, "_sync_unassigned_release_confluence_page") as sync_page:
                result = service._run_unassigned_auto_sync(
                    self._snapshot(first, second_assigned),
                    refresh_mode="silent",
                )

        state = self._read_state()
        self.assertEqual(result["result"], "waiting")
        self.assertEqual(state["active_row_keys"], [first["row_key"]])
        self.assertEqual(
            set(state["notified_row_keys"]),
            {first["row_key"], second["row_key"]},
        )
        sync_page.assert_not_called()

    def test_notified_row_reappearing_in_same_week_does_not_notify_again(self):
        item = self._item()
        with (
            patch.object(service, "is_automation_enabled", return_value=True),
            patch.object(service, "_current_week_key", return_value="2026-W24"),
        ):
            service._run_unassigned_auto_sync(
                self._snapshot(item),
                refresh_mode="silent",
            )
            service._run_unassigned_auto_sync(
                self._snapshot(),
                refresh_mode="silent",
            )
            with patch.object(service, "_sync_unassigned_release_confluence_page") as sync_page:
                result = service._run_unassigned_auto_sync(
                    self._snapshot(item),
                    refresh_mode="silent",
                )

        self.assertEqual(result["result"], "waiting")
        sync_page.assert_not_called()

    def test_enabling_after_disabled_observation_forces_baseline(self):
        class FakeThread:
            def __init__(self, **kwargs):
                self.kwargs = kwargs
                self.started = False

            def is_alive(self):
                return False

            def start(self):
                self.started = True

        service._last_observed_auto_sync_enabled = False
        with (
            patch.object(service, "is_automation_enabled", return_value=True),
            patch.object(service.threading, "Thread", FakeThread),
        ):
            scheduled = service.schedule_unassigned_auto_sync(
                self._snapshot(self._item()),
                refresh_mode="silent",
            )

        self.assertTrue(scheduled)
        self.assertTrue(service._queued_auto_sync_job["force_baseline"])
        self.assertTrue(service._auto_sync_worker_thread.started)

    def test_notify_state_is_included_in_cache_backup(self):
        backup_names = {path.name for path in backup_service.BACKUP_FILES}
        self.assertIn("release_monitor_unassigned_notify_state.json", backup_names)


class ReleaseMonitorConfluenceNotificationRouteTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.register_blueprint(dashboard_bp)
        self.client = self.app.test_client()

    def test_manual_endpoint_is_removed(self):
        response = self.client.post(
            "/dashboard/release-monitor/confluence-unassigned-sync"
        )
        self.assertEqual(response.status_code, 404)

    def test_full_confluence_export_route_still_exists(self):
        rules = {rule.rule for rule in self.app.url_map.iter_rules()}
        self.assertIn("/dashboard/release-monitor/confluence-sync", rules)


if __name__ == "__main__":
    unittest.main()
