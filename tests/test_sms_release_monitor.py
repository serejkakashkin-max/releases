import io
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from flask import Flask

from routes.sms_routes import sms_bp
from services import sms_service


class ReleaseMonitorSmsTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.original_root = sms_service.SMS_TEMPLATES_ROOT
        sms_service.SMS_TEMPLATES_ROOT = self.root
        self.app = Flask(__name__)
        self.app.register_blueprint(sms_bp)
        self.client = self.app.test_client()

    def tearDown(self):
        sms_service.SMS_TEMPLATES_ROOT = self.original_root
        self.temp_dir.cleanup()

    def _write_template(self, profile):
        path = self.root / f"Получатели ({profile}).csv"
        path.write_text("79000000000;old text\n", encoding="cp1251")
        return path

    @staticmethod
    def _item(profile="CLM", release_key="SMECLM-1"):
        return {
            "row_key": f"{release_key}::{release_key}-ROV",
            "release_key": release_key,
            "rov_key": f"{release_key}-ROV",
            "result": "success",
            "profile": profile,
            "text": "Тестовое SMS",
        }

    def test_profile_resolver_rejects_unknown_and_paths(self):
        self._write_template("CLM")
        with self.assertRaises(ValueError):
            sms_service.resolve_sms_profile_template("../CLM")
        with self.assertRaises(ValueError):
            sms_service.resolve_sms_profile_template("template.csv")

    def test_missing_profile_template_is_controlled_error(self):
        with self.assertRaises(FileNotFoundError):
            sms_service.resolve_sms_profile_template("CLM")

    @patch("routes.sms_routes.increment_counter")
    def test_generate_groups_csv_by_profile(self, increment_counter):
        self._write_template("CLM")
        self._write_template("AI")
        payload = {
            "items": [
                self._item("CLM", "SMECLM-100"),
                self._item("AI", "AIGAS-200"),
            ]
        }

        response = self.client.post("/sms/release-monitor/generate", json=payload)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "application/zip")
        with zipfile.ZipFile(io.BytesIO(response.data)) as archive:
            names = archive.namelist()
            self.assertTrue(any(name.startswith("CLM/") for name in names))
            self.assertTrue(any(name.startswith("AI/") for name in names))
            for name in names:
                content = archive.read(name).decode("cp1251")
                self.assertIn("Тестовое SMS", content)
        increment_counter.assert_called_once_with("sms")

    def test_generate_rejects_frontend_template_path(self):
        self._write_template("CLM")
        item = self._item()
        item["template_path"] = "../secret.csv"

        response = self.client.post("/sms/release-monitor/generate", json={"items": [item]})

        self.assertEqual(response.status_code, 400)
        self.assertIn("запрещены", response.get_json()["errors"][0])

    def test_generate_rejects_unknown_profile(self):
        response = self.client.post(
            "/sms/release-monitor/generate",
            json={"items": [self._item("../CLM")]},
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("Неизвестный профиль", " ".join(response.get_json()["errors"]))

    def test_generate_rejects_missing_template(self):
        response = self.client.post(
            "/sms/release-monitor/generate",
            json={"items": [self._item("EMRM")]},
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("не найден", " ".join(response.get_json()["errors"]))


if __name__ == "__main__":
    unittest.main()
