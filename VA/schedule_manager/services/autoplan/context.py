from dataclasses import dataclass
from typing import Dict, Set

from VA.schedule_manager.models.employee import Employee
from VA.schedule_manager.models.schedule_grid import ScheduleGrid


@dataclass
class PlanningContext:
    grid: ScheduleGrid
    employees: Dict[str, Employee]
    assignments: Dict[str, dict]
    historical_load: Dict[str, int]
    holiday_work_history: Dict[str, int]
    newcomer_shift_history: Dict[str, Set[str]]
    counters: Dict[str, int]
    holiday_work_counters: Dict[str, int]
    current_month_holiday_work_counts: Dict[str, int]
    current_month_duty_blocks: Dict[str, int]
    monthly_shift_usage: Dict[str, Set[str]]

    @classmethod
    def build(
        cls,
        grid: ScheduleGrid,
        employees: Dict[str, Employee],
        historical_load: Dict[str, int],
        holiday_work_history: Dict[str, int],
        newcomer_shift_history: Dict[str, Set[str]],
    ) -> "PlanningContext":
        return cls(
            grid=grid,
            employees=employees,
            assignments={row.employee_name: dict(row.assignments) for row in grid.employees},
            historical_load=historical_load,
            holiday_work_history=holiday_work_history,
            newcomer_shift_history=newcomer_shift_history,
            counters={
                row.employee_name: historical_load.get(row.employee_name, 0)
                for row in grid.employees
            },
            holiday_work_counters={
                row.employee_name: holiday_work_history.get(row.employee_name, 0)
                for row in grid.employees
            },
            current_month_holiday_work_counts={
                row.employee_name: 0
                for row in grid.employees
            },
            current_month_duty_blocks={
                row.employee_name: 0
                for row in grid.employees
            },
            monthly_shift_usage={
                row.employee_name: set()
                for row in grid.employees
            },
        )
