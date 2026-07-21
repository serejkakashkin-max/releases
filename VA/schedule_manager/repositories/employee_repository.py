from pathlib import Path
from typing import List

from VA.schedule_manager.models.employee import Employee
from VA.schedule_manager.repositories.json_file_store import JsonFileStore
from VA.schedule_manager.config import EMPLOYEES_DATA_FILE


class EmployeeRepository:
    def __init__(
        self,
        data_file: Path = EMPLOYEES_DATA_FILE,
        *,
        directory_projection: bool = True,
    ) -> None:
        self.data_file = data_file
        self.store = JsonFileStore(data_file, "employees")
        self.directory_projection = directory_projection

    def load_all(self) -> List[Employee]:
        if not self.directory_projection:
            return self.load_all_legacy()

        from VA.schedule_manager.integrations.employee_directory_adapter import (
            apply_employee_directory_mode,
        )

        return apply_employee_directory_mode(self.load_all_legacy())

    def load_all_legacy(self) -> List[Employee]:
        data = self.store.load()
        if data is None:
            return []

        return [Employee.from_dict(item) for item in data.get("employees", [])]

    def save_all(self, employees: List[Employee]) -> None:
        if not self.directory_projection:
            self.save_all_legacy(employees)
            return

        from VA.schedule_manager.integrations.employee_directory_adapter import (
            prepare_va_records_for_save,
        )

        records = prepare_va_records_for_save(employees, self.load_all_legacy())
        self.save_all_legacy(records)

    def save_all_legacy(self, employees: List[Employee]) -> None:
        self.store.save({"employees": [employee.to_dict() for employee in employees]})
