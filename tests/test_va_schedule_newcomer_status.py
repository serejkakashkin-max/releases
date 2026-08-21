from datetime import date

from VA.schedule_manager.models.employee import Employee
from VA.schedule_manager.models.schedule_grid import ScheduleDay, ScheduleGrid, ScheduleRow
from VA.schedule_manager.models.schedule_snapshot import ScheduleSnapshot, grid_to_dict
from VA.schedule_manager.services.newcomer_status_service import build_newcomer_alerts


def _snapshot(months):
    month_schedules = []
    for year, month, assignments in months:
        title = f"{month:02d}.{year}"
        grid = ScheduleGrid(
            title,
            year,
            month,
            [ScheduleDay(1, "пн", date(year, month, 1))],
            [ScheduleRow(name, 0, {1: code}) for name, code in assignments.items()],
        )
        month_schedules.append(
            {"year": year, "month": month, "sheet_name": title, "grid": grid_to_dict(grid)}
        )
    return ScheduleSnapshot([], "", "", "", month_schedules)


def _employee(name="Иванов Иван", employee_id="ivan"):
    return Employee(name, employee_id=employee_id, competencies=("newcomer",))


def test_newcomer_alert_boundaries_and_unknown_history():
    snapshot = _snapshot([
        (2026, 1, {"Иванов Иван": "8", "Без смены": ""}),
    ])
    assert build_newcomer_alerts(snapshot, [_employee()], lambda value: value, 2026, 3)["items"] == []
    alert = build_newcomer_alerts(snapshot, [_employee()], lambda value: value, 2026, 4)
    assert alert["items"][0]["months_passed"] == 3

    unknown = build_newcomer_alerts(
        snapshot,
        [_employee(), _employee("Без смены", "unknown")],
        lambda value: value,
        2026,
        6,
    )
    assert [item["employee_id"] for item in unknown["items"]] == ["ivan"]


def test_four_months_and_non_newcomer_and_year_boundary():
    snapshot = _snapshot([
        (2025, 11, {"Иванов Иван": "8", "Опытный": "8"}),
    ])
    experienced = Employee("Опытный", employee_id="experienced", competencies=())
    alerts = build_newcomer_alerts(snapshot, [_employee(), experienced], lambda value: value, 2026, 2)
    assert alerts["items"] == [{
        "employee_id": "ivan",
        "employee_name": "Иванов Иван",
        "since_year": 2025,
        "since_month": 11,
        "months_passed": 3,
    }]
    four_month_alert = build_newcomer_alerts(snapshot, [_employee()], lambda value: value, 2026, 3)
    assert four_month_alert["items"][0]["months_passed"] == 4


def test_empty_or_missing_snapshot_is_unavailable():
    assert build_newcomer_alerts(None, [], lambda value: value, 2026, 2) == {"status": "unavailable", "items": []}
    empty = ScheduleSnapshot([], "", "", "", [])
    assert build_newcomer_alerts(empty, [], lambda value: value, 2026, 2) == {"status": "unavailable", "items": []}
