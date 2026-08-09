from __future__ import annotations

import builtins
import sys
import threading
import types
import unittest
from unittest import mock

from tests._support import PROJECT_ROOT, prepare_config_import

prepare_config_import()

from services.feature_flags_service import DEFAULT_FEATURE_FLAGS, _normalize_flags
from services.gigachat_service import (
    GigaChatDisabledError,
    GigaChatHelper,
)
from services.sup_parameters_service import (
    SupParametersValidationError,
    _admin_config_from_payload,
    _merge_managed_config,
    _validate_managed_config,
)


class _Response:
    choices = [types.SimpleNamespace(message=types.SimpleNamespace(content="ok"))]


class _SslContext:
    def load_cert_chain(self, **_kwargs):
        return None

    def load_verify_locations(self, **_kwargs):
        return None


class GigaChatFeatureFlagTests(unittest.TestCase):
    def test_default_and_missing_or_invalid_values_are_enabled(self):
        self.assertTrue(DEFAULT_FEATURE_FLAGS["integrations"]["gigachat"]["enabled"])
        self.assertTrue(_normalize_flags({})["integrations"]["gigachat"]["enabled"])
        self.assertTrue(
            _normalize_flags({"integrations": {"gigachat": {"enabled": "false"}}})[
                "integrations"
            ]["gigachat"]["enabled"]
        )
        self.assertFalse(
            _normalize_flags({"integrations": {"gigachat": {"enabled": False}}})[
                "integrations"
            ]["gigachat"]["enabled"]
        )

    def test_sup_schema_round_trip_is_strict_and_preserves_unknown_config(self):
        managed = _admin_config_from_payload({})
        self.assertTrue(managed["integrations"]["gigachat"]["enabled"])
        managed["integrations"]["gigachat"]["enabled"] = False
        validated = _validate_managed_config(managed)
        merged = _merge_managed_config({"private_section": {"keep": True}}, validated)
        self.assertFalse(merged["integrations"]["gigachat"]["enabled"])
        self.assertEqual({"keep": True}, merged["private_section"])

        managed["integrations"]["gigachat"]["enabled"] = "false"
        with self.assertRaises(SupParametersValidationError):
            _validate_managed_config(managed)

    def test_disabled_path_does_not_import_sdk_touch_certificates_or_construct(self):
        helper = GigaChatHelper()
        real_import = builtins.__import__
        imported = []

        def guarded_import(name, *args, **kwargs):
            if name == "gigachat" or name.startswith("gigachat."):
                imported.append(name)
                raise AssertionError("GigaChat SDK must not be imported while disabled")
            return real_import(name, *args, **kwargs)

        with (
            mock.patch("services.gigachat_service.is_gigachat_enabled", return_value=False),
            mock.patch("pathlib.Path.is_file", side_effect=AssertionError("certificate access")),
            mock.patch("builtins.__import__", side_effect=guarded_import),
        ):
            self.assertEqual(
                {"enabled": False, "available": False, "state": "disabled"},
                helper.get_status(),
            )
            with self.assertRaises(GigaChatDisabledError):
                helper.chat("never sent")
        self.assertEqual([], imported)

    def test_enabled_client_is_lazy_and_uses_installed_sdk_argument(self):
        calls = []

        class FakeClient:
            def __init__(self, **kwargs):
                calls.append(kwargs)

            def chat(self, _prompt):
                return _Response()

            def close(self):
                return None

        helper = GigaChatHelper()
        fake_module = types.SimpleNamespace(GigaChat=FakeClient)
        with (
            mock.patch("services.gigachat_service.is_gigachat_enabled", return_value=True),
            mock.patch.dict(sys.modules, {"gigachat": fake_module}),
            mock.patch("pathlib.Path.is_file", return_value=True),
            mock.patch("services.gigachat_service.ssl.create_default_context", return_value=_SslContext()),
        ):
            self.assertIsNone(helper.client)
            self.assertEqual([], calls)
            self.assertEqual("ok", helper.chat("hello").choices[0].message.content)
        self.assertEqual(1, len(calls))
        self.assertTrue(calls[0]["verify_ssl_certs"])
        self.assertNotIn("verify_ssl", calls[0])

    def test_off_on_transition_retires_old_client_and_creates_fresh_one(self):
        enabled = {"value": True}
        created = []

        class FakeClient:
            def __init__(self):
                self.closed = False

            def chat(self, _prompt):
                return _Response()

            def close(self):
                self.closed = True

        helper = GigaChatHelper()

        def build():
            client = FakeClient()
            created.append(client)
            return client

        with (
            mock.patch(
                "services.gigachat_service.is_gigachat_enabled",
                side_effect=lambda: enabled["value"],
            ),
            mock.patch.object(helper, "_build_client", side_effect=build),
        ):
            helper.chat("first")
            enabled["value"] = False
            helper.sync_runtime_state()
            self.assertTrue(created[0].closed)
            with self.assertRaises(GigaChatDisabledError):
                helper.chat("blocked")
            enabled["value"] = True
            helper.sync_runtime_state()
            helper.chat("second")
        self.assertEqual(2, len(created))
        self.assertIsNot(created[0], created[1])

    def test_in_flight_call_can_finish_after_off_transition(self):
        entered = threading.Event()
        release = threading.Event()
        enabled = {"value": True}

        class FakeClient:
            closed = False

            def chat(self, _prompt):
                entered.set()
                release.wait(2)
                return _Response()

            def close(self):
                self.closed = True

        client = FakeClient()
        helper = GigaChatHelper()
        result = []
        with (
            mock.patch(
                "services.gigachat_service.is_gigachat_enabled",
                side_effect=lambda: enabled["value"],
            ),
            mock.patch.object(helper, "_build_client", return_value=client),
        ):
            worker = threading.Thread(target=lambda: result.append(helper.chat("in flight")))
            worker.start()
            self.assertTrue(entered.wait(1))
            enabled["value"] = False
            helper.sync_runtime_state()
            self.assertFalse(client.closed)
            release.set()
            worker.join(2)
        self.assertEqual(1, len(result))
        self.assertTrue(client.closed)


class GigaChatPresentationContractTests(unittest.TestCase):
    def test_sup_and_assignment_frontends_expose_controlled_switch_contract(self):
        sup_template = (PROJECT_ROOT / "templates" / "sup_parameters.html").read_text(encoding="utf-8")
        sup_script = (PROJECT_ROOT / "static" / "js" / "oplot_sup_admin.js").read_text(encoding="utf-8")
        assignment_script = (
            PROJECT_ROOT / "static" / "js" / "release_assignment_center.js"
        ).read_text(encoding="utf-8")
        self.assertIn('id="gigachatEnabled"', sup_template)
        self.assertIn("config.integrations.gigachat.enabled", sup_script)
        self.assertIn("GIGACHAT_ENABLED", assignment_script)
        self.assertIn("Ручное назначение остаётся доступно", assignment_script)

    def test_no_eager_sdk_import_or_startup_certificate_creation(self):
        service = (PROJECT_ROOT / "services" / "gigachat_service.py").read_text(encoding="utf-8")
        config = (PROJECT_ROOT / "config.py").read_text(encoding="utf-8")
        prefix = service.split("def _build_client", 1)[0]
        self.assertNotIn("from gigachat import", prefix)
        self.assertNotIn("CERT_PATH.mkdir", config)

    def test_assignment_recommendations_are_controlled_when_disabled(self):
        from services import release_monitor_service
        from services.gigachat_service import GIGA_HELPER

        with (
            mock.patch.object(
                release_monitor_service,
                "get_release_monitor_snapshot",
                return_value={"items": []},
            ),
            mock.patch.object(
                release_monitor_service,
                "get_release_monitor_week_control",
                return_value={
                    "availability_authoritative": True,
                    "missing_responsible": [],
                    "statistics": {},
                    "assigned_load": {},
                    "period": {},
                },
            ),
            mock.patch.object(GIGA_HELPER, "is_enabled", return_value=False),
            mock.patch.object(
                GIGA_HELPER,
                "chat",
                side_effect=AssertionError("disabled recommendations must not call GigaChat"),
            ),
        ):
            result = release_monitor_service.get_release_monitor_week_responsible_recommendations()
        self.assertEqual("disabled", result["source"])
        self.assertEqual([], result["recommendations"])
        self.assertIn("Ручное назначение", result["message"])


if __name__ == "__main__":
    unittest.main()
