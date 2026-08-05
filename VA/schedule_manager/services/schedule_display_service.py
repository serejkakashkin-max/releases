from typing import Optional

from VA.schedule_manager.services.schedule_service import ScheduleService
from VA.schedule_manager.services.schedule_validator import build_validation_rules, validate_schedule
from VA.schedule_manager.services.shift_service import ShiftService
from VA.schedule_manager.services.autoplan_hint_service import (
    build_autoplan_hints,
    normalize_autoplan_artifact,
)
from VA.schedule_manager.repositories.managed_employee_repository import ManagedEmployeeRepository
from VA.schedule_manager.services.employee_service import EmployeeService


class ScheduleDisplayService:
    def __init__(self, schedule_service: ScheduleService, shift_service: ShiftService) -> None:
        self.schedule_service = schedule_service
        self.shift_service = shift_service
        # 🔥 Инициализируем сервис сотрудников для получения порядка
        self.employee_repo = ManagedEmployeeRepository()
        self.employee_service = EmployeeService(self.employee_repo)

    def shift_lookup(self) -> dict:
        return self.shift_service.lookup()

    def shift_options(self) -> list:
        return self.shift_service.list_shifts()

    def shift_options_payload(self) -> list:
        return [
            {
                "code": shift.code,
                "display_code": shift.display_code,
                "name": shift.name,
                "color": shift.color,
                "text_color": shift.text_color,
                "hours": shift.hours,
            }
            for shift in self.shift_options()
        ]

    def build_context(
        self,
        selected_year: Optional[int] = None,
        selected_month: Optional[int] = None,
    ) -> dict:
        snapshot = self.schedule_service.get_current()
        if snapshot is None:
            return self._empty_context("Нет данных")

        options = snapshot.month_options()
        if not options:
            return {
                **self._empty_context("Данные АС"),
                "schedule_error": "В загруженных данных не найдены месячные графики.",
            }

        years = sorted({option["year"] for option in options}, reverse=True)
        if selected_year is None or selected_month is None:
            selected_year = options[0]["year"]
            selected_month = options[0]["month"]

        months = [
            option
            for option in sorted(options, key=lambda item: item["month"])
            if option["year"] == selected_year
        ]
        selected_option = next(
            (
                option
                for option in options
                if option["year"] == selected_year and option["month"] == selected_month
            ),
            None,
        )
        if selected_option is None and months:
            selected_option = months[-1]
            selected_month = selected_option["month"]

        schedule_grid = None
        schedule_violations = []
        schedule_error = None
        if selected_option is not None:
            try:
                schedule_grid = snapshot.get_month_grid(selected_option["sheet_name"])

                # 🔥 СОРТИРУЕМ ПО ПОРЯДКУ ИЗ СПРАВОЧНИКА (ТОЛЬКО ДЛЯ ОТОБРАЖЕНИЯ)
                schedule_grid = self._sort_by_directory_order(schedule_grid)

                raw_autoplan_artifact = snapshot.get_month_metadata(
                    selected_option["sheet_name"],
                    "autoplan",
                )
                autoplan_artifact = normalize_autoplan_artifact(
                    raw_autoplan_artifact,
                    year=selected_year,
                    month=selected_month,
                    employee_names=[
                        row.employee_name for row in schedule_grid.employees
                    ],
                    valid_days=[day.day for day in schedule_grid.days],
                )
                schedule_violations = validate_schedule(
                    schedule_grid,
                    build_validation_rules(self.shift_service.list_shifts()),
                    self._previous_grid(snapshot, selected_year, selected_month),
                )
            except KeyError as exc:
                schedule_error = str(exc)
                autoplan_artifact = {}
        else:
            autoplan_artifact = {}

        return {
            "schedule_grid": schedule_grid,
            "schedule_violations": schedule_violations,
            "schedule_days_payload": [
                {
                    "day": day.day,
                    "weekday": day.weekday,
                }
                for day in schedule_grid.days
            ] if schedule_grid is not None else [],
            "schedule_source": "Данные АС",
            "schedule_error": schedule_error,
            "workbook_path": None,
            "month_sheet_options": options,
            "year_options": years,
            "month_options": months,
            "selected_year": selected_year,
            "selected_month": selected_month,
            "selected_sheet_name": selected_option["sheet_name"] if selected_option is not None else None,
            "autoplan_artifact": autoplan_artifact,
            "autoplan_hints": build_autoplan_hints(autoplan_artifact),
        }

    def _empty_context(self, schedule_source: str) -> dict:
        return {
            "schedule_grid": None,
            "schedule_violations": [],
            "schedule_days_payload": [],
            "schedule_source": schedule_source,
            "schedule_error": None,
            "workbook_path": None,
            "month_sheet_options": [],
            "year_options": [],
            "month_options": [],
            "selected_year": None,
            "selected_month": None,
            "selected_sheet_name": None,
            "autoplan_artifact": {},
            "autoplan_hints": {},
        }

    def _previous_grid(self, snapshot, year: int, month: int):
        target_index = int(year) * 12 + int(month)
        for option in snapshot.month_options():
            try:
                option_index = int(option["year"]) * 12 + int(option["month"])
            except (KeyError, TypeError, ValueError):
                continue
            if option_index != target_index - 1:
                continue
            try:
                return snapshot.get_month_grid(option["sheet_name"])
            except KeyError:
                return None
        return None

    def _sort_by_directory_order(self, grid):
        """Сортирует сотрудников по порядку из справочника."""
        if grid is None or not grid.employees:
            return grid

        try:
            # Получаем активных сотрудников в порядке из справочника
            directory_employees = [
                emp.name
                for emp in self.employee_service.list_employees()
                if emp.status == "active"
            ]

            if not directory_employees:
                return grid

            # Сортируем по порядку из справочника
            order_index = {name: idx for idx, name in enumerate(directory_employees)}
            sorted_employees = sorted(
                grid.employees,
                key=lambda row: order_index.get(row.employee_name, len(order_index) + 1)
            )

            # Возвращаем новый грид с отсортированными сотрудниками
            from VA.schedule_manager.models.schedule_grid import ScheduleGrid
            return ScheduleGrid(
                title=grid.title,
                year=grid.year,
                month=grid.month,
                days=grid.days,
                employees=sorted_employees,
            )
        except Exception:
            # Если не удалось получить порядок — возвращаем как есть
            return grid