import copy
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from services import release_monitor_service as service


class ReleaseAssignmentCenterTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.original_paths = {
            "SNAPSHOT_DIR": service.SNAPSHOT_DIR,
            "SNAPSHOT_FILE": service.SNAPSHOT_FILE,
            "REVIEWERS_FILE": service.REVIEWERS_FILE,
            "REVISION_FILE": service.REVISION_FILE,
            "DUTY_SCHEDULE_FILE": service.DUTY_SCHEDULE_FILE,
        }
        self.original_cached_data = service._cached_data
        service.SNAPSHOT_DIR = root
        service.SNAPSHOT_FILE = root / "release_monitor_snapshot.json"
        service.REVIEWERS_FILE = root / "release_monitor_reviewers.json"
        service.REVISION_FILE = root / "release_monitor_revision.txt"
        service.DUTY_SCHEDULE_FILE = root / "release_monitor_duty_schedule.json"
        self.first_person = service.OPLOT_VALUES[0]
        self.second_person = service.OPLOT_VALUES[1]

    def tearDown(self):
        for name, value in self.original_paths.items():
            setattr(service, name, value)
        service._cached_data = self.original_cached_data
        self.temp_dir.cleanup()

    @staticmethod
    def _item(row_key, date_value, responsible="", **extra):
        release_key, rov_key = row_key.split("::", 1)
        item = {
            "row_key": row_key,
            "release_key": release_key,
            "rov_key": rov_key,
            "deployment_start": date_value,
            "deployment_start_iso": "",
            "release_summary": f"Release {release_key}",
            "release_status": "Формирование",
            "system_name": "CLM",
            "psi_responsibles": [responsible] if responsible else [],
            "psi_owner": "Дежурный",
            "psi_owner_source": "manual",
            "psi_checker": "Проверяющий",
            "is_cancelled": False,
            "is_final": False,
        }
        item.update(extra)
        return item

    def test_assignment_statistics_count_rows_by_effective_date(self):
        reference = datetime(2026, 6, 10, 12, 0)
        items = [
            self._item("EMRM-1::EMRM-101", "08.06.2026", self.first_person),
            self._item("EMRM-2::EMRM-102", "09.06.2026", self.first_person, is_final=True),
            self._item("EMRM-3::EMRM-103", "15.05.2026", self.first_person),
            self._item("EMRM-4::EMRM-104", "15.02.2026", self.first_person),
            self._item("EMRM-5::EMRM-105", "10.06.2026", self.first_person, is_cancelled=True),
        ]

        stats = service._release_assignment_center_period_stats(items, reference_dt=reference)

        self.assertEqual(stats[self.first_person]["active"], 1)
        self.assertEqual(stats[self.first_person]["week"], 2)
        self.assertEqual(stats[self.first_person]["quarter"], 3)
        self.assertEqual(stats[self.first_person]["year"], 4)

    def test_narrow_assignment_preserves_other_fields_and_detects_conflict(self):
        row_key = "EMRM-10::EMRM-110"
        item = self._item(row_key, datetime.now().strftime("%d.%m.%Y"))
        service._cached_data = {
            "items": [copy.deepcopy(item)],
            "summary": {},
            "meta": {"data_revision": "before"},
        }
        service.REVIEWERS_FILE.write_text(
            json.dumps(
                {
                    row_key: {
                        "reviewer": "Дежурный",
                        "reviewer_source": "manual",
                        "reviewer_date": "manual",
                        "zni_reviewer": "",
                        "checker": "Проверяющий",
                        "responsibles": [],
                    }
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        saved = service.assign_release_monitor_responsible_if_expected(
            row_key,
            self.first_person,
            expected_responsibles=[],
        )

        self.assertEqual(saved["reviewer"], "Дежурный")
        self.assertEqual(saved["checker"], "Проверяющий")
        self.assertEqual(saved["responsibles"], [self.first_person])
        self.assertEqual(service._cached_data["items"][0]["psi_responsibles"], [self.first_person])
        self.assertFalse(service._cached_data["items"][0]["is_missing_week_responsible"])

        with self.assertRaises(service.ReleaseMonitorAssignmentConflict) as context:
            service.assign_release_monitor_responsible_if_expected(
                row_key,
                self.second_person,
                expected_responsibles=[],
            )
        self.assertEqual(context.exception.assignment["responsibles"], [self.first_person])

        persisted = json.loads(service.REVIEWERS_FILE.read_text(encoding="utf-8"))
        self.assertEqual(persisted[row_key]["responsibles"], [self.first_person])
        self.assertEqual(persisted[row_key]["checker"], "Проверяющий")

    def test_same_assignment_is_idempotent(self):
        row_key = "SMECLM-20::SMECLM-120"
        item = self._item(row_key, datetime.now().strftime("%d.%m.%Y"), self.first_person)
        service._cached_data = {
            "items": [copy.deepcopy(item)],
            "summary": {},
            "meta": {"data_revision": "same"},
        }
        service.REVIEWERS_FILE.write_text(
            json.dumps(
                {
                    row_key: {
                        "reviewer": "",
                        "reviewer_source": "",
                        "reviewer_date": "",
                        "zni_reviewer": "",
                        "checker": "",
                        "responsibles": [self.first_person],
                    }
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        saved = service.assign_release_monitor_responsible_if_expected(
            row_key,
            self.first_person,
            expected_responsibles=[],
        )

        self.assertTrue(saved["idempotent"])
        self.assertEqual(saved["responsibles"], [self.first_person])

    def test_snapshot_assignment_prevents_overwrite_when_state_entry_is_missing(self):
        row_key = "SMECSC-25::SMECSC-125"
        item = self._item(
            row_key,
            datetime.now().strftime("%d.%m.%Y"),
            self.first_person,
        )
        service._cached_data = {
            "items": [copy.deepcopy(item)],
            "summary": {},
            "meta": {"data_revision": "snapshot-only"},
        }

        with self.assertRaises(service.ReleaseMonitorAssignmentConflict) as context:
            service.assign_release_monitor_responsible_if_expected(
                row_key,
                self.second_person,
                expected_responsibles=[],
            )

        self.assertEqual(
            context.exception.assignment["responsibles"],
            [self.first_person],
        )
        self.assertFalse(service.REVIEWERS_FILE.exists())

    def test_center_data_uses_snapshot_and_enriches_missing_rows(self):
        week_start, _ = service._get_current_week_bounds()
        deployment_date = week_start.strftime("%d.%m.%Y")
        missing = self._item(
            "AIGAS-30::AIGAS-130",
            deployment_date,
            ke_id="123456",
            release_version="D-01.002.03-test",
        )
        assigned = self._item(
            "AIGAS-31::AIGAS-131",
            (week_start + timedelta(days=1)).strftime("%d.%m.%Y"),
            self.first_person,
        )
        snapshot = {
            "items": [missing, assigned],
            "summary": {},
            "meta": {
                "data_revision": "revision-1",
                "accepted_revision": "accepted-1",
                "accepted_at": "2026-06-14T10:00:00Z",
            },
        }

        with patch.object(service, "get_release_monitor_snapshot", return_value=snapshot):
            payload = service.get_release_monitor_assignment_center_data()

        self.assertEqual(payload["meta"]["data_revision"], "revision-1")
        self.assertEqual(payload["statistics"]["missing_responsible"], 1)
        self.assertEqual(payload["missing_responsible"][0]["row_key"], missing["row_key"])
        self.assertEqual(payload["missing_responsible"][0]["ke_id"], "123456")
        self.assertEqual(
            payload["employee_metrics"][self.first_person]["week"],
            1,
        )
        self.assertTrue(payload["meta"]["view_revision"])


if __name__ == "__main__":
    unittest.main()
