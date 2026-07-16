from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, List, Set

from VA.schedule_manager.models.employee import Employee
from VA.schedule_manager.models.schedule_grid import ScheduleGrid, ScheduleRow
from VA.schedule_manager.repositories.employee_repository import EmployeeRepository
from VA.schedule_manager.repositories.schedule_repository import ScheduleRepository
from VA.schedule_manager.repositories.shift_repository import ShiftRepository
from VA.schedule_manager.services.competency_service import COMPETENCY_MANAGER, COMPETENCY_MPR_COORDINATOR, COMPETENCY_NEWCOMER
from VA.schedule_manager.services.duty_rules import HOLIDAY_WORK_CODE, MOSCOW_DUTY_SHIFTS, KHABAROVSK_SHIFTS, WEEKEND_CODES
from VA.schedule_manager.services.schedule_month_service import ScheduleMonthService
from VA.schedule_manager.services.schedule_service import ScheduleService
from VA.schedule_manager.services.schedule_validator import build_validation_rules, validate_schedule
from VA.schedule_manager.services.shift_service import ShiftService


LOAD_SHIFT_CODES = {"ХД", "ХР", "ДД", "ДР", "ВД", "ВР", "ВХ"}
EVENING_SHIFT_CODES = {"ВД", "ВР"}
CONTINUED_WEEK_SHIFT_CODES = {"ХД", "ХР", "ДД", "ДР", "ВД", "ВР"}
PRIMARY_DUTY_BY_LOCATION = {
    "moscow": {"ДД", "ВД"},
    "khabarovsk": {"ХД"},
}


class ScheduleAutoplanValidationError(Exception):
    pass


@dataclass(frozen=True)
class ScheduleAutoplanAvailability:
    can_autoplan: bool
    reason: str = ""


@dataclass(frozen=True)
class ScheduleAutoplanResult:
    sheet_name: str
    title: str
    assigned_cells_count: int
    violation_count: int
    artifact: dict


class ScheduleAutoplanService:
    def __init__(
        self,
        schedule_repository: ScheduleRepository,
        employee_repository: EmployeeRepository,
        shift_service: ShiftService = None,
        month_service: ScheduleMonthService = None,
    ) -> None:
        self.schedule_service = ScheduleService(schedule_repository)
        self.employee_repository = employee_repository
        self.shift_service = shift_service or ShiftService(ShiftRepository())
        self.month_service = month_service

    def availability(self, sheet_name: str) -> ScheduleAutoplanAvailability:
        try:
            self.schedule_service.get_month_grid(sheet_name)
        except KeyError:
            return ScheduleAutoplanAvailability(False, "График не найден.")
        return ScheduleAutoplanAvailability(True)

    def autoplan(self, sheet_name: str, vacations_confirmed: bool = False) -> ScheduleAutoplanResult:
        if not vacations_confirmed:
            raise ScheduleAutoplanValidationError("Подтвердите, что все отпуска внесены на планируемый месяц.")

        availability = self.availability(sheet_name)
        if not availability.can_autoplan:
            raise ScheduleAutoplanValidationError(availability.reason)

        grid = self.schedule_service.get_month_grid(sheet_name)
        employees = {employee.name: employee for employee in self.employee_repository.load_all()}
        planned, assignment_explanations = self._plan_grid(grid, employees)
        self.schedule_service.save_month_grid(sheet_name, planned)

        violations = validate_schedule(
            planned,
            build_validation_rules(self.shift_service.list_shifts(), employees.values()),
            self._previous_month_grid(planned),
        )
        assigned_cells_count = self._assigned_cells_count(grid, planned)
        artifact = self._build_artifact(planned, assigned_cells_count, len(violations), assignment_explanations)
        self.schedule_service.set_month_metadata(sheet_name, "autoplan", artifact)
        return ScheduleAutoplanResult(sheet_name, planned.title, assigned_cells_count, len(violations), artifact)

    def _month_service(self) -> ScheduleMonthService:
        if self.month_service is None:
            from VA.schedule_manager.repositories.integration_settings_repository import IntegrationSettingsRepository
            from VA.schedule_manager.services.calendar_integration_service import CalendarIntegrationService

            self.month_service = ScheduleMonthService(
                self.schedule_service.repository,
                self.employee_repository,
                CalendarIntegrationService(IntegrationSettingsRepository()),
                self.shift_service,
            )
        return self.month_service

    def _plan_grid(self, grid: ScheduleGrid, employees: Dict[str, Employee]) -> tuple[ScheduleGrid, List[dict]]:
        assignments = {row.employee_name: dict(row.assignments) for row in grid.employees}
        historical_load = self._historical_load(grid)
        holiday_work_history = self._historical_holiday_work_load(grid)
        counters: Dict[str, int] = {row.employee_name: historical_load.get(row.employee_name, 0) for row in grid.employees}
        holiday_work_counters: Dict[str, int] = {
            row.employee_name: holiday_work_history.get(row.employee_name, 0)
            for row in grid.employees
        }
        current_month_holiday_work_counts: Dict[str, int] = {row.employee_name: 0 for row in grid.employees}
        current_month_duty_blocks: Dict[str, int] = {row.employee_name: 0 for row in grid.employees}
        monthly_shift_usage: Dict[str, Set[str]] = {row.employee_name: set() for row in grid.employees}
        previous_week_evening: Set[str] = self._previous_week_evening_from_history(grid)
        assignment_explanations: List[dict] = []

        for week_days in self._week_groups(grid):
            week_day_numbers = [day.day for day in week_days]
            nonworking_day_numbers = [
                day.day
                for day in week_days
                if day.weekday.lower() in WEEKEND_CODES or self._is_holiday_day(assignments, day.day)
            ]
            workday_numbers = [
                day.day
                for day in week_days
                if day.weekday.lower() not in WEEKEND_CODES and not self._is_holiday_day(assignments, day.day)
            ]

            unavailable = {
                employee_name
                for employee_name, row_assignments in assignments.items()
                if any(self._is_absence(row_assignments.get(day, "")) for day in week_day_numbers)
            }
            week_assigned: Set[str] = set()
            for nonworking_day in nonworking_day_numbers:
                existing_nonworking_workers = self._existing_shift_workers(assignments, nonworking_day, HOLIDAY_WORK_CODE)
                if existing_nonworking_workers:
                    employee_name = existing_nonworking_workers[0]
                    counters[employee_name] = counters.get(employee_name, 0) + 1
                    holiday_work_counters[employee_name] = holiday_work_counters.get(employee_name, 0) + 1
                    current_month_holiday_work_counts[employee_name] = current_month_holiday_work_counts.get(employee_name, 0) + 1
                    current_month_duty_blocks[employee_name] = current_month_duty_blocks.get(employee_name, 0) + 1
                    continue
                candidates = self._holiday_candidates(
                    grid,
                    employees,
                    holiday_work_counters,
                    current_month_holiday_work_counts,
                    unavailable,
                )
                employee_name = candidates[0] if candidates else ""
                if employee_name:
                    assignments[employee_name][nonworking_day] = HOLIDAY_WORK_CODE
                    assignment_explanations.append(
                        self._assignment_explanation(
                            employee_name,
                            HOLIDAY_WORK_CODE,
                            f"{nonworking_day} число",
                            [nonworking_day],
                            candidates,
                            counters.get(employee_name, 0),
                            [
                                "дата является выходным или праздничным днем",
                                "на нерабочий день нужен один сотрудник в ВХ",
                                "сотрудник активен, относится к Москве, готов к сверхурочке и не в отпуске на этой неделе",
                                "выбран сотрудник с минимальным количеством ВХ в текущем месяце и за три предыдущих месяца",
                                "руководитель допускается к ВХ только с наименьшим приоритетом",
                            ],
                        )
                    )
                    counters[employee_name] += 1
                    holiday_work_counters[employee_name] += 1
                    current_month_holiday_work_counts[employee_name] += 1
                    current_month_duty_blocks[employee_name] += 1

            if not workday_numbers:
                continue

            current_week_evening: Set[str] = set()
            current_week_day_primary_mpr: Set[str] = set()
            existing_week_assignments = self._existing_week_assignments(assignments, workday_numbers)
            for shift_code, employee_name in existing_week_assignments.items():
                if employee_name not in assignments:
                    continue
                filled_days = self._fill_days(assignments[employee_name], workday_numbers, shift_code)
                if filled_days:
                    assignment_explanations.append(
                        self._assignment_explanation(
                            employee_name,
                            shift_code,
                            self._days_period(filled_days),
                            filled_days,
                            [employee_name],
                            counters.get(employee_name, 0),
                            [
                                "смена уже была начата вручную",
                                "автопланировщик дозаполнил пустые дни этого недельного блока",
                                "существующие ручные назначения не изменялись",
                            ],
                        )
                    )
                counters[employee_name] = counters.get(employee_name, 0) + self._shift_days_count(assignments[employee_name], workday_numbers, shift_code)
                current_month_duty_blocks[employee_name] = current_month_duty_blocks.get(employee_name, 0) + 1
                week_assigned.add(employee_name)
                monthly_shift_usage.setdefault(employee_name, set()).add(shift_code)
                if shift_code in EVENING_SHIFT_CODES:
                    current_week_evening.add(employee_name)
                if self._is_day_primary_mpr(employee_name, shift_code, employees):
                    current_week_day_primary_mpr.add(employee_name)

            continued_assignments = self._continued_week_assignments(grid, week_days)
            for shift_code, employee_name in continued_assignments.items():
                if shift_code in existing_week_assignments:
                    continue
                if employee_name not in assignments or employee_name in unavailable or employee_name in week_assigned:
                    continue
                filled_days = self._fill_days(assignments[employee_name], workday_numbers, shift_code)
                assignment_explanations.append(
                    self._assignment_explanation(
                        employee_name,
                        shift_code,
                        self._days_period(filled_days),
                        filled_days,
                        [employee_name],
                        counters.get(employee_name, 0),
                        [
                            "недельная смена началась в предыдущем месяце",
                            "сотрудник продолжает тот же недельный блок в новом месяце",
                            "автопланировщик не меняет исполнителя с 1 числа внутри продолжающейся недели",
                        ],
                    )
                )
                counters[employee_name] += len(filled_days)
                current_month_duty_blocks[employee_name] += 1
                week_assigned.add(employee_name)
                monthly_shift_usage.setdefault(employee_name, set()).add(shift_code)
                if shift_code in EVENING_SHIFT_CODES:
                    current_week_evening.add(employee_name)
                if self._is_day_primary_mpr(employee_name, shift_code, employees):
                    current_week_day_primary_mpr.add(employee_name)

            for shift_code in KHABAROVSK_SHIFTS:
                if shift_code in existing_week_assignments or shift_code in continued_assignments:
                    continue
                candidates = self._employee_candidates(
                    grid,
                    employees,
                    counters,
                    unavailable,
                    week_assigned,
                    location="khabarovsk",
                    allow_manager=False,
                    shift_code=shift_code,
                    monthly_shift_usage=monthly_shift_usage,
                    previous_week_evening=previous_week_evening,
                    current_week_evening=current_week_evening,
                    current_week_day_primary_mpr=current_week_day_primary_mpr,
                    current_month_duty_blocks=current_month_duty_blocks,
                )
                employee_name = candidates[0] if candidates else ""
                if employee_name:
                    filled_days = self._fill_days(assignments[employee_name], workday_numbers, shift_code)
                    assignment_explanations.append(
                        self._assignment_explanation(
                            employee_name,
                            shift_code,
                            self._days_period(filled_days),
                            filled_days,
                            candidates,
                            counters.get(employee_name, 0),
                            [
                                "смена относится к Хабаровску",
                                "сотрудник активен и относится к локации Хабаровск",
                                "на неделе у сотрудника нет отпуска",
                                "сотрудник еще не назначен в другую дежурную смену этой недели",
                                *self._newcomer_reasons(shift_code, "khabarovsk"),
                                "выбран сотрудник с наименьшим количеством дежурных блоков в месяце и минимальной текущей нагрузкой среди доступных кандидатов",
                            ],
                        )
                    )
                    counters[employee_name] += len(filled_days)
                    current_month_duty_blocks[employee_name] += 1
                    week_assigned.add(employee_name)
                    monthly_shift_usage.setdefault(employee_name, set()).add(shift_code)

            for shift_code in self._moscow_shift_order(previous_week_evening):
                if shift_code in existing_week_assignments or shift_code in continued_assignments:
                    continue
                candidates = self._employee_candidates(
                    grid,
                    employees,
                    counters,
                    unavailable,
                    week_assigned,
                    location="moscow",
                    allow_manager=False,
                    shift_code=shift_code,
                    monthly_shift_usage=monthly_shift_usage,
                    previous_week_evening=previous_week_evening,
                    current_week_evening=current_week_evening,
                    current_week_day_primary_mpr=current_week_day_primary_mpr,
                    current_month_duty_blocks=current_month_duty_blocks,
                )
                employee_name = candidates[0] if candidates else ""
                if employee_name:
                    filled_days = self._fill_days(assignments[employee_name], workday_numbers, shift_code)
                    assignment_explanations.append(
                        self._assignment_explanation(
                            employee_name,
                            shift_code,
                            self._days_period(filled_days),
                            filled_days,
                            candidates,
                            counters.get(employee_name, 0),
                            [
                                "смена относится к Москве",
                                "сотрудник активен и относится к локации Москва",
                                "на неделе у сотрудника нет отпуска",
                                "сотрудник не является руководителем",
                                "сотрудник еще не назначен в другую дежурную смену этой недели",
                                "приоритет отдавался сотрудникам, которые еще не были в этой смене в текущем месяце",
                                *self._evening_reasons(shift_code),
                                *self._newcomer_reasons(shift_code, "moscow"),
                                "выбран сотрудник с наименьшим количеством дежурных блоков в месяце и минимальной текущей нагрузкой среди доступных кандидатов",
                            ],
                        )
                    )
                    counters[employee_name] += len(filled_days)
                    current_month_duty_blocks[employee_name] += 1
                    week_assigned.add(employee_name)
                    monthly_shift_usage.setdefault(employee_name, set()).add(shift_code)
                    if shift_code in EVENING_SHIFT_CODES:
                        current_week_evening.add(employee_name)
                    if self._is_day_primary_mpr(employee_name, shift_code, employees):
                        current_week_day_primary_mpr.add(employee_name)

            for row in grid.employees:
                employee = employees.get(row.employee_name)
                if employee is None or employee.location != "moscow":
                    continue
                if row.employee_name in week_assigned:
                    continue
                filled_days = []
                for day in workday_numbers:
                    if not assignments[row.employee_name].get(day):
                        assignments[row.employee_name][day] = "8"
                        filled_days.append(day)
                if filled_days:
                    assignment_explanations.append(
                        self._assignment_explanation(
                            row.employee_name,
                            "8",
                            self._days_period(filled_days),
                            filled_days,
                            [row.employee_name],
                            counters.get(row.employee_name, 0),
                            [
                                "сотрудник активен и относится к Москве",
                                "сотрудник не занят дежурной сменой на этой неделе",
                                "основная смена 8 используется для свободных рабочих дней",
                            ],
                        )
                    )

            previous_week_evening = current_week_evening

        rows = [
            ScheduleRow(row.employee_name, self._calculate_hours(assignments[row.employee_name]), assignments[row.employee_name])
            for row in grid.employees
        ]
        return ScheduleGrid(grid.title, grid.year, grid.month, grid.days, rows), assignment_explanations

    def _pick_employee(
        self,
        grid: ScheduleGrid,
        employees: Dict[str, Employee],
        counters: Dict[str, int],
        unavailable: Set[str],
        week_assigned: Set[str],
        location: str,
        allow_manager: bool,
        shift_code: str = "",
        monthly_shift_usage: Dict[str, Set[str]] = None,
        previous_week_evening: Set[str] = None,
        current_week_evening: Set[str] = None,
        current_week_day_primary_mpr: Set[str] = None,
        current_month_duty_blocks: Dict[str, int] = None,
    ) -> str:
        candidates = self._employee_candidates(
            grid,
            employees,
            counters,
            unavailable,
            week_assigned,
            location,
            allow_manager,
            shift_code,
            monthly_shift_usage,
            previous_week_evening,
            current_week_evening,
            current_week_day_primary_mpr,
            current_month_duty_blocks,
        )
        return candidates[0] if candidates else ""

    def _employee_candidates(
        self,
        grid: ScheduleGrid,
        employees: Dict[str, Employee],
        counters: Dict[str, int],
        unavailable: Set[str],
        week_assigned: Set[str],
        location: str,
        allow_manager: bool,
        shift_code: str = "",
        monthly_shift_usage: Dict[str, Set[str]] = None,
        previous_week_evening: Set[str] = None,
        current_week_evening: Set[str] = None,
        current_week_day_primary_mpr: Set[str] = None,
        current_month_duty_blocks: Dict[str, int] = None,
    ) -> List[str]:
        monthly_shift_usage = monthly_shift_usage or {}
        previous_week_evening = previous_week_evening or set()
        current_week_evening = current_week_evening or set()
        current_week_day_primary_mpr = current_week_day_primary_mpr or set()
        current_month_duty_blocks = current_month_duty_blocks or {}
        candidates = []
        for row in grid.employees:
            employee = employees.get(row.employee_name)
            if employee is None or employee.status != "active":
                continue
            if employee.location != location:
                continue
            if row.employee_name in unavailable or row.employee_name in week_assigned:
                continue
            if not allow_manager and self._is_manager(employee):
                continue
            candidates.append(row.employee_name)
        if location == "moscow" and shift_code:
            without_same_shift = [
                name for name in candidates if shift_code not in monthly_shift_usage.get(name, set())
            ]
            if without_same_shift:
                candidates = without_same_shift
        if shift_code in EVENING_SHIFT_CODES:
            without_previous_evening = [name for name in candidates if name not in previous_week_evening]
            if without_previous_evening:
                candidates = without_previous_evening
            if self._has_mpr_evening_employee(current_week_evening, employees):
                candidates = [name for name in candidates if not self._is_mpr_coordinator(employees[name])]
            if current_week_day_primary_mpr:
                without_mpr = [name for name in candidates if not self._is_mpr_coordinator(employees[name])]
                if without_mpr:
                    candidates = without_mpr
        if self._should_avoid_newcomers(shift_code, location, employees):
            without_newcomers = [name for name in candidates if not self._is_newcomer(employees[name])]
            if without_newcomers:
                candidates = without_newcomers
        return sorted(
            candidates,
            key=lambda name: (
                current_month_duty_blocks.get(name, 0),
                self._evening_mpr_priority(employees[name], shift_code),
                counters.get(name, 0),
                name,
            ),
        )

    def _pick_holiday_employee(
        self,
        grid: ScheduleGrid,
        employees: Dict[str, Employee],
        counters: Dict[str, int],
        unavailable: Set[str],
    ) -> str:
        candidates = self._holiday_candidates(grid, employees, counters, {}, unavailable)
        return candidates[0] if candidates else ""

    def _holiday_candidates(
        self,
        grid: ScheduleGrid,
        employees: Dict[str, Employee],
        holiday_work_counters: Dict[str, int],
        current_month_holiday_work_counts: Dict[str, int],
        unavailable: Set[str],
    ) -> List[str]:
        candidates = []
        for row in grid.employees:
            employee = employees.get(row.employee_name)
            if employee is None or employee.status != "active":
                continue
            if employee.location != "moscow":
                continue
            if row.employee_name in unavailable:
                continue
            if not employee.overtime_ready:
                continue
            candidates.append(row.employee_name)
        return sorted(
            candidates,
            key=lambda name: (
                current_month_holiday_work_counts.get(name, 0),
                holiday_work_counters.get(name, 0),
                self._holiday_work_manager_priority(employees[name]),
                name,
            ),
        )

    def _holiday_work_manager_priority(self, employee: Employee) -> int:
        return 1 if self._is_manager(employee) else 0

    def _existing_shift_workers(self, assignments: Dict[str, dict], day: int, shift_code: str) -> List[str]:
        workers = []
        for employee_name, row_assignments in assignments.items():
            if self._normalize_load_code(row_assignments.get(day, "")) == shift_code:
                workers.append(employee_name)
        return workers

    def _existing_week_assignments(self, assignments: Dict[str, dict], workday_numbers: List[int]) -> Dict[str, str]:
        existing: Dict[str, str] = {}
        for day in workday_numbers:
            for employee_name, row_assignments in assignments.items():
                shift_code = self._normalize_load_code(row_assignments.get(day, ""))
                if shift_code in CONTINUED_WEEK_SHIFT_CODES and shift_code not in existing:
                    existing[shift_code] = employee_name
        return existing

    def _shift_days_count(self, row_assignments: dict, days: List[int], shift_code: str) -> int:
        return sum(
            1
            for day in days
            if self._normalize_load_code(row_assignments.get(day, "")) == shift_code
        )

    def _week_groups(self, grid: ScheduleGrid) -> List[list]:
        groups: Dict[tuple, list] = {}
        for day in grid.days:
            groups.setdefault(day.date.isocalendar()[:2], []).append(day)
        return [groups[key] for key in sorted(groups)]

    def _moscow_shift_order(self, previous_week_evening: Set[str]) -> tuple:
        if previous_week_evening:
            return ("ВД", "ВР", "ДД", "ДР")
        return MOSCOW_DUTY_SHIFTS

    def _fill_days(self, row_assignments: dict, days: List[int], shift_code: str) -> List[int]:
        filled_days = []
        for day in days:
            current_code = row_assignments.get(day, "")
            if current_code or self._is_absence(current_code):
                continue
            row_assignments[day] = shift_code
            filled_days.append(day)
        return filled_days

    def _assigned_cells_count(self, before: ScheduleGrid, after: ScheduleGrid) -> int:
        before_by_name = {row.employee_name: row.assignments for row in before.employees}
        count = 0
        for row in after.employees:
            before_assignments = before_by_name.get(row.employee_name, {})
            for day, code in row.assignments.items():
                if before_assignments.get(day, "") != code and code:
                    count += 1
        return count

    def _historical_load(self, grid: ScheduleGrid) -> Dict[str, int]:
        snapshot = self.schedule_service.get_current()
        if snapshot is None:
            return {}
        target_index = grid.year * 12 + grid.month
        previous_indexes = {target_index - offset for offset in range(1, 4)}
        load: Dict[str, int] = {}
        for month in snapshot.month_schedules:
            try:
                month_index = int(month["year"]) * 12 + int(month["month"])
            except (KeyError, TypeError, ValueError):
                continue
            if month_index not in previous_indexes:
                continue
            try:
                history_grid = snapshot.get_month_grid(str(month["sheet_name"]))
            except KeyError:
                continue
            for row in history_grid.employees:
                load[row.employee_name] = load.get(row.employee_name, 0) + sum(
                    1
                    for code in row.assignments.values()
                    if self._normalize_load_code(code) in LOAD_SHIFT_CODES
                )
        return load

    def _historical_holiday_work_load(self, grid: ScheduleGrid) -> Dict[str, int]:
        snapshot = self.schedule_service.get_current()
        if snapshot is None:
            return {}
        target_index = grid.year * 12 + grid.month
        previous_indexes = {target_index - offset for offset in range(1, 4)}
        load: Dict[str, int] = {}
        for month in snapshot.month_schedules:
            try:
                month_index = int(month["year"]) * 12 + int(month["month"])
            except (KeyError, TypeError, ValueError):
                continue
            if month_index not in previous_indexes:
                continue
            try:
                history_grid = snapshot.get_month_grid(str(month["sheet_name"]))
            except KeyError:
                continue
            for row in history_grid.employees:
                load[row.employee_name] = load.get(row.employee_name, 0) + sum(
                    1
                    for code in row.assignments.values()
                    if self._normalize_load_code(code) == HOLIDAY_WORK_CODE
                )
        return load

    def _continued_week_assignments(self, grid: ScheduleGrid, week_days: List) -> Dict[str, str]:
        if not grid.days or not week_days:
            return {}
        first_month_date = min(day.date for day in grid.days)
        if first_month_date.weekday() == 0:
            return {}
        current_week = first_month_date.isocalendar()[:2]
        if week_days[0].date.isocalendar()[:2] != current_week:
            return {}
        snapshot = self.schedule_service.get_current()
        if snapshot is None:
            return {}

        continued: Dict[str, str] = {}
        previous_days = []
        for month in snapshot.month_schedules:
            try:
                history_grid = snapshot.get_month_grid(str(month["sheet_name"]))
            except KeyError:
                continue
            for history_day in history_grid.days:
                if history_day.date >= first_month_date:
                    continue
                if history_day.date.isocalendar()[:2] != current_week:
                    continue
                previous_days.append((history_day.date, history_day.day, history_grid.employees))

        for _, day_number, rows in sorted(previous_days):
            for row in rows:
                shift_code = self._normalize_load_code(row.assignments.get(day_number, ""))
                if shift_code in CONTINUED_WEEK_SHIFT_CODES:
                    continued[shift_code] = row.employee_name
        return continued

    def _previous_week_evening_from_history(self, grid: ScheduleGrid) -> Set[str]:
        if not grid.days:
            return set()
        snapshot = self.schedule_service.get_current()
        if snapshot is None:
            return set()
        previous_week = (min(day.date for day in grid.days) - timedelta(days=1)).isocalendar()[:2]
        evening_workers: Set[str] = set()
        for month in snapshot.month_schedules:
            try:
                history_grid = snapshot.get_month_grid(str(month["sheet_name"]))
            except KeyError:
                continue
            for history_day in history_grid.days:
                if history_day.date.isocalendar()[:2] != previous_week:
                    continue
                for row in history_grid.employees:
                    if self._normalize_load_code(row.assignments.get(history_day.day, "")) in EVENING_SHIFT_CODES:
                        evening_workers.add(row.employee_name)
        return evening_workers

    def _previous_month_grid(self, grid: ScheduleGrid) -> ScheduleGrid:
        snapshot = self.schedule_service.get_current()
        if snapshot is None:
            return None
        target_index = grid.year * 12 + grid.month
        for month in snapshot.month_schedules:
            try:
                month_index = int(month["year"]) * 12 + int(month["month"])
            except (KeyError, TypeError, ValueError):
                continue
            if month_index != target_index - 1:
                continue
            try:
                return snapshot.get_month_grid(str(month["sheet_name"]))
            except KeyError:
                return None
        return None

    def _normalize_load_code(self, value: object) -> str:
        code = " ".join(str(value or "").strip().split())
        shift = self.shift_service.lookup().get(code) or self.shift_service.lookup().get(code.lower())
        return shift.code if shift else code

    def _should_avoid_newcomers(self, shift_code: str, location: str, employees: Dict[str, Employee]) -> bool:
        if shift_code not in PRIMARY_DUTY_BY_LOCATION.get(location, set()):
            return False
        active_in_location = [
            employee
            for employee in employees.values()
            if employee.status == "active" and employee.location == location and not self._is_manager(employee)
        ]
        return len(active_in_location) >= 2

    def _newcomer_reasons(self, shift_code: str, location: str) -> List[str]:
        if shift_code not in PRIMARY_DUTY_BY_LOCATION.get(location, set()):
            return []
        return ["сотрудники с компетенцией Новичок не выбирались основным дежурным при наличии других сотрудников в локации"]

    def _evening_reasons(self, shift_code: str) -> List[str]:
        if shift_code not in EVENING_SHIFT_CODES:
            return []
        return [
            "сотрудники с вечерней сменой на предыдущей неделе исключались при наличии альтернатив",
            "при прочих равных приоритет отдавался сотрудникам с компетенцией МПР-координатор",
            "в паре ВД/ВР не назначались два МПР-координатора одновременно",
            "если МПР-координатор уже назначен в ДД на этой неделе, другой МПР-координатор не выбирался в вечерние смены при наличии альтернатив",
        ]

    def _evening_mpr_priority(self, employee: Employee, shift_code: str) -> int:
        if shift_code in EVENING_SHIFT_CODES and COMPETENCY_MPR_COORDINATOR in set(employee.competencies):
            return 0
        return 1

    def _has_mpr_evening_employee(self, employee_names: Set[str], employees: Dict[str, Employee]) -> bool:
        return any(
            name in employees and self._is_mpr_coordinator(employees[name])
            for name in employee_names
        )

    def _assignment_explanation(
        self,
        employee_name: str,
        shift_code: str,
        period: str,
        days: List[int],
        candidates: List[str],
        load_before: int,
        reasons: List[str],
    ) -> dict:
        return {
            "employee_name": employee_name,
            "shift_code": shift_code,
            "shift_name": self._shift_name(shift_code),
            "period": period,
            "days": days,
            "reason": "; ".join(reasons) + ".",
            "candidate_count": len(candidates),
            "load_before": load_before,
        }

    def _days_period(self, days: List[int]) -> str:
        if not days:
            return ""
        if min(days) == max(days):
            return f"{days[0]} число"
        return f"{min(days)}-{max(days)} числа"

    def _shift_name(self, shift_code: str) -> str:
        shift = self.shift_service.lookup().get(shift_code) or self.shift_service.lookup().get(str(shift_code).lower())
        return shift.name if shift else shift_code

    def _build_artifact(
        self,
        grid: ScheduleGrid,
        assigned_cells_count: int,
        violation_count: int,
        assignment_explanations: List[dict],
    ) -> dict:
        return {
            "source": "autoplanner",
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "title": "График сформирован автопланировщиком",
            "summary": (
                f"Автопланировщик заполнил {assigned_cells_count} ячеек по справочникам сотрудников, "
                "смен и текущим правилам проверки графика, не изменяя уже внесенные назначения."
            ),
            "reasons": [
                "Перед запуском пользователь подтвердил, что отпуска и праздничные дни на месяц внесены.",
                "Уже начатые вручную недельные смены сохранялись и дозаполнялись тем же сотрудником.",
                "Сотрудники с отпуском на неделе не назначались в дежурные смены этой недели.",
                "Смены Москвы и Хабаровска назначались только сотрудникам соответствующей локации.",
                "Если недельный блок начинался в предыдущем месяце, сотрудники продолжали свои смены в первой неполной неделе нового месяца.",
                "Нагрузка кандидатов учитывала дежурные смены за три предыдущих месяца.",
                "При выборе кандидатов сначала выравнивалось количество дежурных блоков в текущем месяце.",
                "Московский сотрудник не назначался в одну и ту же дежурную смену более одного раза за месяц при наличии альтернатив.",
                "Вечерние смены ВД/ВР не назначались сотруднику две недели подряд при наличии альтернатив, включая переход между месяцами.",
                "При назначении ВД/ВР приоритет отдавался сотрудникам с компетенцией МПР-координатор, но не назначались два МПР-координатора одновременно.",
                "Если МПР-координатор назначался в ДД, другой МПР-координатор не назначался в вечернюю смену этой же недели при наличии альтернатив.",
                "Сотрудники с компетенцией Новичок не назначались основным дежурным, если в локации есть другие сотрудники.",
                "Руководитель не назначался в дежурные смены и оставался доступен только для основной смены 8.",
                "На каждый выходной или праздничный день назначался один ВХ с учетом готовности к сверхурочке, ВХ за три предыдущих месяца и ВХ в текущем месяце.",
                "Руководитель допускался к ВХ только с наименьшим приоритетом.",
                "После заполнения часы были пересчитаны, а график проверен стандартным валидатором.",
            ],
            "violation_count": violation_count,
            "year": grid.year,
            "month": grid.month,
            "assignment_explanations": assignment_explanations,
        }

    def _has_holidays(self, grid: ScheduleGrid) -> bool:
        return any(self._is_holiday(value) for row in grid.employees for value in row.assignments.values())

    def _is_holiday_day(self, assignments: Dict[str, dict], day: int) -> bool:
        return any(self._is_holiday(row_assignments.get(day, "")) for row_assignments in assignments.values())

    def _is_holiday(self, value: object) -> bool:
        return self._code_key(value) in {"п", "праздник"}

    def _is_absence(self, value: object) -> bool:
        return self._code_key(value) in {"о", "отпуск"}

    def _is_manager(self, employee: Employee) -> bool:
        return employee.role == "manager" or COMPETENCY_MANAGER in set(employee.competencies)

    def _is_newcomer(self, employee: Employee) -> bool:
        return COMPETENCY_NEWCOMER in set(employee.competencies)

    def _is_mpr_coordinator(self, employee: Employee) -> bool:
        return COMPETENCY_MPR_COORDINATOR in set(employee.competencies)

    def _is_day_primary_mpr(self, employee_name: str, shift_code: str, employees: Dict[str, Employee]) -> bool:
        employee = employees.get(employee_name)
        return shift_code == "ДД" and employee is not None and self._is_mpr_coordinator(employee)

    def _calculate_hours(self, assignments: dict) -> int:
        lookup = self.shift_service.lookup()
        total = 0.0
        for code in assignments.values():
            shift = lookup.get(str(code).strip()) or lookup.get(str(code).strip().lower())
            if shift:
                total += shift.hours
        return int(total)

    def _code_key(self, value: object) -> str:
        return " ".join(str(value or "").strip().split()).lower()
