from typing import List

from VA.schedule_manager.models.employee import Employee


class ManagedEmployeeRepository:
    """Read-only current VA employees projected from Employee Directory."""

    def load_all(self) -> List[Employee]:
        from VA.schedule_manager.integrations.employee_directory_adapter import (
            get_managed_va_employees,
            managed_to_employee,
        )

        return [managed_to_employee(item) for item in get_managed_va_employees()]

    def save_all(self, employees: List[Employee]) -> None:
        raise RuntimeError("Use UUID-based VA employee settings API.")
