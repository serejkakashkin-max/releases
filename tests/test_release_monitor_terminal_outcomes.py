import unittest
from unittest import mock

from services import release_monitor_service as monitor


def _row(*, status, ke="CI12345678", is_reroll=False, rov_status="Утвержден"):
    return {
        "row_key": "REL-1::ROV-1",
        "release_key": "REL-1",
        "release_status": status,
        "rov_key": "ROV-1",
        "rov_status": rov_status,
        "has_rov": True,
        "ke": ke,
        "is_reroll": is_reroll,
        "is_overdue": True,
        "deployment_end": "01.08.2026",
        "deployment_end_iso": "2026-08-01T23:00:00",
        "sort_date": "2026-08-01T23:00:00",
        "year": 2026,
    }


def _deferred_outcome():
    return {
        "REL-1::ROV-1": {
            "state": "deferred",
            "release_key": "REL-1",
            "rov_key": "ROV-1",
            "detected_at": "01.08.2026 23:01:00",
            "updated_at": "01.08.2026 23:01:00",
        }
    }


class ReleaseMonitorTerminalOutcomeTests(unittest.TestCase):
    def _apply_attempts(self, item, outcomes=None):
        with mock.patch.object(
            monitor,
            "_load_release_attempt_outcomes",
            return_value=outcomes if outcomes is not None else _deferred_outcome(),
        ), mock.patch.object(monitor, "_save_release_attempt_outcomes"):
            monitor._apply_release_attempt_outcomes([item])

    def test_cancelled_after_work_stays_numbered_but_is_not_overdue(self):
        for status in ("Отменен", "Отменён", "Отменено"):
            with self.subTest(status=status):
                item = _row(status=status, rov_status=status)
                monitor._apply_release_status_consistency([item])
                self._apply_attempts(item)
                monitor._apply_force_unnumbered_flags(2026, [item], manual_order={})

                self.assertTrue(item["is_cancelled"])
                self.assertFalse(item["is_final"])
                self.assertFalse(item["is_overdue"])
                self.assertEqual("cancelled", item["row_state"])
                self.assertTrue(item["is_deferred_attempt"])
                self.assertTrue(item["is_deferred_resolved"])
                self.assertFalse(item["is_unnumbered"])

    def test_active_failed_attempt_remains_overdue(self):
        item = _row(status="Формирование")
        monitor._apply_release_status_consistency([item])
        self._apply_attempts(item)

        self.assertFalse(item["is_cancelled"])
        self.assertFalse(item["is_final"])
        self.assertTrue(item["is_overdue"])
        self.assertEqual("overdue", item["row_state"])

    def test_installed_terminal_attempt_is_not_overdue(self):
        item = _row(status=monitor.FINAL_RELEASE_STATUS)
        monitor._apply_release_status_consistency([item])
        self._apply_attempts(item)

        self.assertTrue(item["is_final"])
        self.assertFalse(item["is_overdue"])
        self.assertEqual("final", item["row_state"])

    def test_cancelled_without_ke_keeps_existing_unnumbered_rule(self):
        item = _row(status="Отменено", ke="")
        monitor._apply_release_status_consistency([item])
        self._apply_attempts(item, outcomes={})
        monitor._apply_force_unnumbered_flags(2026, [item], manual_order={})

        self.assertTrue(item["is_unnumbered"])

    def test_cancelled_reroll_and_force_numbered_rules_are_unchanged(self):
        item = _row(
            status=monitor.FINAL_RELEASE_STATUS,
            is_reroll=True,
            rov_status="Отменен",
        )
        item.update({"is_final": True, "is_cancelled": False, "is_non_final": False})
        monitor._apply_force_unnumbered_flags(2026, [item], manual_order={})
        self.assertTrue(item["is_natural_unnumbered"])
        self.assertTrue(item["is_unnumbered"])

        forced = _row(
            status=monitor.FINAL_RELEASE_STATUS,
            is_reroll=True,
            rov_status="Отменен",
        )
        forced.update({"is_final": True, "is_cancelled": False, "is_non_final": False})
        monitor._apply_force_unnumbered_flags(
            2026,
            [forced],
            manual_order={"2026": {"force_numbered": [forced["row_key"]]}},
        )
        self.assertTrue(forced["is_force_numbered"])
        self.assertFalse(forced["is_unnumbered"])


if __name__ == "__main__":
    unittest.main()
