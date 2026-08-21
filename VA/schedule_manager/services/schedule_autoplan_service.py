from dataclasses import dataclass
from datetime import timedelta
from typing import Callable, Dict, List, Optional, Set

from VA.schedule_manager.models.employee import Employee
from VA.schedule_manager.models.schedule_grid import ScheduleGrid
from VA.schedule_manager.repositories.managed_employee_repository import ManagedEmployeeRepository
from VA.schedule_manager.repositories.schedule_repository import ScheduleRepository
from VA.schedule_manager.repositories.shift_repository import ShiftRepository
from VA.schedule_manager.services.autoplan.audit import AutoplanAuditBuilder
from VA.schedule_manager.services.autoplan.capacity import DutyCapacityDiagnostics
from VA.schedule_manager.services.autoplan.candidates import (
    CandidateGenerator,
    HolidayCandidateRequest,
    WeekdayCandidateRequest,
)
from VA.schedule_manager.services.autoplan.context import PlanningContext
from VA.schedule_manager.services.autoplan.planner import MonthPlanner, PlannerDependencies
from VA.schedule_manager.services.autoplan.rules import EmployeeRuleSet
from VA.schedule_manager.services.autoplan.scoring import CandidateScorer, ScoringPolicy
from VA.schedule_manager.services.autoplan_contract import AUTOPLAN_CONTRACT
from VA.schedule_manager.services.newcomer_history import collect_newcomer_shift_codes
from VA.schedule_manager.services.duty_rules import HOLIDAY_WORK_CODE, MOSCOW_DUTY_SHIFTS, KHABAROVSK_SHIFTS, WEEKEND_CODES
from VA.schedule_manager.services.schedule_month_service import ScheduleMonthService
from VA.schedule_manager.services.schedule_service import ScheduleService
from VA.schedule_manager.services.schedule_validator import build_validation_rules, validate_schedule
from VA.schedule_manager.services.shift_service import ShiftService


LOAD_SHIFT_CODES = set(AUTOPLAN_CONTRACT.load_shift_codes)
EVENING_SHIFT_CODES = set(AUTOPLAN_CONTRACT.evening_shift_codes)
CONTINUED_WEEK_SHIFT_CODES = set(AUTOPLAN_CONTRACT.weekday_shift_codes)
DUTY_SHIFT_CODES = set(AUTOPLAN_CONTRACT.weekday_shift_codes)
NEWCOMER_TRAINEE_SHIFT_CODES = frozenset(AUTOPLAN_CONTRACT.newcomer_trainee_shift_codes)


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
        employee_repository: ManagedEmployeeRepository,
        shift_service: ShiftService = None,
        month_service: ScheduleMonthService = None,
    ) -> None:
        self.schedule_service = ScheduleService(schedule_repository)
        self.employee_repository = employee_repository
        self.shift_service = shift_service or ShiftService(ShiftRepository())
        self.month_service = month_service
        self.candidate_scorer = CandidateScorer()
        self.employee_rules = EmployeeRuleSet()
        self.audit_builder = AutoplanAuditBuilder(self._shift_name)
        self.candidate_generator = CandidateGenerator(
            self.candidate_scorer,
            self.employee_rules.is_manager,
            self.employee_rules.is_newcomer,
            self.employee_rules.is_mpr_coordinator,
            self.employee_rules.evening_mpr_priority,
            self.employee_rules.holiday_work_manager_priority,
            EVENING_SHIFT_CODES,
            KHABAROVSK_SHIFTS,
            HOLIDAY_WORK_CODE,
            NEWCOMER_TRAINEE_SHIFT_CODES,
        )

    def availability(self, sheet_name: str) -> ScheduleAutoplanAvailability:
        try:
            self.schedule_service.get_month_grid(sheet_name)
        except KeyError:
            return ScheduleAutoplanAvailability(False, "График не найден.")
        try:
            employees = self.employee_repository.load_all()
        except RuntimeError:
            return ScheduleAutoplanAvailability(
                False,
                "Current employee list is temporarily unavailable.",
            )
        if not [employee for employee in employees if employee.status == "active"]:
            return ScheduleAutoplanAvailability(False, "No active employees for planning.")
        return ScheduleAutoplanAvailability(True)

    def autoplan(
        self,
        sheet_name: str,
        vacations_confirmed: bool = False,
        progress: Optional[Callable[[str], None]] = None,
    ) -> ScheduleAutoplanResult:
        self._progress(progress, "Проверяю подтверждение отпусков и праздничных дней.")
        if not vacations_confirmed:
            raise ScheduleAutoplanValidationError("Подтвердите, что все отпуска внесены на планируемый месяц.")

        self._progress(progress, "Проверяю доступность графика и справочника сотрудников.")
        availability = self.availability(sheet_name)
        if not availability.can_autoplan:
            raise ScheduleAutoplanValidationError(availability.reason)

        self._progress(progress, "Загружаю текущий график из данных АС.")
        grid = self.schedule_service.get_month_grid(sheet_name)
        self._progress(progress, "Загружаю сотрудников, смены и компетенции.")
        employees = {employee.name: employee for employee in self.employee_repository.load_all()}
        self._progress(progress, "Анализирую нагрузку и смены за три предыдущих месяца.")
        newcomer_shift_history = self._historical_shift_codes(grid)
        duty_capacity = DutyCapacityDiagnostics(ScoringPolicy().preferred_month_duty_blocks)
        planned, assignment_explanations, stop_cells = self._plan_grid(
            grid, employees, progress=progress, duty_capacity=duty_capacity
        )

        self._progress(progress, "Запускаю проверку правил сформированного графика.")
        violations = validate_schedule(
            planned,
            build_validation_rules(
                self.shift_service.list_shifts(),
                employees.values(),
                newcomer_allowed_shift_codes=newcomer_shift_history,
                newcomer_trainee_shift_codes=NEWCOMER_TRAINEE_SHIFT_CODES,
            ),
            self._previous_month_grid(planned),
        )
        assigned_cells_count = self._assigned_cells_count(grid, planned)
        artifact = self.audit_builder.build_artifact(
            planned, assigned_cells_count, len(violations), assignment_explanations,
            stop_cells, duty_capacity.build()
        )
        self._progress(progress, "Сохраняю график и пояснения автопланировщика.")
        self.schedule_service.save_month_grid(
            sheet_name,
            planned,
            metadata_updates={"autoplan": artifact},
        )
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

    def _progress(self, progress: Optional[Callable[[str], None]], message: str) -> None:
        if progress is not None:
            progress(message)

    def _plan_grid(
        self,
        grid: ScheduleGrid,
        employees: Dict[str, Employee],
        progress: Optional[Callable[[str], None]] = None,
        duty_capacity: DutyCapacityDiagnostics = None,
    ) -> tuple[ScheduleGrid, List[dict], List[dict]]:
        duty_capacity = duty_capacity or DutyCapacityDiagnostics(ScoringPolicy().preferred_month_duty_blocks)
        context = PlanningContext.build(
            grid,
            employees,
            self._historical_load(grid),
            self._historical_holiday_work_load(grid),
            self._historical_shift_codes(grid),
        )
        previous_week_evening: Set[str] = self._previous_week_evening_from_history(grid)

        if grid.days and min(day.date for day in grid.days).weekday() != 0:
            self._progress(progress, "Месяц начинается не с понедельника: проверяю хвост недели в предыдущем графике.")

        planner = MonthPlanner(
            PlannerDependencies(
                duty_capacity=duty_capacity,
                audit_builder=self.audit_builder,
                candidate_generator=self.candidate_generator,
                employee_rules=self.employee_rules,
                holiday_work_code=HOLIDAY_WORK_CODE,
                khabarovsk_shifts=KHABAROVSK_SHIFTS,
                evening_shift_codes=EVENING_SHIFT_CODES,
                weekend_codes=WEEKEND_CODES,
                week_groups=self._week_groups,
                moscow_shift_order=self._moscow_shift_order,
                is_holiday_day=self._is_holiday_day,
                is_absence=self._is_absence,
                existing_shift_workers=self._existing_shift_workers,
                existing_week_assignments=self._existing_week_assignments,
                continued_week_assignments=self._continued_week_assignments,
                fill_days=self._fill_days,
                fill_days_until_stop=self._fill_days_until_stop,
                calculate_hours=self._calculate_hours,
                days_period=self._days_period,
                newcomer_reasons=self._newcomer_reasons,
                evening_reasons=self._evening_reasons,
            )
        )
        return planner.plan(context, previous_week_evening, progress)

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
        newcomer_shift_history: Dict[str, Set[str]] = None,
        previous_week_shift_workers: Dict[str, Set[str]] = None,
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
            newcomer_shift_history,
            previous_week_shift_workers,
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
        newcomer_shift_history: Dict[str, Set[str]] = None,
        previous_week_shift_workers: Dict[str, Set[str]] = None,
    ) -> List[str]:
        return self.candidate_generator.weekday_candidates(
            WeekdayCandidateRequest(
                grid=grid,
                employees=employees,
                counters=counters,
                unavailable=unavailable,
                week_assigned=week_assigned,
                location=location,
                allow_manager=allow_manager,
                shift_code=shift_code,
                monthly_shift_usage=monthly_shift_usage or {},
                previous_week_evening=previous_week_evening or set(),
                current_week_evening=current_week_evening or set(),
                current_week_day_primary_mpr=current_week_day_primary_mpr or set(),
                current_month_duty_blocks=current_month_duty_blocks or {},
                newcomer_shift_history=newcomer_shift_history or {},
                previous_week_shift_workers=previous_week_shift_workers or {},
            )
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
        newcomer_shift_history: Dict[str, Set[str]] = None,
    ) -> List[str]:
        return self.candidate_generator.holiday_candidates(
            HolidayCandidateRequest(
                grid=grid,
                employees=employees,
                holiday_work_counters=holiday_work_counters,
                current_month_holiday_work_counts=current_month_holiday_work_counts,
                unavailable=unavailable,
                newcomer_shift_history=newcomer_shift_history or {},
            )
        )

    def _holiday_work_manager_priority(self, employee: Employee) -> int:
        return self.employee_rules.holiday_work_manager_priority(employee)

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

    def _fill_days(
        self,
        row_assignments: dict,
        days: List[int],
        shift_code: str,
        replace_default_shift: bool = False,
    ) -> List[int]:
        filled_days = []
        for day in days:
            current_code = row_assignments.get(day, "")
            if current_code and not (
                replace_default_shift and self._is_default_work_shift(current_code)
            ):
                continue
            row_assignments[day] = shift_code
            filled_days.append(day)
        return filled_days

    def _fill_days_until_stop(
        self,
        row_assignments: dict,
        days: List[int],
        shift_code: str,
    ) -> tuple[List[int], Optional[int], List[int]]:
        filled_days: List[int] = []
        for index, day in enumerate(days):
            current_code = row_assignments.get(day, "")
            if current_code and not self._is_default_work_shift(current_code):
                return filled_days, day, days[index:]
            row_assignments[day] = shift_code
            filled_days.append(day)
        return filled_days, None, []

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
        previous_indexes = {
            target_index - offset
            for offset in range(1, AUTOPLAN_CONTRACT.historical_load_months + 1)
        }
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
                blocks = set()
                for schedule_day in history_grid.days:
                    shift_code = self._normalize_load_code(row.assignments.get(schedule_day.day, ""))
                    if shift_code not in LOAD_SHIFT_CODES:
                        continue
                    if shift_code == HOLIDAY_WORK_CODE:
                        blocks.add((shift_code, schedule_day.date))
                    else:
                        blocks.add((shift_code, schedule_day.date.isocalendar()[:2]))
                load[row.employee_name] = load.get(row.employee_name, 0) + len(blocks)
        return load

    def _historical_holiday_work_load(self, grid: ScheduleGrid) -> Dict[str, int]:
        snapshot = self.schedule_service.get_current()
        if snapshot is None:
            return {}
        target_index = grid.year * 12 + grid.month
        previous_indexes = {
            target_index - offset
            for offset in range(1, AUTOPLAN_CONTRACT.historical_load_months + 1)
        }
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

    def _historical_shift_codes(self, grid: ScheduleGrid) -> Dict[str, Set[str]]:
        return collect_newcomer_shift_codes(
            self.schedule_service.get_current(),
            grid,
            self._normalize_load_code,
            LOAD_SHIFT_CODES,
            include_current_month=True,
        )

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

    def _newcomer_reasons(self, shift_code: str, location: str) -> List[str]:
        if shift_code not in DUTY_SHIFT_CODES:
            return []
        return ["Новичок допускается к резервным сменам ДР/ВР сразу; к остальным дежурным сменам и ВХ — только если такая смена уже была у него в графике, включая текущий месяц"]

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
        return self.employee_rules.evening_mpr_priority(employee, shift_code)

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
        return self.audit_builder.assignment_explanation(
            employee_name,
            shift_code,
            period,
            days,
            candidates,
            load_before,
            reasons,
        )

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
        stop_cells: List[dict] = None,
    ) -> dict:
        return self.audit_builder.build_artifact(
            grid,
            assigned_cells_count,
            violation_count,
            assignment_explanations,
            stop_cells,
        )

    def _has_holidays(self, grid: ScheduleGrid) -> bool:
        return any(self._is_holiday(value) for row in grid.employees for value in row.assignments.values())

    def _is_holiday_day(self, assignments: Dict[str, dict], day: int) -> bool:
        return any(self._is_holiday(row_assignments.get(day, "")) for row_assignments in assignments.values())

    def _is_holiday(self, value: object) -> bool:
        return self._code_key(value) in {"п", "праздник"}

    def _is_absence(self, value: object) -> bool:
        return self._code_key(value) in {"о", "отпуск"}

    def _is_default_work_shift(self, value: object) -> bool:
        return self._code_key(value) == "8"

    def _is_manager(self, employee: Employee) -> bool:
        return self.employee_rules.is_manager(employee)

    def _is_newcomer(self, employee: Employee) -> bool:
        return self.employee_rules.is_newcomer(employee)

    def _is_mpr_coordinator(self, employee: Employee) -> bool:
        return self.employee_rules.is_mpr_coordinator(employee)

    def _is_day_primary_mpr(self, employee_name: str, shift_code: str, employees: Dict[str, Employee]) -> bool:
        return self.employee_rules.is_day_primary_mpr(employee_name, shift_code, employees)

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
