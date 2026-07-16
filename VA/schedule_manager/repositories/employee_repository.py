from pathlib import Path
from typing import List

from VA.schedule_manager.models.employee import Employee
from VA.schedule_manager.repositories.json_file_store import JsonFileStore
from VA.schedule_manager.config import EMPLOYEES_DATA_FILE


class EmployeeRepository:
    def __init__(self, data_file: Path = EMPLOYEES_DATA_FILE) -> None:
        self.data_file = data_file
        self.store = JsonFileStore(data_file, "employees")

    def load_all(self) -> List[Employee]:
        data = self.store.load()
        if data is None:
            return []

        return [Employee.from_dict(item) for item in data.get("employees", [])]

    def save_all(self, employees: List[Employee]) -> None:
        self.store.save({"employees": [employee.to_dict() for employee in employees]})
