from datetime import date, timedelta

from VA.schedule_manager.models.employee import Employee
from VA.schedule_manager.models.schedule_grid import ScheduleDay, ScheduleGrid, ScheduleRow
from VA.schedule_manager.repositories.schedule_repository import ScheduleRepository
from VA.schedule_manager.services.autoplan.capacity import DutyCapacityDiagnostics
from VA.schedule_manager.services.schedule_autoplan_service import ScheduleAutoplanService


class _EmployeeRepository:
    def __init__(self, employees):
        self._employees = employees

    def load_all(self):
        return list(self._employees)


def test_capacity_diagnostics_is_empty_when_assignments_fit_limit():
    diagnostics = DutyCapacityDiagnostics(2)
    diagnostics.start_week("1-5 числа")
    diagnostics.note_shift_candidates("ДД", ["А", "Б"])
    diagnostics.note_planned_assignment("А", "ДД", ["А", "Б"], 1, 4)
    diagnostics.finish_week()

    assert diagnostics.build() == {}


def test_capacity_diagnostics_records_third_block_before_increment():
    diagnostics = DutyCapacityDiagnostics(2)
    diagnostics.start_week("8-12 числа")
    diagnostics.note_shift_candidates("ДД", ["А", "Б"])
    diagnostics.note_planned_assignment("А", "ДД", ["А", "Б"], 2, 7)
    diagnostics.finish_week()

    artifact = diagnostics.build()
    assert artifact["overloaded_assignments"][0]["blocks_before"] == 2
    assert artifact["overloaded_assignments"][0]["candidate_count"] == 2
    assert "дежурных блоков 1" in artifact["warnings"][0]


def test_existing_blocks_count_but_do_not_create_overload():
    diagnostics = DutyCapacityDiagnostics(2)
    diagnostics.start_week("15-19 числа")
    diagnostics.note_existing_block("А", "ДД")
    diagnostics.note_existing_block("Б", "ВД")
    diagnostics.note_shift_candidates("ДР", ["А"])
    diagnostics.note_planned_assignment("А", "ДР", ["А"], 2, 3)
    diagnostics.finish_week()

    artifact = diagnostics.build()
    assert artifact["warnings"] == [
        "На неделе 15-19 числа доступных дежурных 2, дежурных блоков 3 — "
        "ровное распределение (не более 2 блоков на человека) недостижимо."
    ]
    assert len(artifact["overloaded_assignments"]) == 1


def test_full_manual_week_is_counted_by_month_planner_capacity_diagnostics(tmp_path, monkeypatch):
    employees = [
        Employee("А", status="active", location="moscow", competencies=("support",)),
        Employee("Б", status="active", location="moscow", competencies=("support",)),
    ]
    service = ScheduleAutoplanService(
        ScheduleRepository(tmp_path / "schedule.json"),
        _EmployeeRepository(employees),
    )
    days = [
        ScheduleDay(day, (date(2026, 8, 3) + timedelta(days=day - 1)).strftime("%a"), date(2026, 8, 3) + timedelta(days=day - 1))
        for day in range(1, 20)
        if (date(2026, 8, 3) + timedelta(days=day - 1)).weekday() < 5
    ]
    assignments_a = {day.day: "ДД" if day.day <= 5 else "ДР" if day.day <= 12 else "" for day in days}
    assignments_b = {day.day: "" if day.day <= 12 else "ДД" for day in days}
    grid = ScheduleGrid(
        "Август 2026",
        2026,
        8,
        days,
        [ScheduleRow("А", 0, assignments_a), ScheduleRow("Б", 0, assignments_b)],
    )
    monkeypatch.setattr(service, "_moscow_shift_order", lambda previous_week_evening: ("ВД",))

    diagnostics = DutyCapacityDiagnostics(2)
    service._plan_grid(grid, {employee.name: employee for employee in employees}, duty_capacity=diagnostics)
    artifact = diagnostics.build()

    warning = next(warning for warning in artifact["warnings"] if "15-19 числа" in warning)
    assert "доступных дежурных 2, дежурных блоков 2" in warning
