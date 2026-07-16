from collections import Counter
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from VA.schedule_manager.models.schedule_grid import ScheduleGrid, ScheduleRow


DUTY_SHIFT_CODES = ("ХД", "ХР", "ДД", "ДР", "ВД", "ВР")
MOSCOW_DUTY_SHIFT_CODES = ("ДД", "ДР", "ВД", "ВР")
KHABAROVSK_SHIFT_CODES = ("ХД", "ХР")
NON_DUTY_CODES = {"", "8", "Вых", "ВХ", "Праздник", "отпуск"}


@dataclass(frozen=True)
class EmployeeWorkload:
    employee_name: str
    total_duty_assignments: int
    shift_counts: Dict[str, int]
    last_duty_shift: Optional[str]
    hours: Optional[int]


@dataclass(frozen=True)
class ScheduleAnalysis:
    title: str
    employee_count: int
    workloads: List[EmployeeWorkload]
    last_week_block: Dict[str, str] = field(default_factory=dict)


def analyze_schedule(grid: ScheduleGrid) -> ScheduleAnalysis:
    workloads = [_analyze_employee(row) for row in grid.employees]
    return ScheduleAnalysis(
        title=grid.title,
        employee_count=len(grid.employees),
        workloads=workloads,
        last_week_block=_last_week_block(grid),
    )


def _analyze_employee(row: ScheduleRow) -> EmployeeWorkload:
    counts = Counter()
    last_duty_shift: Optional[str] = None

    for day in sorted(row.assignments):
        code = row.assignments[day]
        if code in DUTY_SHIFT_CODES:
            counts[code] += 1
            last_duty_shift = code

    shift_counts = {code: counts.get(code, 0) for code in DUTY_SHIFT_CODES}
    return EmployeeWorkload(
        employee_name=row.employee_name,
        total_duty_assignments=sum(shift_counts.values()),
        shift_counts=shift_counts,
        last_duty_shift=last_duty_shift,
        hours=row.hours,
    )


def _last_week_block(grid: ScheduleGrid) -> Dict[str, str]:
    if not grid.days:
        return {}

    last_day = grid.days[-1]
    last_week_days = {
        schedule_day.day
        for schedule_day in grid.days
        if schedule_day.date.isocalendar().week == last_day.date.isocalendar().week
    }
    block: Dict[str, str] = {}

    for row in grid.employees:
        for day in sorted(last_week_days):
            code = row.assignments.get(day, "")
            if code in DUTY_SHIFT_CODES:
                block[code] = row.employee_name

    return block

