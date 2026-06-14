import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from services import feature_flags_service as service


class FeatureFlagsServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.flags_file = Path(self.temp_dir.name) / "feature_flags.json"
        self.file_patcher = patch.object(service, "FEATURE_FLAGS_FILE", self.flags_file)
        self.file_patcher.start()
        service._cached_mtime_ns = None
        service._last_load_error_key = None
        service._cached_flags = service._normalize_flags({})

    def tearDown(self):
        self.file_patcher.stop()
        self.temp_dir.cleanup()

    def _write_flags(self, payload):
        self.flags_file.write_text(
            json.dumps(payload, ensure_ascii=False),
            encoding="utf-8",
        )

    def test_missing_file_uses_safe_false_defaults(self):
        flags = service.get_feature_flags()
        self.assertFalse(flags["maintenance"]["index"])
        self.assertFalse(
            flags["automation"]["confluence_unassigned_auto_sync"]
        )

    def test_flags_reload_when_file_changes(self):
        self._write_flags(
            {
                "maintenance": {"release_monitor": True},
                "automation": {"confluence_unassigned_auto_sync": False},
            }
        )
        self.assertTrue(service.is_maintenance_enabled("release_monitor"))
        self.assertFalse(
            service.is_automation_enabled("confluence_unassigned_auto_sync")
        )

        self._write_flags(
            {
                "maintenance": {"release_monitor": False},
                "automation": {"confluence_unassigned_auto_sync": True},
            }
        )
        self.flags_file.touch()

        self.assertFalse(service.is_maintenance_enabled("release_monitor"))
        self.assertTrue(
            service.is_automation_enabled("confluence_unassigned_auto_sync")
        )

    def test_invalid_json_uses_safe_false_defaults(self):
        self.flags_file.write_text("{broken", encoding="utf-8")
        flags = service.get_feature_flags()
        self.assertFalse(flags["maintenance"]["chatbot"])
        self.assertFalse(
            flags["automation"]["confluence_unassigned_auto_sync"]
        )

    def test_non_boolean_values_do_not_enable_flags(self):
        self._write_flags(
            {
                "maintenance": {"index": "true"},
                "automation": {"confluence_unassigned_auto_sync": 1},
            }
        )
        self.assertFalse(service.is_maintenance_enabled("index"))
        self.assertFalse(
            service.is_automation_enabled("confluence_unassigned_auto_sync")
        )


if __name__ == "__main__":
    unittest.main()
