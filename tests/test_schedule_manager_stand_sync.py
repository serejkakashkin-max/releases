from __future__ import annotations

import runpy
import sys
import types
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from tests._support import PROJECT_ROOT
from VA.schedule_manager.models.employee import Employee
from VA.schedule_manager.models.integration_settings import CalendarIntegrationSettings
from VA.schedule_manager.models.schedule_grid import ScheduleDay, ScheduleGrid, ScheduleRow
from VA.schedule_manager.models.schedule_snapshot import ScheduleSnapshot, grid_to_dict
from VA.schedule_manager.services import calendar_integration_service as calendar_module
from VA.schedule_manager.services.calendar_integration_service import CalendarIntegrationService
from VA.schedule_manager.services.schedule_display_service import ScheduleDisplayService
from VA.schedule_manager.services.schedule_month_service import ScheduleMonthService


class _SettingsRepository:
    def __init__(self, settings: CalendarIntegrationSettings) -> None:
        self.settings = settings
        self.saved = None

    def load_calendar(self) -> CalendarIntegrationSettings:
        return self.settings

    def save_calendar(self, settings: CalendarIntegrationSettings) -> None:
        self.saved = settings


class _EmployeeService:
    def __init__(self, employees) -> None:
        self._employees = employees

    def list_employees(self):
        return list(self._employees)


def _grid(names: list[str], *, year: int = 2026, month: int = 8) -> ScheduleGrid:
    day = ScheduleDay(1, "сб", date(year, month, 1))
    return ScheduleGrid(
        title=f"{month}.{year}",
        year=year,
        month=month,
        days=[day],
        employees=[ScheduleRow(name, 0, {1: ""}) for name in names],
    )


class CalendarIntegrationStandSyncTests(unittest.TestCase):
    def test_local_holidays_provider_has_no_network_dependency(self):
        repository = _SettingsRepository(CalendarIntegrationSettings())
        service = CalendarIntegrationService(repository)
        fake_holidays = types.SimpleNamespace(
            Russia=lambda years: {
                date(int(years), 1, 1): "Новый год",
                date(int(years), 5, 9): "День Победы",
            }
        )

        with patch.object(calendar_module, "HOLIDAYS_AVAILABLE", True), patch.object(
            calendar_module, "holidays", fake_holidays, create=True
        ):
            state = service.load_calendar(2027, 5)

        self.assertEqual(state.source, "holidays library")
        self.assertEqual(state.warning, "")
        self.assertIn(date(2027, 5, 9), state.holidays)
        source = (PROJECT_ROOT / "VA/schedule_manager/services/calendar_integration_service.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("urlopen", source)
        self.assertNotIn("http://", source)
        self.assertNotIn("https://", source)

    def test_disabled_and_missing_library_states_are_controlled(self):
        disabled = CalendarIntegrationService(
            _SettingsRepository(CalendarIntegrationSettings(enabled=False))
        ).load_calendar(2027, 1)
        self.assertEqual(disabled.source, "Не настроен")
        self.assertTrue(disabled.warning)

        service = CalendarIntegrationService(_SettingsRepository(CalendarIntegrationSettings()))
        with patch.object(calendar_module, "HOLIDAYS_AVAILABLE", False):
            missing = service.load_calendar(2027, 1)
        self.assertEqual(missing.holidays, set())
        self.assertIn("не установлена", missing.warning)

    def test_settings_are_bounded_and_unexpected_fields_are_ignored(self):
        repository = _SettingsRepository(CalendarIntegrationSettings())
        service = CalendarIntegrationService(repository)
        saved = service.save_settings(
            {
                "enabled": "on",
                "provider": "custom",
                "api_url": "https://unexpected.invalid/",
                "api_token": "must-not-be-saved",
                "timeout_seconds": "999",
                "unexpected": "value",
            }
        )
        self.assertIs(saved, repository.saved)
        self.assertEqual(saved.provider, "holidays")
        self.assertEqual(saved.api_url, "")
        self.assertEqual(saved.api_token, "")
        self.assertEqual(saved.timeout_seconds, 30)
        self.assertNotIn("unexpected", saved.to_dict())


class ScheduleOrderingStandSyncTests(unittest.TestCase):
    def test_display_sort_uses_active_directory_order_without_mutating_grid(self):
        original = _grid(["Неизвестный 1", "Сотрудник Б", "Сотрудник А", "Неизвестный 2"])
        service = ScheduleDisplayService.__new__(ScheduleDisplayService)
        service.employee_service = _EmployeeService(
            [
                Employee("Сотрудник А", status="active"),
                Employee("Уволенный", status="dismissed"),
                Employee("Сотрудник Б", status="active"),
            ]
        )

        sorted_grid = service._sort_by_directory_order(original)

        self.assertEqual(
            [row.employee_name for row in sorted_grid.employees],
            ["Сотрудник А", "Сотрудник Б", "Неизвестный 1", "Неизвестный 2"],
        )
        self.assertEqual(
            [row.employee_name for row in original.employees],
            ["Неизвестный 1", "Сотрудник Б", "Сотрудник А", "Неизвестный 2"],
        )

    def test_new_and_copied_months_keep_active_directory_members(self):
        service = ScheduleMonthService.__new__(ScheduleMonthService)
        service.employee_service = _EmployeeService(
            [
                Employee("Новый", status="active"),
                Employee("Сотрудник Б", status="active"),
                Employee("Сотрудник А", status="active"),
                Employee("Уволенный", status="dismissed"),
            ]
        )
        service.shift_service = types.SimpleNamespace(lookup=lambda: {})
        previous = _grid(["Сотрудник А", "Сотрудник Б"], year=2026, month=7)
        snapshot = ScheduleSnapshot(
            employees=[],
            original_filename="fixture",
            stored_filename="",
            uploaded_at="",
            month_schedules=[
                {
                    "year": 2026,
                    "month": 7,
                    "month_name": "Июль",
                    "sheet_name": "Июль 2026",
                    "label": "Июль 2026",
                    "grid": grid_to_dict(previous),
                }
            ],
        )

        ordered = service._employees_for_new_month(snapshot, "last_schedule")
        self.assertEqual(ordered, ["Сотрудник А", "Сотрудник Б", "Новый"])
        self.assertEqual(
            service._employees_for_new_month(snapshot, "directory"),
            ["Новый", "Сотрудник Б", "Сотрудник А"],
        )

        copied = service._copy_grid_to_month(previous, 2026, 8, set(), ordered)
        self.assertEqual(
            [row.employee_name for row in copied.employees],
            ["Сотрудник А", "Сотрудник Б", "Новый"],
        )

    def test_latest_schedule_is_selected_by_period_not_list_position(self):
        service = ScheduleMonthService.__new__(ScheduleMonthService)
        older = _grid(["Старый"], year=2025, month=12)
        latest = _grid(["Новый"], year=2026, month=8)
        snapshot = ScheduleSnapshot(
            employees=[],
            original_filename="fixture",
            stored_filename="",
            uploaded_at="",
            month_schedules=[
                {"year": 2025, "month": 12, "month_name": "Декабрь", "sheet_name": "old", "label": "old", "grid": grid_to_dict(older)},
                {"year": 2026, "month": 8, "month_name": "Август", "sheet_name": "new", "label": "new", "grid": grid_to_dict(latest)},
            ],
        )
        self.assertEqual(service._get_employees_from_last_schedule(snapshot), ["Новый"])


class ScheduleManagerPackagingAndStartupTests(unittest.TestCase):
    def test_requirements_are_utf8_without_nulls_or_conflicting_duplicates(self):
        raw = (PROJECT_ROOT / "requirements.txt").read_bytes()
        self.assertNotIn(b"\x00", raw)
        text = raw.decode("utf-8")
        lines = [line.strip() for line in text.splitlines() if line.strip() and not line.startswith("#")]
        names = [line.split("==", 1)[0].split(">=", 1)[0].casefold() for line in lines]
        self.assertEqual(len(names), len(set(names)))
        self.assertIn("holidays>=0.90.0", lines)
        self.assertIn("packaging>=24.0", lines)
        self.assertFalse(any("file:" in line or "\\" in line for line in lines))
        self.assertFalse(any("token" in line.casefold() or "password" in line.casefold() for line in lines))

    def test_version_checker_degrades_without_holidays_and_has_no_network_or_install_action(self):
        packaging = types.ModuleType("packaging")
        packaging_version = types.ModuleType("packaging.version")

        class Version:
            def __init__(self, value):
                self.parts = tuple(int(part) for part in str(value).split("."))

            def __lt__(self, other):
                return self.parts < other.parts

            def __ge__(self, other):
                return self.parts >= other.parts

        packaging_version.Version = Version
        source_path = PROJECT_ROOT / "VA/schedule_manager/utils/version_checker.py"
        with patch.dict(
            sys.modules,
            {"packaging": packaging, "packaging.version": packaging_version, "holidays": None},
        ):
            namespace = runpy.run_path(str(source_path))
            result = namespace["check_holidays_version"]()

        self.assertFalse(result["installed"])
        source = source_path.read_text(encoding="utf-8")
        self.assertNotIn("requests", source)
        self.assertNotIn("urlopen", source)
        self.assertNotIn("subprocess", source)
        self.assertNotIn("pip.main", source)

    def test_startup_hook_is_guarded_and_integration_template_contract_is_stable(self):
        app_source = (PROJECT_ROOT / "app.py").read_text(encoding="utf-8")
        self.assertIn("if not os.environ.get('WERKZEUG_RUN_MAIN')", app_source)
        self.assertIn("log_holidays_status()", app_source)
        self.assertIn("except Exception", app_source)

        template = (
            PROJECT_ROOT
            / "VA/schedule_manager/templates/va_schedule_manager/settings/integrations.html"
        ).read_text(encoding="utf-8")
        self.assertIn('extends "va_schedule_manager/base.html"', template)
        self.assertIn("method=\"post\"", template)
        self.assertIn("va_schedule_manager.settings.save_calendar_integration", template)
        self.assertIn("name=\"provider\" value=\"holidays\"", template)


if __name__ == "__main__":
    unittest.main()
