from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Set

from VA.schedule_manager.models.employee import Employee
from VA.schedule_manager.models.schedule_grid import ScheduleGrid
from VA.schedule_manager.models.shift import ShiftDefinition
from VA.schedule_manager.repositories.employee_repository import EmployeeRepository
from VA.schedule_manager.repositories.shift_repository import ShiftRepository
from VA.schedule_manager.services.competency_service import COMPETENCY_MANAGER, COMPETENCY_MPR_COORDINATOR
from VA.schedule_manager.services.duty_rules import (
    DUTY_SHIFTS,
    HOLIDAY_WORK_CODE,
    REQUIRED_WEEKDAY_DUTY_SHIFTS,
    WEEKEND_CODES,
    WEEKEND_MARK,
)
from VA.schedule_manager.services.shift_service import ShiftService


@dataclass(frozen=True)
class ScheduleViolation:
    day: int
    shift: str
    employee_name: str
    message: str


@dataclass(frozen=True)
class ScheduleValidationRules:
    required_weekday_duty_shifts: Set[str]
    moscow_restricted_shifts: Set[str]
    khabarovsk_restricted_shifts: Set[str]
    absence_codes: Set[str]
    holiday_codes: Set[str]
    holiday_work_code: str
    weekend_mark: str
    alias_lookup: Dict[str, str]
    manager_names: Set[str]
    moscow_employee_names: Set[str]
    khabarovsk_employee_names: Set[str]
    mpr_coordinator_names: Set[str]
    overtime_ready_employee_names: Set[str]
    day_primary_shift_code: str
    evening_shift_codes: Set[str]
    continued_week_shift_codes: Set[str]

    @property
    def weekend_allowed_codes(self) -> Set[str]:
        return {"", self.weekend_mark, self.holiday_work_code, *self.absence_codes, *self.holiday_codes}

    @property
    def holiday_allowed_codes(self) -> Set[str]:
        return self.weekend_allowed_codes

    def canonical_code(self, value: object) -> str:
        normalized = _normalize_code(value)
        if not normalized:
            return ""
        return self.alias_lookup.get(normalized, self.alias_lookup.get(normalized.lower(), normalized))


def build_validation_rules(
    shifts: Optional[Iterable[ShiftDefinition]] = None,
    employees: Optional[Iterable[Employee]] = None,
) -> ScheduleValidationRules:
    shift_list = list(shifts) if shifts is not None else ShiftService(ShiftRepository()).list_shifts()
    employee_list = list(employees) if employees is not None else EmployeeRepository().load_all()
    alias_lookup = _build_alias_lookup(shift_list)
    absence_codes = _codes_by_meaning(shift_list, ("отпуск",))
    holiday_codes = _codes_by_meaning(shift_list, ("праздник",))
    holiday_work_code = _canonical_from_lookup(HOLIDAY_WORK_CODE, alias_lookup)
    day_primary_shift_code = _canonical_from_lookup("ДД", alias_lookup)
    employee_groups = _employee_groups(employee_list)

    khabarovsk_shifts = {
        shift.code for shift in shift_list if _normalize_code(shift.timezone).lower() in {"хаб", "хабаровск"}
    }
    moscow_restricted_shifts = {
        shift.code
        for shift in shift_list
        if _normalize_code(shift.timezone).lower() == "мск"
        and shift.hours > 0
        and shift.code != holiday_work_code
    }

    return ScheduleValidationRules(
        required_weekday_duty_shifts={
            _canonical_from_lookup(code, alias_lookup) for code in REQUIRED_WEEKDAY_DUTY_SHIFTS
        },
        moscow_restricted_shifts=moscow_restricted_shifts,
        khabarovsk_restricted_shifts=khabarovsk_shifts,
        absence_codes=absence_codes,
        holiday_codes=holiday_codes,
        holiday_work_code=holiday_work_code,
        weekend_mark=WEEKEND_MARK,
        alias_lookup=alias_lookup,
        manager_names=employee_groups["managers"],
        moscow_employee_names=employee_groups["moscow"],
        khabarovsk_employee_names=employee_groups["khabarovsk"],
        mpr_coordinator_names=employee_groups["mpr"],
        overtime_ready_employee_names=employee_groups["overtime_ready"],
        day_primary_shift_code=day_primary_shift_code,
        evening_shift_codes=_evening_shift_codes(shift_list),
        continued_week_shift_codes={_canonical_from_lookup(code, alias_lookup) for code in DUTY_SHIFTS},
    )


def validate_schedule(
    grid: ScheduleGrid,
    rules: Optional[ScheduleValidationRules] = None,
    previous_grid: Optional[ScheduleGrid] = None,
) -> List[ScheduleViolation]:
    rules = rules or build_validation_rules()
    violations: List[ScheduleViolation] = []

    for schedule_day in grid.days:
        assignments_by_shift = _assignments_by_shift(grid, schedule_day.day, rules)
        if _is_holiday(assignments_by_shift, rules):
            violations.extend(_validate_holiday(grid, schedule_day.day, assignments_by_shift, rules))
            continue

        if schedule_day.weekday.lower() in WEEKEND_CODES:
            violations.extend(_validate_weekend(grid, schedule_day.day, rules))
            continue

        for shift_code in sorted(rules.required_weekday_duty_shifts):
            employees = assignments_by_shift.get(shift_code, [])
            if len(employees) != 1:
                violations.append(
                    ScheduleViolation(
                        day=schedule_day.day,
                        shift=shift_code,
                        employee_name="",
                        message=f"Смена {shift_code} должна быть закрыта ровно одним сотрудником.",
                    )
                )

        violations.extend(_validate_employee_restrictions(grid, schedule_day.day, rules))
        violations.extend(_validate_mpr_restrictions(grid, schedule_day.day, rules))

    if previous_grid is not None:
        violations.extend(_validate_transition_week(grid, previous_grid, rules))

    return violations


def _assignments_by_shift(grid: ScheduleGrid, day: int, rules: ScheduleValidationRules) -> Dict[str, List[str]]:
    assignments: Dict[str, List[str]] = {}
    for row in grid.employees:
        code = rules.canonical_code(row.assignments.get(day, ""))
        if code:
            assignments.setdefault(code, []).append(row.employee_name)
    return assignments


def _is_holiday(assignments_by_shift: Dict[str, List[str]], rules: ScheduleValidationRules) -> bool:
    return any(code in assignments_by_shift for code in rules.holiday_codes)


def _validate_holiday(
    grid: ScheduleGrid, day: int, assignments_by_shift: Dict[str, List[str]], rules: ScheduleValidationRules
) -> List[ScheduleViolation]:
    violations: List[ScheduleViolation] = []
    holiday_workers = assignments_by_shift.get(rules.holiday_work_code, [])
    if len(holiday_workers) != 1:
        violations.append(
            ScheduleViolation(
                day=day,
                shift=rules.holiday_work_code,
                employee_name="",
                message=f"В выходной или праздничный день должна быть ровно одна смена {rules.holiday_work_code}.",
            )
        )

    for row in grid.employees:
        code = rules.canonical_code(row.assignments.get(day, ""))
        if code not in rules.holiday_allowed_codes:
            violations.append(
                ScheduleViolation(
                    day=day,
                    shift=code,
                    employee_name=row.employee_name,
                    message=f"В праздничный день смен быть не должно, кроме {rules.holiday_work_code}.",
                )
            )
        if code == rules.holiday_work_code:
            violations.extend(_validate_holiday_work_employee(row.employee_name, day, code, rules))
    return violations


def _validate_weekend(grid: ScheduleGrid, day: int, rules: ScheduleValidationRules) -> List[ScheduleViolation]:
    violations: List[ScheduleViolation] = []
    assignments_by_shift = _assignments_by_shift(grid, day, rules)
    holiday_workers = assignments_by_shift.get(rules.holiday_work_code, [])
    if len(holiday_workers) != 1:
        violations.append(
            ScheduleViolation(
                day=day,
                shift=rules.holiday_work_code,
                employee_name="",
                message=f"В выходной или праздничный день должна быть ровно одна смена {rules.holiday_work_code}.",
            )
        )
    for row in grid.employees:
        code = rules.canonical_code(row.assignments.get(day, ""))
        if code not in rules.weekend_allowed_codes:
            violations.append(
                ScheduleViolation(
                    day=day,
                    shift=code,
                    employee_name=row.employee_name,
                    message="В субботу и воскресенье смен быть не должно.",
                )
            )
        if code == rules.holiday_work_code:
            violations.extend(_validate_holiday_work_employee(row.employee_name, day, code, rules))
    return violations


def _validate_holiday_work_employee(
    employee_name: str,
    day: int,
    code: str,
    rules: ScheduleValidationRules,
) -> List[ScheduleViolation]:
    if (
        employee_name in rules.moscow_employee_names
        and employee_name in rules.overtime_ready_employee_names
    ):
        return []
    return [
        ScheduleViolation(
            day,
            code,
            employee_name,
            "ВХ можно назначать только активному московскому сотруднику с признаком готовности к сверхурочке.",
        )
    ]


def _validate_transition_week(
    grid: ScheduleGrid,
    previous_grid: ScheduleGrid,
    rules: ScheduleValidationRules,
) -> List[ScheduleViolation]:
    if not grid.days:
        return []
    first_date = min(day.date for day in grid.days)
    if first_date.weekday() == 0:
        return []
    transition_week = first_date.isocalendar()[:2]
    first_week_days = [
        day
        for day in grid.days
        if day.date.isocalendar()[:2] == transition_week
        and day.weekday.lower() not in WEEKEND_CODES
    ]
    if not first_week_days:
        return []

    continued = _previous_week_assignments(previous_grid, first_date, transition_week, rules)
    if not continued:
        return []

    violations: List[ScheduleViolation] = []
    for schedule_day in first_week_days:
        assignments_by_shift = _assignments_by_shift(grid, schedule_day.day, rules)
        if _is_holiday(assignments_by_shift, rules):
            continue
        for shift_code, expected_employee in continued.items():
            current_workers = assignments_by_shift.get(shift_code, [])
            if current_workers == [expected_employee]:
                continue
            violations.append(
                ScheduleViolation(
                    day=schedule_day.day,
                    shift=shift_code,
                    employee_name=expected_employee,
                    message=f"Переходящая смена {shift_code} должна продолжаться сотрудником {expected_employee} из предыдущего месяца.",
                )
            )
    return violations


def _previous_week_assignments(
    previous_grid: ScheduleGrid,
    first_date,
    transition_week: tuple,
    rules: ScheduleValidationRules,
) -> Dict[str, str]:
    continued: Dict[str, str] = {}
    previous_days = [
        day
        for day in previous_grid.days
        if day.date < first_date and day.date.isocalendar()[:2] == transition_week
    ]
    for schedule_day in sorted(previous_days, key=lambda item: item.date):
        for row in previous_grid.employees:
            shift_code = rules.canonical_code(row.assignments.get(schedule_day.day, ""))
            if shift_code in rules.continued_week_shift_codes:
                continued[shift_code] = row.employee_name
    return continued


def _validate_employee_restrictions(
    grid: ScheduleGrid, day: int, rules: ScheduleValidationRules
) -> List[ScheduleViolation]:
    violations: List[ScheduleViolation] = []
    for row in grid.employees:
        code = rules.canonical_code(row.assignments.get(day, ""))
        if not code or code in rules.absence_codes or code == rules.weekend_mark:
            continue

        if code == rules.holiday_work_code:
            if (
                row.employee_name not in rules.moscow_employee_names
                or row.employee_name not in rules.overtime_ready_employee_names
            ):
                violations.append(
                    ScheduleViolation(
                        day,
                        code,
                        row.employee_name,
                        "ВХ можно назначать только активному московскому сотруднику с признаком готовности к сверхурочке.",
                    )
                )

        if row.employee_name in rules.manager_names and code != "8":
            violations.append(
                ScheduleViolation(day, code, row.employee_name, "Руководитель может быть только в смене 8.")
            )
        if row.employee_name in rules.moscow_employee_names and code in rules.khabarovsk_restricted_shifts:
            violations.append(
                ScheduleViolation(day, code, row.employee_name, "Московского сотрудника нельзя назначать на ХД/ХР.")
            )
        if row.employee_name in rules.khabarovsk_employee_names and code in rules.moscow_restricted_shifts:
            violations.append(
                ScheduleViolation(day, code, row.employee_name, "Хабаровского сотрудника нельзя назначать на 8/ДД/ДР/ВД/ВР.")
            )
    return violations


def _validate_mpr_restrictions(grid: ScheduleGrid, day: int, rules: ScheduleValidationRules) -> List[ScheduleViolation]:
    if not rules.mpr_coordinator_names:
        return []

    violations: List[ScheduleViolation] = []
    mpr_rows = [row for row in grid.employees if row.employee_name in rules.mpr_coordinator_names]
    evening_workers = [
        row.employee_name
        for row in mpr_rows
        if rules.canonical_code(row.assignments.get(day, "")) in rules.evening_shift_codes
    ]
    if len(evening_workers) > 1:
        violations.append(
            ScheduleViolation(
                day,
                "/".join(sorted(rules.evening_shift_codes)),
                "",
                "МПР-координаторы не должны одновременно быть в вечерних сменах.",
            )
        )

    day_primary_workers = [
        row.employee_name
        for row in mpr_rows
        if rules.canonical_code(row.assignments.get(day, "")) == rules.day_primary_shift_code
    ]
    if day_primary_workers:
        blocked_codes = rules.evening_shift_codes | rules.absence_codes
        for row in mpr_rows:
            if row.employee_name in day_primary_workers:
                continue
            code = rules.canonical_code(row.assignments.get(day, ""))
            if code in blocked_codes:
                violations.append(
                    ScheduleViolation(
                        day,
                        code,
                        row.employee_name,
                        "Если МПР-координатор основной дежурный днем, другой МПР-координатор не должен быть в отпуске или вечерней смене.",
                    )
                )
    return violations


def _employee_groups(employees: Iterable[Employee]) -> Dict[str, Set[str]]:
    groups = {"managers": set(), "moscow": set(), "khabarovsk": set(), "mpr": set(), "overtime_ready": set()}
    for employee in employees:
        if employee.status != "active":
            continue
        competencies = set(employee.competencies)
        if employee.role == "manager":
            competencies.add(COMPETENCY_MANAGER)
        if COMPETENCY_MANAGER in competencies:
            groups["managers"].add(employee.name)
        if COMPETENCY_MPR_COORDINATOR in competencies:
            groups["mpr"].add(employee.name)
        if employee.overtime_ready:
            groups["overtime_ready"].add(employee.name)
        if employee.location == "moscow":
            groups["moscow"].add(employee.name)
        if employee.location == "khabarovsk":
            groups["khabarovsk"].add(employee.name)
    return groups


def _evening_shift_codes(shifts: Iterable[ShiftDefinition]) -> Set[str]:
    result = set()
    for shift in shifts:
        text = " ".join((shift.code, shift.short_name, shift.name, shift.description)).lower()
        if "вечер" in text:
            result.add(shift.code)
    return result


def _build_alias_lookup(shifts: Iterable[ShiftDefinition]) -> Dict[str, str]:
    result: Dict[str, str] = {}
    for shift in shifts:
        for value in (shift.code, shift.short_name, *shift.aliases):
            normalized = _normalize_code(value)
            if normalized:
                result[normalized] = shift.code
                result[normalized.lower()] = shift.code
    return result


def _codes_by_meaning(shifts: Iterable[ShiftDefinition], keywords: Iterable[str]) -> Set[str]:
    lowered_keywords = tuple(keyword.lower() for keyword in keywords)
    result = set()
    for shift in shifts:
        text = " ".join((shift.code, shift.short_name, shift.name, shift.description)).lower()
        if any(keyword in text for keyword in lowered_keywords):
            result.add(shift.code)
    return result


def _canonical_from_lookup(value: str, alias_lookup: Dict[str, str]) -> str:
    normalized = _normalize_code(value)
    return alias_lookup.get(normalized, alias_lookup.get(normalized.lower(), normalized))


def _normalize_code(value: object) -> str:
    if value is None:
        return ""
    return " ".join(str(value).strip().split())
