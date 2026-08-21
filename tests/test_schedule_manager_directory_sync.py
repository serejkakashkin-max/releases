from __future__ import annotations

import json
import unittest
from datetime import date
from unittest import mock

from services.employee_directory_service import EmployeeDirectoryRuntimeContext
from VA.schedule_manager.integrations.release_monitor_duty_provider import (
    ReleaseMonitorDutyProvider,
)
from VA.schedule_manager.integrations.employee_directory_adapter import (
    get_managed_va_employees,
    project_schedule_snapshot_to_current_directory,
)
from VA.schedule_manager.models.employee import Employee
from VA.schedule_manager.models.schedule_grid import ScheduleDay, ScheduleGrid, ScheduleRow
from VA.schedule_manager.models.schedule_snapshot import ScheduleSnapshot, grid_to_dict
from VA.schedule_manager.repositories.schedule_repository import ScheduleRepository
from VA.schedule_manager.services.schedule_display_service import ScheduleDisplayService


def _directory_context(*, revision: int, release_name: str) -> EmployeeDirectoryRuntimeContext:
    payload = {
        "employees": [
            {
                "employee_id": "employee-roman",
                "enabled": True,
                "full_name": "Квашин Роман Вячеславович",
                "release_name": release_name,
                "aliases": [
                    {"type": "release", "value": "Кашин Р. В.", "jira_domain": ""},
                    {"type": "full", "value": "Кашин Роман Вячеславович", "jira_domain": ""},
                ],
                "source_refs": [],
                "memberships": {
                    "release_monitor": {"enabled": True, "order": 1},
                    "va_schedule_manager": {"enabled": True, "order": 1},
                },
            }
        ]
    }
    return EmployeeDirectoryRuntimeContext(
        status="available",
        revision=revision,
        etag=f"directory-{revision}",
        _payload_json=json.dumps(payload, ensure_ascii=False),
    )


def _ordered_directory_context() -> EmployeeDirectoryRuntimeContext:
    employees = []
    for index, (employee_id, full_name, release_name, aliases) in enumerate(
        (
            ("employee-before", "Сотрудник Первый", "Первый П. П.", []),
            (
                "employee-roman",
                "Квашин Роман Вячеславович",
                "Квашин Р. В.",
                [
                    {"type": "release", "value": "Кашин Р. В.", "jira_domain": ""},
                    {"type": "full", "value": "Кашин Роман Вячеславович", "jira_domain": ""},
                ],
            ),
            ("employee-after", "Сотрудник Последний", "Последний П. П.", []),
        ),
        start=1,
    ):
        employees.append(
            {
                "employee_id": employee_id,
                "enabled": True,
                "full_name": full_name,
                "release_name": release_name,
                "aliases": aliases,
                "source_refs": [],
                "memberships": {
                    "release_monitor": {"enabled": True, "order": index},
                    "va_schedule_manager": {"enabled": True, "order": index},
                },
            }
        )
    return EmployeeDirectoryRuntimeContext(
        status="available",
        revision=2,
        etag="directory-2",
        _payload_json=json.dumps({"employees": employees}, ensure_ascii=False),
    )


class ScheduleManagerDirectorySyncTests(unittest.TestCase):
    def test_admin_schedule_projects_current_name_without_changing_assignments(self):
        snapshot = ScheduleSnapshot.from_dict(
            {
                "employees": [{"name": "Кашин Р. В."}],
                "original_filename": "schedule.xlsx",
                "stored_filename": "stored.xlsx",
                "uploaded_at": "2026-08-01 10:00:00",
                "month_schedules": [
                    {
                        "year": 2026,
                        "month": 8,
                        "month_name": "Август",
                        "sheet_name": "Август 2026",
                        "label": "Август 2026",
                        "autoplan": {
                            "source": "autoplanner",
                            "assignment_explanations": [
                                {"employee_name": "Кашин Р. В.", "days": [1]}
                            ],
                            "capacity_diagnostics": {
                                "warnings": ["Диагностика вместимости."],
                                "overloaded_assignments": [
                                    {
                                        "employee_name": "Кашин Р. В.",
                                        "shift_code": "ВД",
                                        "other_candidates": [{"name": "Кашин Р. В."}],
                                    }
                                ],
                            },
                        },
                        "grid": {
                            "title": "Август 2026",
                            "year": 2026,
                            "month": 8,
                            "days": [
                                {"day": 1, "weekday": "сб", "date": "2026-08-01"}
                            ],
                            "employees": [
                                {
                                    "employee_name": "Кашин Р. В.",
                                    "hours": 8,
                                    "assignments": {"1": "ВД"},
                                }
                            ],
                        },
                    }
                ],
            }
        )

        projected = project_schedule_snapshot_to_current_directory(
            snapshot,
            _directory_context(revision=2, release_name="Квашин Р. В."),
        )

        row = projected.get_month_grid("Август 2026").employees[0]
        self.assertEqual("Квашин Р. В.", row.employee_name)
        self.assertEqual(8, row.hours)
        self.assertEqual({1: "ВД"}, row.assignments)
        self.assertEqual("Квашин Р. В.", projected.employees[0].name)
        self.assertEqual(
            "Квашин Р. В.",
            projected.get_month_metadata("Август 2026", "autoplan")[
                "assignment_explanations"
            ][0]["employee_name"],
        )
        diagnostics = projected.get_month_metadata("Август 2026", "autoplan")[
            "capacity_diagnostics"
        ]
        self.assertEqual(
            "Квашин Р. В.", diagnostics["overloaded_assignments"][0]["employee_name"]
        )
        self.assertEqual(
            "Квашин Р. В.", diagnostics["overloaded_assignments"][0]["other_candidates"][0]["name"]
        )
        self.assertEqual(
            "Кашин Р. В.", snapshot.get_month_grid("Август 2026").employees[0].employee_name
        )

    def test_managed_employee_uses_current_directory_name_not_historical_alias(self):
        settings = mock.Mock(
            status="available",
            payload={
                "migration": {
                    "status": "complete",
                    "unresolved": 0,
                    "ambiguous": 0,
                    "conflicts": 0,
                },
                "employees": {},
            },
        )
        employees = get_managed_va_employees(
            _directory_context(revision=2, release_name="Квашин Р. В."),
            settings,
        )

        self.assertEqual("Квашин Р. В.", employees[0].name)

    def test_schedule_repository_projects_name_for_admin_editing_contract(self):
        raw = {
            "employees": [{"name": "Кашин Р. В."}],
            "original_filename": "schedule.xlsx",
            "stored_filename": "stored.xlsx",
            "uploaded_at": "2026-08-01 10:00:00",
            "month_schedules": [
                {
                    "year": 2026,
                    "month": 8,
                    "month_name": "Август",
                    "sheet_name": "Август 2026",
                    "label": "Август 2026",
                    "grid": {
                        "title": "Август 2026",
                        "year": 2026,
                        "month": 8,
                        "days": [
                            {"day": 1, "weekday": "сб", "date": "2026-08-01"}
                        ],
                        "employees": [
                            {
                                "employee_name": "Кашин Р. В.",
                                "hours": 8,
                                "assignments": {"1": "ВД"},
                            }
                        ],
                    },
                }
            ],
        }
        repository = ScheduleRepository.__new__(ScheduleRepository)
        repository.store = mock.Mock(load=mock.Mock(return_value=raw))
        with mock.patch(
            "VA.schedule_manager.integrations.employee_directory_adapter.load_employee_directory_context",
            return_value=_directory_context(revision=2, release_name="Квашин Р. В."),
        ):
            snapshot = repository.load()

        self.assertEqual(
            "Квашин Р. В.",
            snapshot.get_month_grid("Август 2026").employees[0].employee_name,
        )

    def test_admin_display_keeps_directory_order_after_rename(self):
        grid = ScheduleGrid(
            title="Август 2026",
            year=2026,
            month=8,
            days=[ScheduleDay(day=1, weekday="сб", date=date(2026, 8, 1))],
            employees=[
                ScheduleRow("Последний П. П.", 0, {1: ""}),
                ScheduleRow("Кашин Р. В.", 8, {1: "ВД"}),
                ScheduleRow("Первый П. П.", 0, {1: ""}),
            ],
        )
        snapshot = ScheduleSnapshot(
            employees=[],
            original_filename="schedule.xlsx",
            stored_filename="stored.xlsx",
            uploaded_at="2026-08-01 10:00:00",
            month_schedules=[
                {
                    "year": 2026,
                    "month": 8,
                    "month_name": "Август",
                    "sheet_name": "Август 2026",
                    "label": "Август 2026",
                    "grid": grid_to_dict(grid),
                }
            ],
        )
        context = _ordered_directory_context()
        projected = project_schedule_snapshot_to_current_directory(snapshot, context)
        service = ScheduleDisplayService.__new__(ScheduleDisplayService)
        service.employee_service = mock.Mock()
        service.employee_service.list_employees.return_value = [
            Employee(name="Первый П. П.", status="active"),
            Employee(name="Квашин Р. В.", status="active"),
            Employee(name="Последний П. П.", status="active"),
        ]

        unavailable = EmployeeDirectoryRuntimeContext(
            status="missing",
            revision=None,
            etag="",
        )
        with mock.patch(
            "services.employee_directory_service.load_employee_directory_context",
            return_value=unavailable,
        ):
            ordered = service._sort_by_directory_order(
                projected.get_month_grid("Август 2026")
            )

        self.assertEqual(
            ["Первый П. П.", "Квашин Р. В.", "Последний П. П."],
            [row.employee_name for row in ordered.employees],
        )
        self.assertEqual({1: "ВД"}, ordered.employees[1].assignments)

    def test_admin_display_orders_historical_spacing_variants_by_directory(self):
        grid = ScheduleGrid(
            title="Август 2026",
            year=2026,
            month=8,
            days=[ScheduleDay(day=1, weekday="сб", date=date(2026, 8, 1))],
            employees=[
                ScheduleRow("Кашкин С.Н.", 0, {1: ""}),
                ScheduleRow("Тутов А. М.", 0, {1: "ДД"}),
                ScheduleRow("Ефимов В. В.", 0, {1: "ДР"}),
                ScheduleRow("Мухиддинов М.", 0, {1: "ВД"}),
                ScheduleRow("Айрапетова Н. Г.", 0, {1: "ВР"}),
                ScheduleRow("Андреев В. Ю.", 0, {1: ""}),
            ],
        )
        service = ScheduleDisplayService.__new__(ScheduleDisplayService)
        service.employee_service = mock.Mock()
        service.employee_service.list_employees.return_value = [
            Employee(name="Васькин Антон Анатольевич", status="active"),
            Employee(name="Гапоненко Д.А.", status="active"),
            Employee(name="Кондратьева А.А.", status="active"),
            Employee(name="Фисан К.Ю.", status="active"),
            Employee(name="Тутов А.М.", status="active"),
            Employee(name="Ефимов В.В.", status="active"),
            Employee(name="Частухин А.М.", status="active"),
            Employee(name="Мухиддинов М.Б.", status="active"),
            Employee(name="Кашкин С.Н.", status="active"),
            Employee(name="Айрапетова Н.Г.", status="active"),
            Employee(name="Андреев Василий Юрьевич", status="active"),
        ]

        unavailable = EmployeeDirectoryRuntimeContext(
            status="missing",
            revision=None,
            etag="",
        )
        with mock.patch(
            "services.employee_directory_service.load_employee_directory_context",
            return_value=unavailable,
        ):
            ordered = service._sort_by_directory_order(grid)

        self.assertEqual(
            [
                "Тутов А. М.",
                "Ефимов В. В.",
                "Мухиддинов М.",
                "Кашкин С.Н.",
                "Айрапетова Н. Г.",
                "Андреев В. Ю.",
            ],
            [row.employee_name for row in ordered.employees],
        )
        self.assertEqual({1: "ДД"}, ordered.employees[0].assignments)

    def test_projection_is_not_cancelled_when_another_month_already_has_current_name(self):
        old_grid = ScheduleGrid(
            title="Август 2026",
            year=2026,
            month=8,
            days=[ScheduleDay(day=1, weekday="сб", date=date(2026, 8, 1))],
            employees=[ScheduleRow("Кашин Р. В.", 8, {1: "ВД"})],
        )
        current_grid = ScheduleGrid(
            title="Сентябрь 2026",
            year=2026,
            month=9,
            days=[ScheduleDay(day=1, weekday="вт", date=date(2026, 9, 1))],
            employees=[
                ScheduleRow("Кашин Р. В.", 8, {1: "8"}),
                ScheduleRow("Квашин Р. В.", 0, {1: ""}),
            ],
        )
        snapshot = ScheduleSnapshot(
            employees=[],
            original_filename="schedule.xlsx",
            stored_filename="stored.xlsx",
            uploaded_at="2026-08-01 10:00:00",
            month_schedules=[
                {
                    "year": 2026,
                    "month": 8,
                    "month_name": "Август",
                    "sheet_name": "Август 2026",
                    "label": "Август 2026",
                    "grid": grid_to_dict(old_grid),
                },
                {
                    "year": 2026,
                    "month": 9,
                    "month_name": "Сентябрь",
                    "sheet_name": "Сентябрь 2026",
                    "label": "Сентябрь 2026",
                    "grid": grid_to_dict(current_grid),
                },
            ],
        )

        projected = project_schedule_snapshot_to_current_directory(
            snapshot,
            _directory_context(revision=3, release_name="Квашин Р. В."),
        )

        self.assertEqual(
            "Квашин Р. В.",
            projected.get_month_grid("Август 2026").employees[0].employee_name,
        )
        self.assertEqual(
            ["Кашин Р. В.", "Квашин Р. В."],
            [
                row.employee_name
                for row in projected.get_month_grid("Сентябрь 2026").employees
            ],
        )
        self.assertEqual(
            {1: "ВД"},
            projected.get_month_grid("Август 2026").employees[0].assignments,
        )

    def test_read_only_schedule_projects_historical_row_with_current_directory_name(self):
        provider = ReleaseMonitorDutyProvider()
        months = [
            {
                "year": 2026,
                "month": 8,
                "label": "Август 2026",
                "days": [],
                "employees": [
                    {
                        "employee_name": "Кашин Р. В.",
                        "hours": 0,
                        "assignments": {},
                        "ambiguous": False,
                    }
                ],
                "autoplan_artifact": {},
            }
        ]
        with mock.patch(
            "services.employee_directory_service.load_employee_directory_context",
            return_value=_directory_context(revision=2, release_name="Квашин Р. В."),
        ):
            _projection, month_grids = provider._project(
                months,
                [],
                {},
                "schedule-revision",
                True,
            )

        self.assertEqual(
            "Квашин Р. В.",
            month_grids["2026-08"]["employees"][0]["employee_name"],
        )
        self.assertEqual("Кашин Р. В.", months[0]["employees"][0]["employee_name"])

    def test_read_only_schedule_keeps_directory_order_for_spacing_variants(self):
        provider = ReleaseMonitorDutyProvider()
        months = [
            {
                "year": 2026,
                "month": 8,
                "label": "Август 2026",
                "days": [],
                "employees": [
                    {
                        "employee_name": "Кашкин С.Н.",
                        "hours": 0,
                        "assignments": {},
                        "ambiguous": False,
                    },
                    {
                        "employee_name": "Тутов А. М.",
                        "hours": 0,
                        "assignments": {},
                        "ambiguous": False,
                    },
                    {
                        "employee_name": "Ефимов В. В.",
                        "hours": 0,
                        "assignments": {},
                        "ambiguous": False,
                    },
                ],
                "autoplan_artifact": {},
            }
        ]
        unavailable = EmployeeDirectoryRuntimeContext(
            status="missing",
            revision=None,
            etag="",
        )
        with mock.patch("services.employee_directory_service.load_employee_directory_context", return_value=unavailable), mock.patch(
            "VA.schedule_manager.services.employee_identity.build_directory_order_index",
            return_value={
                "тутовам": 0,
                "ефимоввв": 1,
                "кашкинсн": 2,
                "__surname__": {"тутов": 0, "ефимов": 1, "кашкин": 2},
            },
        ):
            _projection, month_grids = provider._project(
                months,
                [],
                {},
                "schedule-revision",
                True,
            )

        self.assertEqual(
            ["Тутов А. М.", "Ефимов В. В.", "Кашкин С.Н."],
            [
                row["employee_name"]
                for row in month_grids["2026-08"]["employees"]
            ],
        )

    def test_employee_directory_revision_is_part_of_provider_cache_signature(self):
        provider = ReleaseMonitorDutyProvider()
        provider._cache_signature = (None, None, ("available", 1, "directory-1"))
        provider._cache = {"status": {"status": "ready"}}
        replacement = {
            "status": {"status": "ready"},
            "projection": {},
            "months": [],
            "month_grids": {},
            "effective_shifts": [],
        }
        changed_signature = (None, None, ("available", 2, "directory-2"))
        with (
            mock.patch.object(
                provider,
                "_source_signatures",
                return_value=changed_signature,
            ),
            mock.patch.object(provider, "_stable_read", return_value=replacement) as reload_state,
        ):
            provider._load()

        reload_state.assert_called_once_with()
        self.assertEqual(changed_signature, provider._cache_signature)
        self.assertEqual(
            "directory-2",
            provider._signature_payload(changed_signature)["employee_directory"]["etag"],
        )


if __name__ == "__main__":
    unittest.main()
