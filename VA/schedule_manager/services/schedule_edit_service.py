from dataclasses import dataclass
from typing import List, Optional

from VA.schedule_manager.models.schedule_grid import ScheduleGrid, ScheduleRow
from VA.schedule_manager.services.employee_service import EmployeeService
from VA.schedule_manager.services.schedule_service import ScheduleService
from VA.schedule_manager.services.schedule_validator import build_validation_rules, validate_schedule
from VA.schedule_manager.services.shift_service import ShiftService


class ScheduleEditValidationError(Exception):
    pass


@dataclass(frozen=True)
class ScheduleCellUpdate:
    employee_name: str
    day: int
    shift_code: str
    display_code: str
    shift_name: str
    color: str
    text_color: str
    hours: float
    title: str
    violation_count: int
    violations: List[dict]
    autoplan_artifact_cleared: bool = False


@dataclass(frozen=True)
class ScheduleEmployeeAdd:
    employee_name: str
    hours: int
    assignments: dict
    title: str
    violation_count: int
    violations: List[dict]
    autoplan_artifact_cleared: bool = False


@dataclass(frozen=True)
class ScheduleEmployeeDelete:
    employee_name: str
    title: str
    violation_count: int
    violations: List[dict]
    autoplan_artifact_cleared: bool = False


@dataclass(frozen=True)
class ScheduleBulkFill:
    cells: List[dict]
    rows: List[dict]
    title: str
    violation_count: int
    violations: List[dict]
    applied_to_full_days: bool
    autoplan_artifact_cleared: bool = False


class ScheduleEditService:
    def __init__(
        self,
        schedule_service: ScheduleService,
        shift_service: ShiftService,
        repository: Optional[object] = None,
        employee_service: Optional[EmployeeService] = None,
    ) -> None:
        self.schedule_service = schedule_service
        self.shift_service = shift_service
        self.employee_service = employee_service

    def apply_edits(self, grid: ScheduleGrid, workbook_id: str, sheet_name: str) -> ScheduleGrid:
        return self._recalculate_grid_hours(grid)

    def update_cell(self, sheet_name: str, employee_name: str, day: int, shift_code: str) -> ScheduleCellUpdate:
        snapshot = self.schedule_service.get_current()
        if snapshot is None:
            raise ScheduleEditValidationError("Сначала загрузите Excel-файл.")

        shift_code = self._normalize_shift_code(shift_code)
        if day <= 0:
            raise ScheduleEditValidationError("Некорректный день месяца.")

        try:
            base_grid = snapshot.get_month_grid(sheet_name)
        except KeyError as exc:
            raise ScheduleEditValidationError("Лист графика не найден.") from exc

        if day not in {schedule_day.day for schedule_day in base_grid.days}:
            raise ScheduleEditValidationError("В выбранном месяце нет такого дня.")
        if not any(row.employee_name == employee_name for row in base_grid.employees):
            raise ScheduleEditValidationError("Сотрудник не найден в графике.")

        if self._is_holiday_shift(shift_code):
            grid = self._fill_days(base_grid, {day}, shift_code)
        else:
            grid = self._update_grid_cell(base_grid, employee_name, day, shift_code)
        self.schedule_service.save_month_grid(sheet_name, grid)
        autoplan_artifact_cleared = self.schedule_service.clear_month_metadata(sheet_name, "autoplan")
        row = next(item for item in grid.employees if item.employee_name == employee_name)
        shift = self.shift_service.lookup().get(shift_code) or self.shift_service.lookup().get(shift_code.lower())
        violations = validate_schedule(grid, build_validation_rules(self.shift_service.list_shifts()))

        return ScheduleCellUpdate(
            employee_name=employee_name,
            day=day,
            shift_code=shift_code,
            display_code=shift.display_code if shift else shift_code,
            shift_name=shift.name if shift else "",
            color=shift.color if shift else "",
            text_color=shift.text_color if shift else "",
            hours=row.hours or 0,
            title=grid.title,
            violation_count=len(violations),
            violations=[
                {
                    "day": violation.day,
                    "shift": violation.shift,
                    "employee_name": violation.employee_name,
                    "message": violation.message,
                }
                for violation in violations[:8]
            ],
            autoplan_artifact_cleared=autoplan_artifact_cleared,
        )

    def add_employee(self, sheet_name: str, employee_name: str, fill_mode: str = "empty") -> ScheduleEmployeeAdd:
        snapshot = self.schedule_service.get_current()
        if snapshot is None:
            raise ScheduleEditValidationError("Сначала загрузите Excel-файл.")

        employee_name = " ".join(str(employee_name or "").strip().split())
        if not employee_name:
            raise ScheduleEditValidationError("Выберите сотрудника.")
        if self.employee_service is not None:
            active_names = {employee.name for employee in self.employee_service.list_employees() if employee.status == "active"}
            if employee_name not in active_names:
                raise ScheduleEditValidationError("Сотрудник не найден среди активных.")
        if fill_mode not in {"empty", "workdays_8"}:
            raise ScheduleEditValidationError("Некорректный способ заполнения строки.")

        try:
            grid = snapshot.get_month_grid(sheet_name)
        except KeyError as exc:
            raise ScheduleEditValidationError("Лист графика не найден.") from exc
        if any(row.employee_name == employee_name for row in grid.employees):
            raise ScheduleEditValidationError("Сотрудник уже есть в текущем графике.")

        assignments = self._default_assignments(grid, fill_mode)
        row = ScheduleRow(
            employee_name=employee_name,
            hours=self._calculate_hours(assignments),
            assignments=assignments,
        )
        updated_grid = ScheduleGrid(
            title=grid.title,
            year=grid.year,
            month=grid.month,
            days=grid.days,
            employees=[*grid.employees, row],
        )
        self.schedule_service.save_month_grid(sheet_name, updated_grid)
        autoplan_artifact_cleared = self.schedule_service.clear_month_metadata(sheet_name, "autoplan")
        violations = validate_schedule(updated_grid, build_validation_rules(self.shift_service.list_shifts()))

        return ScheduleEmployeeAdd(
            employee_name=employee_name,
            hours=int(row.hours or 0),
            assignments=row.assignments,
            title=updated_grid.title,
            violation_count=len(violations),
            violations=[
                {
                    "day": violation.day,
                    "shift": violation.shift,
                    "employee_name": violation.employee_name,
                    "message": violation.message,
                }
                for violation in violations[:8]
            ],
            autoplan_artifact_cleared=autoplan_artifact_cleared,
        )

    def delete_employee(self, sheet_name: str, employee_name: str) -> ScheduleEmployeeDelete:
        snapshot = self.schedule_service.get_current()
        if snapshot is None:
            raise ScheduleEditValidationError("Сначала загрузите Excel-файл.")

        employee_name = " ".join(str(employee_name or "").strip().split())
        if not employee_name:
            raise ScheduleEditValidationError("Выберите сотрудника.")

        try:
            grid = snapshot.get_month_grid(sheet_name)
        except KeyError as exc:
            raise ScheduleEditValidationError("Лист графика не найден.") from exc
        if not any(row.employee_name == employee_name for row in grid.employees):
            raise ScheduleEditValidationError("Сотрудник не найден в текущем графике.")

        updated_grid = ScheduleGrid(
            title=grid.title,
            year=grid.year,
            month=grid.month,
            days=grid.days,
            employees=[row for row in grid.employees if row.employee_name != employee_name],
        )
        self.schedule_service.save_month_grid(sheet_name, updated_grid)
        autoplan_artifact_cleared = self.schedule_service.clear_month_metadata(sheet_name, "autoplan")
        violations = validate_schedule(updated_grid, build_validation_rules(self.shift_service.list_shifts()))

        return ScheduleEmployeeDelete(
            employee_name=employee_name,
            title=updated_grid.title,
            violation_count=len(violations),
            violations=[
                {
                    "day": violation.day,
                    "shift": violation.shift,
                    "employee_name": violation.employee_name,
                    "message": violation.message,
                }
                for violation in violations[:8]
            ],
            autoplan_artifact_cleared=autoplan_artifact_cleared,
        )

    def bulk_fill(self, sheet_name: str, cells: List[dict], shift_code: str) -> ScheduleBulkFill:
        snapshot = self.schedule_service.get_current()
        if snapshot is None:
            raise ScheduleEditValidationError("Сначала загрузите Excel-файл.")

        shift_code = self._normalize_shift_code(shift_code)
        if not cells:
            raise ScheduleEditValidationError("Выберите ячейки для заполнения.")

        try:
            grid = snapshot.get_month_grid(sheet_name)
        except KeyError as exc:
            raise ScheduleEditValidationError("Лист графика не найден.") from exc

        day_set = {day.day for day in grid.days}
        employee_set = {row.employee_name for row in grid.employees}
        normalized_cells = []
        for cell in cells:
            try:
                day = int(cell.get("day", 0))
            except (TypeError, ValueError) as exc:
                raise ScheduleEditValidationError("Некорректный день месяца.") from exc
            employee_name = " ".join(str(cell.get("employee_name", "")).strip().split())
            if day not in day_set:
                raise ScheduleEditValidationError("В выбранном месяце нет такого дня.")
            if employee_name not in employee_set:
                raise ScheduleEditValidationError("Сотрудник не найден в графике.")
            normalized_cells.append({"employee_name": employee_name, "day": day})

        applied_to_full_days = self._is_holiday_shift(shift_code)
        if applied_to_full_days:
            target_days = {cell["day"] for cell in normalized_cells}
            updated_grid = self._fill_days(grid, target_days, shift_code)
        else:
            updated_grid = self._fill_cells(grid, normalized_cells, shift_code)

        self.schedule_service.save_month_grid(sheet_name, updated_grid)
        autoplan_artifact_cleared = self.schedule_service.clear_month_metadata(sheet_name, "autoplan")
        violations = validate_schedule(updated_grid, build_validation_rules(self.shift_service.list_shifts()))
        touched_rows = {
            row.employee_name
            for row in updated_grid.employees
            if applied_to_full_days or any(cell["employee_name"] == row.employee_name for cell in normalized_cells)
        }
        touched_days = {cell["day"] for cell in normalized_cells}
        if applied_to_full_days:
            touched_cells = [
                {"employee_name": row.employee_name, "day": day, "shift_code": row.assignments.get(day, "")}
                for row in updated_grid.employees
                for day in touched_days
            ]
        else:
            touched_cells = [
                {"employee_name": cell["employee_name"], "day": cell["day"], "shift_code": shift_code}
                for cell in normalized_cells
            ]

        return ScheduleBulkFill(
            cells=[self._cell_payload(cell) for cell in touched_cells],
            rows=[
                {"employee_name": row.employee_name, "hours": row.hours or 0}
                for row in updated_grid.employees
                if row.employee_name in touched_rows
            ],
            title=updated_grid.title,
            violation_count=len(violations),
            violations=[
                {
                    "day": violation.day,
                    "shift": violation.shift,
                    "employee_name": violation.employee_name,
                    "message": violation.message,
                }
                for violation in violations[:8]
            ],
            applied_to_full_days=applied_to_full_days,
            autoplan_artifact_cleared=autoplan_artifact_cleared,
        )

    def _normalize_shift_code(self, shift_code: object) -> str:
        code = " ".join(str(shift_code or "").strip().split())
        if not code:
            return ""
        shift = self.shift_service.lookup().get(code) or self.shift_service.lookup().get(code.lower())
        if shift is None:
            raise ScheduleEditValidationError("Неизвестная смена.")
        return shift.code

    def _update_grid_cell(self, grid: ScheduleGrid, employee_name: str, day: int, shift_code: str) -> ScheduleGrid:
        rows = []
        for row in grid.employees:
            assignments = dict(row.assignments)
            if row.employee_name == employee_name:
                assignments[day] = shift_code
            rows.append(
                ScheduleRow(
                    employee_name=row.employee_name,
                    hours=self._calculate_hours(assignments),
                    assignments=assignments,
                )
            )

        return ScheduleGrid(
            title=grid.title,
            year=grid.year,
            month=grid.month,
            days=grid.days,
            employees=rows,
        )

    def _fill_cells(self, grid: ScheduleGrid, cells: List[dict], shift_code: str) -> ScheduleGrid:
        selected = {(cell["employee_name"], cell["day"]) for cell in cells}
        rows = []
        for row in grid.employees:
            assignments = dict(row.assignments)
            for day in grid.days:
                if (row.employee_name, day.day) in selected:
                    assignments[day.day] = shift_code
            rows.append(ScheduleRow(row.employee_name, self._calculate_hours(assignments), assignments))
        return ScheduleGrid(grid.title, grid.year, grid.month, grid.days, rows)

    def _fill_days(self, grid: ScheduleGrid, days: set, shift_code: str) -> ScheduleGrid:
        rows = []
        for row in grid.employees:
            assignments = dict(row.assignments)
            for day in days:
                assignments[day] = shift_code
            rows.append(ScheduleRow(row.employee_name, self._calculate_hours(assignments), assignments))
        return ScheduleGrid(grid.title, grid.year, grid.month, grid.days, rows)

    def _is_holiday_shift(self, shift_code: str) -> bool:
        holiday_codes = {self._normalize_code(code).lower() for code in self._holiday_codes()}
        return self._normalize_code(shift_code).lower() in holiday_codes

    def _cell_payload(self, cell: dict) -> dict:
        code = cell["shift_code"]
        shift = self.shift_service.lookup().get(code) or self.shift_service.lookup().get(str(code).lower())
        return {
            "employee_name": cell["employee_name"],
            "day": cell["day"],
            "shift_code": code,
            "display_code": shift.display_code if shift else code,
            "shift_name": shift.name if shift else "",
            "color": shift.color if shift else "",
            "text_color": shift.text_color if shift else "",
        }

    def _default_assignments(self, grid: ScheduleGrid, fill_mode: str) -> dict:
        if fill_mode == "empty":
            return {day.day: "" for day in grid.days}

        return {
            day.day: "" if self._is_non_working_day(grid, day.day, day.weekday) else "8"
            for day in grid.days
        }

    def _is_non_working_day(self, grid: ScheduleGrid, day: int, weekday: str) -> bool:
        if weekday in {"сб", "вс"}:
            return True
        holiday_codes = self._holiday_codes()
        return any(
            self._normalize_code(row.assignments.get(day, "")) in holiday_codes
            for row in grid.employees
        )

    def _holiday_codes(self) -> set:
        lookup = self.shift_service.lookup()
        shift = lookup.get("Праздник") or lookup.get("праздник") or lookup.get("П") or lookup.get("п")
        if shift is None:
            return {"Праздник", "П"}
        return {
            code
            for code in (
                self._normalize_code(shift.code),
                self._normalize_code(shift.short_name),
                *(self._normalize_code(alias) for alias in shift.aliases),
            )
            if code
        }

    def _recalculate_grid_hours(self, grid: ScheduleGrid) -> ScheduleGrid:
        return ScheduleGrid(
            title=grid.title,
            year=grid.year,
            month=grid.month,
            days=grid.days,
            employees=[
                ScheduleRow(
                    employee_name=row.employee_name,
                    hours=self._calculate_hours(row.assignments),
                    assignments=dict(row.assignments),
                )
                for row in grid.employees
            ],
        )

    def _calculate_hours(self, assignments: dict) -> float:
        lookup = self.shift_service.lookup()
        total = 0.0
        for code in assignments.values():
            normalized = " ".join(str(code or "").strip().split())
            if not normalized:
                continue
            shift = lookup.get(normalized) or lookup.get(normalized.lower())
            if shift is not None:
                total += shift.hours
        return total

    def _normalize_code(self, value: object) -> str:
        return " ".join(str(value or "").strip().split())
