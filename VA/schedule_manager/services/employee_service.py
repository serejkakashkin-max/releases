from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from VA.schedule_manager.models.employee import Employee
from VA.schedule_manager.repositories.managed_employee_repository import ManagedEmployeeRepository
from VA.schedule_manager.services.schedule_service import ScheduleService


EMPLOYEE_STATUSES = {
    "active": "Активен",
    "long_leave": "Длительный отпуск",
}

EMPLOYEE_LOCATIONS = {
    "moscow": "Москва",
    "khabarovsk": "Хабаровск",
}


class EmployeeValidationError(Exception):
    pass


class EmployeeInUseError(Exception):
    pass


@dataclass(frozen=True)
class ScheduleUsage:
    sheet_name: str
    title: str
    assigned_days: int


class EmployeeService:
    """Read-only current employee facade backed by Employee Directory."""

    def __init__(
        self,
        repository: ManagedEmployeeRepository,
        workbook_path: Optional[Path] = None,
        schedule_service: Optional[ScheduleService] = None,
        competency_service=None,
    ) -> None:
        self.repository = repository
        self.schedule_service = schedule_service

    def list_employees(self) -> List[Employee]:
        return list(self.repository.load_all())

    def active_count(self) -> int:
        return sum(
            1
            for employee in self.list_employees()
            if employee.status == "active"
        )

    def is_used_in_schedules(self, name: str) -> bool:
        return bool(self.find_schedule_usage(name))

    def find_schedule_usage(self, name: str) -> List[ScheduleUsage]:
        if self.schedule_service is None:
            return []
        snapshot = self.schedule_service.get_current()
        if snapshot is None:
            return []
        normalized_name = self._normalize_name(name)
        usage = []
        for option in snapshot.month_options():
            grid = snapshot.get_month_grid(option["sheet_name"])
            for row in grid.employees:
                if self._normalize_name(row.employee_name) != normalized_name:
                    continue
                assigned_days = sum(
                    1
                    for code in row.assignments.values()
                    if self._normalize_name(code)
                )
                usage.append(
                    ScheduleUsage(
                        option["sheet_name"],
                        option["label"],
                        assigned_days,
                    )
                )
                break
        return usage

    @staticmethod
    def _normalize_name(value: object) -> str:
        return " ".join(str(value or "").strip().split())
