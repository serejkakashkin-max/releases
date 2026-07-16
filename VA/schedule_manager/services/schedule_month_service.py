import calendar
from dataclasses import dataclass
from datetime import date, datetime
from typing import List

from VA.schedule_manager.models.employee import Employee
from VA.schedule_manager.models.schedule_grid import ScheduleDay, ScheduleGrid, ScheduleRow
from VA.schedule_manager.models.schedule_snapshot import ScheduleSnapshot
from VA.schedule_manager.repositories.employee_repository import EmployeeRepository
from VA.schedule_manager.repositories.schedule_repository import ScheduleRepository
from VA.schedule_manager.repositories.shift_repository import ShiftRepository
from VA.schedule_manager.services.calendar_integration_service import CalendarIntegrationService
from VA.schedule_manager.services.employee_service import EmployeeService
from VA.schedule_manager.services.shift_service import ShiftService


MONTH_NAMES = {
    1: "Январь",
    2: "Февраль",
    3: "Март",
    4: "Апрель",
    5: "Май",
    6: "Июнь",
    7: "Июль",
    8: "Август",
    9: "Сентябрь",
    10: "Октябрь",
    11: "Ноябрь",
    12: "Декабрь",
}
WEEKDAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]


class ScheduleMonthValidationError(Exception):
    pass


@dataclass(frozen=True)
class ScheduleMonthCreateResult:
    sheet_name: str
    title: str
    year: int
    month: int
    employee_count: int
    calendar_source: str
    calendar_warning: str = ""


@dataclass(frozen=True)
class ScheduleMonthUsage:
    sheet_name: str
    title: str
    is_empty: bool
    filled_cells_count: int
    allowed_cells_count: int


@dataclass(frozen=True)
class ScheduleMonthDeleteResult:
    sheet_name: str
    title: str
    action: str
    filled_cells_count: int


@dataclass(frozen=True)
class ScheduleMonthCopyResult:
    source_sheet_name: str
    sheet_name: str
    title: str
    year: int
    month: int
    employee_count: int
    overwritten: bool
    calendar_source: str
    calendar_warning: str = ""


class ScheduleMonthService:
    def __init__(
        self,
        schedule_repository: ScheduleRepository,
        employee_repository: EmployeeRepository,
        calendar_service: CalendarIntegrationService,
        shift_service: ShiftService = None,
    ) -> None:
        self.schedule_repository = schedule_repository
        self.employee_service = EmployeeService(employee_repository)
        self.calendar_service = calendar_service
        self.shift_service = shift_service or ShiftService(ShiftRepository())

    def create_month(self, year: int, month: int, employee_source: str = "last_schedule") -> ScheduleMonthCreateResult:
        self._validate_period(year, month)
        if employee_source not in {"last_schedule", "directory"}:
            raise ScheduleMonthValidationError("Некорректный источник сотрудников.")
        snapshot = self.schedule_repository.load()
        if snapshot is not None and self._has_month(snapshot, year, month):
            raise ScheduleMonthValidationError("График за выбранный месяц уже есть.")

        employees = self._employees_for_new_month(snapshot, employee_source)
        if not employees:
            raise ScheduleMonthValidationError("Нет активных сотрудников для нового графика.")

        calendar_state = self.calendar_service.load_calendar(year, month)
        grid = self._build_empty_grid(year, month, employees, calendar_state.holidays)
        month_name = MONTH_NAMES[month]
        sheet_name = f"{month_name} {year}"

        if snapshot is None:
            snapshot = ScheduleSnapshot(
                employees=[Employee(name=name) for name in employees],
                original_filename="Создано в АС",
                stored_filename="",
                uploaded_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                month_schedules=[],
            )

        self.schedule_repository.save(snapshot.add_month_grid(sheet_name, month_name, grid))
        return ScheduleMonthCreateResult(
            sheet_name=sheet_name,
            title=grid.title,
            year=year,
            month=month,
            employee_count=len(employees),
            calendar_source=calendar_state.source,
            calendar_warning=calendar_state.warning,
        )

    def analyze_month(self, sheet_name: str) -> ScheduleMonthUsage:
        snapshot = self.schedule_repository.load()
        if snapshot is None:
            raise ScheduleMonthValidationError("Данные графиков не загружены.")

        try:
            grid = snapshot.get_month_grid(sheet_name)
        except KeyError as exc:
            raise ScheduleMonthValidationError("График не найден.") from exc

        allowed_codes = self._allowed_empty_codes()
        filled_cells_count = 0
        allowed_cells_count = 0
        for row in grid.employees:
            for code in row.assignments.values():
                normalized = self._normalize_code(code)
                if not normalized:
                    continue
                if self._code_key(normalized) in allowed_codes:
                    allowed_cells_count += 1
                else:
                    filled_cells_count += 1

        return ScheduleMonthUsage(
            sheet_name=sheet_name,
            title=grid.title,
            is_empty=filled_cells_count == 0,
            filled_cells_count=filled_cells_count,
            allowed_cells_count=allowed_cells_count,
        )

    def delete_month(self, sheet_name: str, action: str = "delete_empty") -> ScheduleMonthDeleteResult:
        usage = self.analyze_month(sheet_name)
        if action == "delete_empty" and not usage.is_empty:
            raise ScheduleMonthValidationError("В графике есть заполненные смены. Сначала очистите их или подтвердите удаление графика целиком.")
        if action == "clear_filled":
            self._clear_filled_assignments(sheet_name)
            return ScheduleMonthDeleteResult(sheet_name, usage.title, "clear_filled", usage.filled_cells_count)
        if action not in {"delete_empty", "delete_any"}:
            raise ScheduleMonthValidationError("Некорректное действие с графиком.")

        snapshot = self.schedule_repository.load()
        if snapshot is None:
            raise ScheduleMonthValidationError("Данные графиков не загружены.")
        self.schedule_repository.save(snapshot.remove_month_grid(sheet_name))
        return ScheduleMonthDeleteResult(sheet_name, usage.title, "delete", usage.filled_cells_count)

    def copy_month(
        self,
        source_sheet_name: str,
        target_year: int,
        target_month: int,
        overwrite: bool = False,
    ) -> ScheduleMonthCopyResult:
        self._validate_period(target_year, target_month)
        snapshot = self.schedule_repository.load()
        if snapshot is None:
            raise ScheduleMonthValidationError("Данные графиков не загружены.")

        try:
            source_grid = snapshot.get_month_grid(source_sheet_name)
        except KeyError as exc:
            raise ScheduleMonthValidationError("Месяц-источник не найден.") from exc

        target_exists = self._has_month(snapshot, target_year, target_month)
        if target_exists and not overwrite:
            raise ScheduleMonthValidationError("Целевой месяц уже существует. Подтвердите перезапись.")

        calendar_state = self.calendar_service.load_calendar(target_year, target_month)
        target_grid = self._copy_grid_to_month(source_grid, target_year, target_month, calendar_state.holidays)
        month_name = MONTH_NAMES[target_month]
        sheet_name = f"{month_name} {target_year}"

        self.schedule_repository.save(snapshot.add_month_grid(sheet_name, month_name, target_grid))
        return ScheduleMonthCopyResult(
            source_sheet_name=source_sheet_name,
            sheet_name=sheet_name,
            title=target_grid.title,
            year=target_year,
            month=target_month,
            employee_count=len(target_grid.employees),
            overwritten=target_exists,
            calendar_source=calendar_state.source,
            calendar_warning=calendar_state.warning,
        )

    def _validate_period(self, year: int, month: int) -> None:
        if year < 2000 or year > 2100:
            raise ScheduleMonthValidationError("Выберите год от 2000 до 2100.")
        if month < 1 or month > 12:
            raise ScheduleMonthValidationError("Выберите месяц от 1 до 12.")

    def _has_month(self, snapshot: ScheduleSnapshot, year: int, month: int) -> bool:
        return any(
            int(option["year"]) == year and int(option["month"]) == month
            for option in snapshot.month_options()
        )

    def _employees_for_new_month(self, snapshot: ScheduleSnapshot, employee_source: str) -> List[str]:
        if employee_source == "last_schedule" and snapshot is not None and snapshot.month_schedules:
            latest = max(snapshot.month_schedules, key=lambda item: (int(item["year"]), int(item["month"])))
            grid = snapshot.get_month_grid(latest["sheet_name"])
            names = [row.employee_name for row in grid.employees]
            if names:
                return names

        employees = [
            employee.name
            for employee in self.employee_service.list_employees()
            if employee.status == "active"
        ]
        return employees

    def _build_empty_grid(self, year: int, month: int, employee_names: List[str], holidays: set) -> ScheduleGrid:
        days_count = calendar.monthrange(year, month)[1]
        days = []
        for day_number in range(1, days_count + 1):
            current = date(year, month, day_number)
            days.append(ScheduleDay(day_number, WEEKDAYS[current.weekday()], current))

        rows = [
            ScheduleRow(
                employee_name=name,
                hours=0,
                assignments={
                    day.day: "П" if day.date in holidays else ""
                    for day in days
                },
            )
            for name in employee_names
        ]
        return ScheduleGrid(
            title=f"{MONTH_NAMES[month]} {year}",
            year=year,
            month=month,
            days=days,
            employees=rows,
        )

    def _copy_grid_to_month(self, source_grid: ScheduleGrid, year: int, month: int, holidays: set) -> ScheduleGrid:
        days_count = calendar.monthrange(year, month)[1]
        days = []
        for day_number in range(1, days_count + 1):
            current = date(year, month, day_number)
            days.append(ScheduleDay(day_number, WEEKDAYS[current.weekday()], current))

        rows = []
        for source_row in source_grid.employees:
            assignments = {}
            for target_day in days:
                if target_day.date in holidays:
                    assignments[target_day.day] = "П"
                    continue
                assignments[target_day.day] = source_row.assignments.get(target_day.day, "")
            rows.append(
                ScheduleRow(
                    employee_name=source_row.employee_name,
                    hours=self._calculate_hours(assignments),
                    assignments=assignments,
                )
            )
        return ScheduleGrid(
            title=f"{MONTH_NAMES[month]} {year}",
            year=year,
            month=month,
            days=days,
            employees=rows,
        )

    def _clear_filled_assignments(self, sheet_name: str) -> None:
        snapshot = self.schedule_repository.load()
        if snapshot is None:
            raise ScheduleMonthValidationError("Данные графиков не загружены.")
        grid = snapshot.get_month_grid(sheet_name)
        allowed_codes = self._allowed_empty_codes()
        rows = []
        for row in grid.employees:
            assignments = {
                day: code if self._code_key(code) in allowed_codes else ""
                for day, code in row.assignments.items()
            }
            rows.append(
                ScheduleRow(
                    employee_name=row.employee_name,
                    hours=self._calculate_hours(assignments),
                    assignments=assignments,
                )
            )
        updated_snapshot = snapshot.replace_month_grid(
            sheet_name,
            ScheduleGrid(grid.title, grid.year, grid.month, grid.days, rows),
        )
        self.schedule_repository.save(updated_snapshot.clear_month_metadata(sheet_name, "autoplan"))

    def _allowed_empty_codes(self) -> set:
        result = {"", "п", "праздник", "о", "отпуск"}
        for shift in self.shift_service.list_shifts():
            text = " ".join((shift.code, shift.short_name, shift.name, shift.description)).lower()
            if "праздник" not in text and "отпуск" not in text:
                continue
            for value in (shift.code, shift.short_name, *shift.aliases):
                key = self._code_key(value)
                if key:
                    result.add(key)
        return result

    def _calculate_hours(self, assignments: dict) -> int:
        lookup = self.shift_service.lookup()
        total = 0.0
        for code in assignments.values():
            normalized = self._normalize_code(code)
            if not normalized:
                continue
            shift = lookup.get(normalized) or lookup.get(normalized.lower())
            if shift is not None:
                total += shift.hours
        return int(total)

    def _normalize_code(self, value: object) -> str:
        return " ".join(str(value or "").strip().split())

    def _code_key(self, value: object) -> str:
        return self._normalize_code(value).lower()
