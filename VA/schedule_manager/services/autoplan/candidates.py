from dataclasses import dataclass
from typing import Callable, Dict, List, Set

from VA.schedule_manager.models.employee import Employee
from VA.schedule_manager.models.schedule_grid import ScheduleGrid
from VA.schedule_manager.services.autoplan.scoring import CandidateScorer


@dataclass(frozen=True)
class WeekdayCandidateRequest:
    grid: ScheduleGrid
    employees: Dict[str, Employee]
    counters: Dict[str, int]
    unavailable: Set[str]
    week_assigned: Set[str]
    location: str
    allow_manager: bool
    shift_code: str
    monthly_shift_usage: Dict[str, Set[str]]
    previous_week_evening: Set[str]
    current_week_evening: Set[str]
    current_week_day_primary_mpr: Set[str]
    current_month_duty_blocks: Dict[str, int]
    newcomer_shift_history: Dict[str, Set[str]]
    previous_week_shift_workers: Dict[str, Set[str]]


@dataclass(frozen=True)
class HolidayCandidateRequest:
    grid: ScheduleGrid
    employees: Dict[str, Employee]
    holiday_work_counters: Dict[str, int]
    current_month_holiday_work_counts: Dict[str, int]
    unavailable: Set[str]
    newcomer_shift_history: Dict[str, Set[str]]


class CandidateGenerator:
    def __init__(
        self,
        scorer: CandidateScorer,
        is_manager: Callable[[Employee], bool],
        is_newcomer: Callable[[Employee], bool],
        is_mpr_coordinator: Callable[[Employee], bool],
        evening_priority: Callable[[Employee, str], int],
        holiday_work_manager_priority: Callable[[Employee], int],
        evening_shift_codes: Set[str],
        khabarovsk_shifts: Set[str],
        holiday_work_code: str,
    ) -> None:
        self.scorer = scorer
        self.is_manager = is_manager
        self.is_newcomer = is_newcomer
        self.is_mpr_coordinator = is_mpr_coordinator
        self.evening_priority = evening_priority
        self.holiday_work_manager_priority = holiday_work_manager_priority
        self.evening_shift_codes = evening_shift_codes
        self.khabarovsk_shifts = khabarovsk_shifts
        self.holiday_work_code = holiday_work_code

    def weekday_candidates(self, request: WeekdayCandidateRequest) -> List[str]:
        candidates = []
        for row in request.grid.employees:
            employee = request.employees.get(row.employee_name)
            if employee is None or employee.status != "active":
                continue
            if employee.location != request.location:
                continue
            if row.employee_name in request.unavailable or row.employee_name in request.week_assigned:
                continue
            if not request.allow_manager and self.is_manager(employee):
                continue
            if (
                self.is_newcomer(employee)
                and request.shift_code not in request.newcomer_shift_history.get(row.employee_name, set())
            ):
                continue
            candidates.append(row.employee_name)

        if request.shift_code:
            without_same_shift = [
                name
                for name in candidates
                if request.shift_code not in request.monthly_shift_usage.get(name, set())
            ]
            if without_same_shift:
                candidates = without_same_shift

        if request.shift_code in self.khabarovsk_shifts:
            previous_same_shift_workers = request.previous_week_shift_workers.get(request.shift_code, set())
            without_previous_same_shift = [
                name
                for name in candidates
                if name not in previous_same_shift_workers
            ]
            if without_previous_same_shift:
                candidates = without_previous_same_shift

        if request.shift_code in self.evening_shift_codes:
            without_previous_evening = [
                name
                for name in candidates
                if name not in request.previous_week_evening
            ]
            if without_previous_evening:
                candidates = without_previous_evening
            if self._has_mpr_evening_employee(request.current_week_evening, request.employees):
                candidates = [
                    name
                    for name in candidates
                    if not self.is_mpr_coordinator(request.employees[name])
                ]
            if request.current_week_day_primary_mpr:
                without_mpr = [
                    name
                    for name in candidates
                    if not self.is_mpr_coordinator(request.employees[name])
                ]
                if without_mpr:
                    candidates = without_mpr

        return self.scorer.rank_weekday_candidates(
            candidates,
            request.employees,
            request.shift_code,
            request.counters,
            request.current_month_duty_blocks,
            self.evening_priority,
        )

    def holiday_candidates(self, request: HolidayCandidateRequest) -> List[str]:
        candidates = []
        for row in request.grid.employees:
            employee = request.employees.get(row.employee_name)
            if employee is None or employee.status != "active":
                continue
            if employee.location != "moscow":
                continue
            if row.employee_name in request.unavailable:
                continue
            if not employee.overtime_ready:
                continue
            if (
                self.is_newcomer(employee)
                and self.holiday_work_code not in request.newcomer_shift_history.get(row.employee_name, set())
            ):
                continue
            candidates.append(row.employee_name)
        return sorted(
            candidates,
            key=lambda name: (
                request.current_month_holiday_work_counts.get(name, 0),
                request.holiday_work_counters.get(name, 0),
                self.holiday_work_manager_priority(request.employees[name]),
                name,
            ),
        )

    def _has_mpr_evening_employee(self, employee_names: Set[str], employees: Dict[str, Employee]) -> bool:
        return any(
            name in employees and self.is_mpr_coordinator(employees[name])
            for name in employee_names
        )
