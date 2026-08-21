from typing import Dict

from VA.schedule_manager.models.employee import Employee
from VA.schedule_manager.services.competency_service import (
    COMPETENCY_MANAGER,
    COMPETENCY_MPR_COORDINATOR,
    COMPETENCY_NEWCOMER,
)


class EmployeeRuleSet:
    def is_manager(self, employee: Employee) -> bool:
        return employee.role == "manager" or COMPETENCY_MANAGER in set(employee.competencies)

    def is_newcomer(self, employee: Employee) -> bool:
        return COMPETENCY_NEWCOMER in set(employee.competencies)

    def is_mpr_coordinator(self, employee: Employee) -> bool:
        return COMPETENCY_MPR_COORDINATOR in set(employee.competencies)

    def is_day_primary_mpr(self, employee_name: str, shift_code: str, employees: Dict[str, Employee]) -> bool:
        employee = employees.get(employee_name)
        return shift_code == "ДД" and employee is not None and self.is_mpr_coordinator(employee)

    def evening_mpr_priority(self, employee: Employee, shift_code: str) -> int:
        if shift_code in {"ВД", "ВР"} and self.is_mpr_coordinator(employee):
            return 0
        return 1

    def holiday_work_manager_priority(self, employee: Employee) -> int:
        return 1 if self.is_manager(employee) else 0
