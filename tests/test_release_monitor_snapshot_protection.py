import copy
import hashlib
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from services import release_monitor_service as service


class ReleaseMonitorSnapshotProtectionTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.original_paths = {
            "SNAPSHOT_DIR": service.SNAPSHOT_DIR,
            "SNAPSHOT_FILE": service.SNAPSHOT_FILE,
            "LAST_GOOD_SNAPSHOT_FILE": service.LAST_GOOD_SNAPSHOT_FILE,
            "CANDIDATE_SNAPSHOT_FILE": service.CANDIDATE_SNAPSHOT_FILE,
            "SNAPSHOT_ARCHIVES_DIR": service.SNAPSHOT_ARCHIVES_DIR,
            "REVISION_FILE": service.REVISION_FILE,
            "MANUAL_RELEASES_FILE": service.MANUAL_RELEASES_FILE,
            "MANUAL_OVERRIDES_FILE": service.MANUAL_OVERRIDES_FILE,
            "REVIEWERS_FILE": service.REVIEWERS_FILE,
            "ORDER_FILE": service.ORDER_FILE,
            "DUTY_SCHEDULE_FILE": service.DUTY_SCHEDULE_FILE,
            "DATE_OVERRIDES_FILE": service.DATE_OVERRIDES_FILE,
            "ZNI_FILE": service.ZNI_FILE,
            "WORK_MARKS_FILE": service.WORK_MARKS_FILE,
            "ATTEMPTS_FILE": service.ATTEMPTS_FILE,
        }
        service.SNAPSHOT_DIR = root
        service.SNAPSHOT_FILE = root / "release_monitor_snapshot.json"
        service.LAST_GOOD_SNAPSHOT_FILE = root / "release_monitor_last_good.json"
        service.CANDIDATE_SNAPSHOT_FILE = root / "release_monitor_candidate.json"
        service.SNAPSHOT_ARCHIVES_DIR = root / "release_monitor_archives"
        service.REVISION_FILE = root / "release_monitor_revision.txt"
        service.MANUAL_RELEASES_FILE = root / "release_monitor_manual_releases.json"
        service.MANUAL_OVERRIDES_FILE = root / "release_monitor_manual_overrides.json"
        service.REVIEWERS_FILE = root / "release_monitor_reviewers.json"
        service.ORDER_FILE = root / "release_monitor_order.json"
        service.DUTY_SCHEDULE_FILE = root / "release_monitor_duty_schedule.json"
        service.DATE_OVERRIDES_FILE = root / "release_monitor_date_overrides.json"
        service.ZNI_FILE = root / "release_monitor_zni.json"
        service.WORK_MARKS_FILE = root / "release_monitor_work_marks.json"
        service.ATTEMPTS_FILE = root / "release_monitor_attempts.json"
        service._snapshot_recovery_checked = False
        service._snapshot_requires_display_migration = False

    def tearDown(self):
        for name, value in self.original_paths.items():
            setattr(service, name, value)
        service._snapshot_recovery_checked = False
        service._snapshot_requires_display_migration = False
        self.temp_dir.cleanup()

    @staticmethod
    def _item(index, prefix="EMRM", year=None):
        year = year or datetime.now().year
        release_key = f"{prefix}-{index}"
        rov_key = f"{prefix}-{10000 + index}"
        return {
            "release_key": release_key,
            "rov_key": rov_key,
            "row_key": f"{release_key}::{rov_key}",
            "source_prefix": prefix,
            "year": year,
        }

    @staticmethod
    def _hash(path):
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()

    def _accepted_payload(self, count, revision="100"):
        return service._prepare_accepted_snapshot(
            {
                "items": [self._item(index) for index in range(count)],
                "meta": {},
            },
            accepted_revision=revision,
            accepted_at="2026-06-01T10:00:00Z",
        )

    def test_operational_day_keeps_evening_window_until_0300(self):
        release_start = datetime(2026, 6, 9, 21, 0)
        release_end = datetime(2026, 6, 10, 1, 0)

        self.assertTrue(
            service._is_release_window_in_operational_day(
                release_start,
                release_end,
                datetime(2026, 6, 10, 2, 59),
            )
        )
        self.assertFalse(
            service._is_release_window_in_operational_day(
                release_start,
                release_end,
                datetime(2026, 6, 10, 3, 0),
            )
        )

    def test_operational_day_includes_release_started_after_midnight(self):
        self.assertTrue(
            service._is_release_window_in_operational_day(
                datetime(2026, 6, 10, 0, 30),
                datetime(2026, 6, 10, 2, 30),
                datetime(2026, 6, 10, 2, 0),
            )
        )

    def test_date_only_release_uses_operational_day_date(self):
        release_date = datetime(2026, 6, 9)

        self.assertTrue(
            service._is_release_window_in_operational_day(
                release_date,
                None,
                datetime(2026, 6, 10, 2, 0),
            )
        )
        self.assertFalse(
            service._is_release_window_in_operational_day(
                release_date,
                None,
                datetime(2026, 6, 10, 3, 0),
            )
        )

    def test_large_candidate_drop_is_rejected(self):
        baseline = {"items": [self._item(index) for index in range(456)]}
        candidate = service._build_raw_release_candidate(
            [self._item(index) for index in range(110)],
            [{"status": "success", "expected_total": 110, "fetched_total": 110}],
            "full",
        )

        report = service._validate_release_candidate(candidate, baseline)

        self.assertEqual("rejected", report["status"])
        reason_codes = {reason["code"] for reason in report["reasons"]}
        self.assertIn("total_drop", reason_codes)
        self.assertIn("current_year_drop", reason_codes)
        self.assertIn("real_rov_drop", reason_codes)

    def test_incomplete_pagination_and_disappeared_prefix_are_rejected(self):
        baseline = {
            "items": [
                *[self._item(index, prefix="EMRM") for index in range(20)],
                *[self._item(100 + index, prefix="SMECLM") for index in range(20)],
            ]
        }
        candidate = service._build_raw_release_candidate(
            [self._item(200 + index, prefix="SMECLM") for index in range(40)],
            [{"status": "success", "expected_total": 41, "fetched_total": 40}],
            "full",
        )

        report = service._validate_release_candidate(candidate, baseline)

        reason_codes = {reason["code"] for reason in report["reasons"]}
        self.assertEqual("rejected", report["status"])
        self.assertIn("incomplete_pagination", reason_codes)
        self.assertIn("prefix_disappeared", reason_codes)

    def test_year_rollover_compares_only_baseline_years_in_new_window(self):
        class January2026(datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2026, 1, 2, 12, 0, 0, tzinfo=tz)

        baseline = {
            "items": [
                *[self._item(index, year=2025) for index in range(100)],
                *[self._item(1000 + index, year=2024) for index in range(300)],
            ]
        }
        candidate_items = [
            *[self._item(index, year=2025) for index in range(100)],
            *[self._item(2000 + index, year=2026) for index in range(5)],
        ]

        with patch.object(service, "datetime", January2026):
            candidate = service._build_raw_release_candidate(
                candidate_items,
                [{"status": "success", "expected_total": 105, "fetched_total": 105}],
                "full",
            )
            report = service._validate_release_candidate(candidate, baseline)

        self.assertEqual("accepted", report["status"])
        self.assertEqual([2025], report["comparison_years"])
        self.assertEqual(100, report["baseline"]["total"])
        self.assertEqual(100, report["candidate"]["total"])
        self.assertNotIn(
            "current_year_drop",
            {reason["code"] for reason in report["reasons"]},
        )

    def test_year_rollover_still_rejects_loss_of_intersecting_year(self):
        class January2026(datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2026, 1, 2, 12, 0, 0, tzinfo=tz)

        baseline = {
            "items": [
                *[self._item(index, year=2025) for index in range(100)],
                *[self._item(1000 + index, year=2024) for index in range(300)],
            ]
        }
        candidate_items = [self._item(2000 + index, year=2026) for index in range(5)]

        with patch.object(service, "datetime", January2026):
            candidate = service._build_raw_release_candidate(
                candidate_items,
                [{"status": "success", "expected_total": 5, "fetched_total": 5}],
                "full",
            )
            report = service._validate_release_candidate(candidate, baseline)

        self.assertEqual("rejected", report["status"])
        self.assertEqual([2025], report["comparison_years"])
        self.assertIn("total_drop", {reason["code"] for reason in report["reasons"]})

    def test_rejected_candidate_does_not_read_overlays_or_change_good_files(self):
        baseline = self._accepted_payload(100)
        service._atomic_write_json(service.SNAPSHOT_FILE, baseline)
        service._atomic_write_json(service.LAST_GOOD_SNAPSHOT_FILE, baseline)
        service._atomic_write_text(service.REVISION_FILE, "100")
        state_files = (
            service.MANUAL_RELEASES_FILE,
            service.MANUAL_OVERRIDES_FILE,
            service.REVIEWERS_FILE,
            service.ORDER_FILE,
            service.DUTY_SCHEDULE_FILE,
            service.DATE_OVERRIDES_FILE,
            service.ZNI_FILE,
            service.WORK_MARKS_FILE,
            service.ATTEMPTS_FILE,
        )
        for state_file in state_files:
            service._atomic_write_json(state_file, {"sentinel": state_file.name})
        before = {
            path: self._hash(path)
            for path in (
                service.SNAPSHOT_FILE,
                service.LAST_GOOD_SNAPSHOT_FILE,
                service.REVISION_FILE,
                *state_files,
            )
        }
        candidate = service._build_raw_release_candidate(
            [self._item(index) for index in range(20)],
            [{"status": "success", "expected_total": 20, "fetched_total": 20}],
            "full",
        )

        with patch.object(service, "_fetch_release_monitor_data", return_value=copy.deepcopy(candidate)):
            with patch.object(
                service,
                "_load_manual_release_overrides",
                side_effect=AssertionError("overlay state must not be read"),
            ):
                with self.assertRaises(service.ReleaseMonitorCandidateRejected):
                    service._execute_transactional_full_refresh(
                        reliable=False,
                        base_payload=baseline,
                    )

        after = {path: self._hash(path) for path in before}
        self.assertEqual(before, after)
        self.assertTrue(service.CANDIDATE_SNAPSHOT_FILE.exists())

    def test_only_reliable_full_archives_previous_good_snapshot(self):
        initial = self._accepted_payload(1)
        service._atomic_write_json(service.SNAPSHOT_FILE, initial)
        service._atomic_write_json(service.LAST_GOOD_SNAPSHOT_FILE, initial)

        full_payload = service._commit_accepted_snapshot(
            {"items": [self._item(1), self._item(2)], "meta": {"marker": "full"}},
            mode="full",
        )
        self.assertFalse(service.SNAPSHOT_ARCHIVES_DIR.exists())

        reliable_payload = service._commit_accepted_snapshot(
            {
                "items": [self._item(1), self._item(2), self._item(3)],
                "meta": {"marker": "reliable"},
            },
            mode=service.RELIABLE_FULL_REFRESH_MODE,
        )
        archives = list(service.SNAPSHOT_ARCHIVES_DIR.glob("snapshot_*.json.gz"))
        self.assertEqual(1, len(archives))
        archived = service._load_archive_snapshot(archives[0])
        self.assertEqual(full_payload["items"], archived["items"])
        self.assertEqual(
            reliable_payload["meta"]["accepted_revision"],
            service._load_json_payload(service.SNAPSHOT_FILE)["meta"]["accepted_revision"],
        )
        self.assertEqual(
            service._load_json_payload(service.SNAPSHOT_FILE),
            service._load_json_payload(service.LAST_GOOD_SNAPSHOT_FILE),
        )

    def test_recovery_uses_newer_revision_and_then_valid_archive(self):
        active = self._accepted_payload(2, revision="200")
        last_good = self._accepted_payload(3, revision="300")
        service._atomic_write_json(service.SNAPSHOT_FILE, active)
        service._atomic_write_json(service.LAST_GOOD_SNAPSHOT_FILE, last_good)

        service._recover_snapshot_storage(force=True)
        self.assertEqual(
            service._load_json_payload(service.SNAPSHOT_FILE),
            service._load_json_payload(service.LAST_GOOD_SNAPSHOT_FILE),
        )
        self.assertEqual(
            "300",
            service._load_json_payload(service.SNAPSHOT_FILE)["meta"]["accepted_revision"],
        )

        service._archive_previous_good_snapshot(last_good)
        service.SNAPSHOT_FILE.write_text("{broken", encoding="utf-8")
        service.LAST_GOOD_SNAPSHOT_FILE.write_text("{broken", encoding="utf-8")
        service._snapshot_recovery_checked = False

        service._recover_snapshot_storage(force=True)
        recovered_active = service._load_json_payload(service.SNAPSHOT_FILE)
        recovered_last_good = service._load_json_payload(service.LAST_GOOD_SNAPSHOT_FILE)
        self.assertEqual(recovered_active, recovered_last_good)
        self.assertEqual("300", recovered_active["meta"]["accepted_revision"])

    def test_refresh_status_uses_live_auto_incremental_state(self):
        original_cached_data = service._cached_data
        original_auto_status = copy.deepcopy(service._auto_incremental_status)
        original_auto_thread = service._auto_incremental_thread
        try:
            service._cached_data = {
                "items": [self._item(1)],
                "summary": {},
                "meta": {
                    "auto_incremental_status": {
                        "state": "idle",
                        "running": False,
                    },
                },
            }
            service._auto_incremental_status.update(
                {
                    "state": "running",
                    "last_started_at": "06.06.2026 16:00:00",
                }
            )

            class AliveThread:
                @staticmethod
                def is_alive():
                    return True

            service._auto_incremental_thread = AliveThread()
            with patch.object(service, "_ensure_scheduler_started"):
                with patch.object(service, "_reload_snapshot_from_disk_if_newer"):
                    payload = service.get_release_monitor_refresh_status()

            status = payload["data"]["meta"]["auto_incremental_status"]
            self.assertEqual("running", status["state"])
            self.assertTrue(status["running"])
        finally:
            service._cached_data = original_cached_data
            service._auto_incremental_status.clear()
            service._auto_incremental_status.update(original_auto_status)
            service._auto_incremental_thread = original_auto_thread

    def test_startup_snapshot_load_uses_memory_load_time_for_cache_ttl(self):
        original_cached_data = service._cached_data
        original_last_cache_update = service._last_cache_update
        try:
            payload = self._accepted_payload(2, revision="400")
            service._atomic_write_json(service.SNAPSHOT_FILE, payload)
            service._atomic_write_json(service.LAST_GOOD_SNAPSHOT_FILE, payload)
            old_timestamp = 1_700_000_000
            service.SNAPSHOT_FILE.touch()
            service.LAST_GOOD_SNAPSHOT_FILE.touch()
            service.SNAPSHOT_FILE.chmod(0o666)
            service.LAST_GOOD_SNAPSHOT_FILE.chmod(0o666)
            import os
            os.utime(service.SNAPSHOT_FILE, (old_timestamp, old_timestamp))
            os.utime(service.LAST_GOOD_SNAPSHOT_FILE, (old_timestamp, old_timestamp))
            service._cached_data = None
            service._last_cache_update = None
            service._snapshot_recovery_checked = False

            before_load = datetime.now().timestamp()
            service._ensure_cached_payload_loaded_locked()

            self.assertIsNotNone(service._cached_data)
            self.assertGreaterEqual(service._last_cache_update, before_load)
            self.assertGreater(service._last_cache_update, service.SNAPSHOT_FILE.stat().st_mtime)
        finally:
            service._cached_data = original_cached_data
            service._last_cache_update = original_last_cache_update

    def test_startup_serves_stale_disk_snapshot_without_synchronous_full_refresh(self):
        original_cached_data = service._cached_data
        original_last_cache_update = service._last_cache_update
        try:
            payload = self._accepted_payload(2, revision="500")
            service._atomic_write_json(service.SNAPSHOT_FILE, payload)
            service._atomic_write_json(service.LAST_GOOD_SNAPSHOT_FILE, payload)
            old_timestamp = 1_700_000_000
            import os
            os.utime(service.SNAPSHOT_FILE, (old_timestamp, old_timestamp))
            os.utime(service.LAST_GOOD_SNAPSHOT_FILE, (old_timestamp, old_timestamp))
            service._cached_data = None
            service._last_cache_update = None
            service._snapshot_recovery_checked = False

            with patch.object(service, "_ensure_scheduler_started"):
                with patch.object(
                    service,
                    "_execute_transactional_full_refresh",
                    side_effect=AssertionError("startup must not run synchronous full refresh"),
                ):
                    result = service.get_release_monitor_data()

            self.assertEqual(2, len(result["items"]))
        finally:
            service._cached_data = original_cached_data
            service._last_cache_update = original_last_cache_update


if __name__ == "__main__":
    unittest.main()
