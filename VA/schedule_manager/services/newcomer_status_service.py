from __future__ import annotations

from typing import Callable, Dict, Iterable

from VA.schedule_manager.models.employee import Employee
from VA.schedule_manager.models.schedule_snapshot import ScheduleSnapshot
from VA.schedule_manager.services.autoplan.rules import EmployeeRuleSet
from VA.schedule_manager.services.employee_identity import employee_identity_matches


NEWCOMER_MAX_MONTHS = 3


def first_default_shift_months(
    snapshot: ScheduleSnapshot | None,
    normalize_code: Callable[[object], str],
) -> Dict[str, tuple[int, int]]:
    first_seen: Dict[str, tuple[int, int]] = {}
    if snapshot is None:
        return first_seen
    months = sorted(
        snapshot.month_schedules,
        key=lambda month: (int(month["year"]), int(month["month"])),
    )
    for month in months:
        try:
            grid = snapshot.get_month_grid(str(month["sheet_name"]))
        except KeyError:
            continue
        month_value = (int(month["year"]), int(month["month"]))
        for row in grid.employees:
            if row.employee_name in first_seen:
                continue
            if any(normalize_code(code) == "8" for code in row.assignments.values()):
                first_seen[row.employee_name] = month_value
    return first_seen


def build_newcomer_alerts(
    snapshot: ScheduleSnapshot | None,
    employees: Iterable[Employee],
    normalize_code: Callable[[object], str],
    now_year: int,
    now_month: int,
) -> dict:
    if snapshot is None or not snapshot.month_schedules:
        return {"status": "unavailable", "items": []}

    first_seen = first_default_shift_months(snapshot, normalize_code)
    items = []
    rules = EmployeeRuleSet()
    for employee in employees:
        if not rules.is_newcomer(employee):
            continue
        since = next(
            (month for name, month in first_seen.items() if employee_identity_matches(name, employee.name)),
            None,
        )
        if since is None:
            continue
        months_passed = (now_year * 12 + now_month) - (since[0] * 12 + since[1])
        if months_passed >= NEWCOMER_MAX_MONTHS:
            items.append(
                {
                    "employee_id": employee.employee_id,
                    "employee_name": employee.name,
                    "since_year": since[0],
                    "since_month": since[1],
                    "months_passed": months_passed,
                }
            )
    items.sort(key=lambda item: item["months_passed"], reverse=True)
    return {"status": "available", "items": items}
