from VA.schedule_manager.models.employee import Employee
from VA.schedule_manager.services.autoplan.rules import EmployeeRuleSet


def test_employee_rules_detect_manager_newcomer_and_mpr():
    rules = EmployeeRuleSet()

    assert rules.is_manager(Employee("Руководитель", role="manager"))
    assert rules.is_manager(Employee("Компетентный руководитель", competencies=("manager",)))
    assert rules.is_newcomer(Employee("Новичок", competencies=("newcomer",)))
    assert rules.is_mpr_coordinator(Employee("МПР", competencies=("mpr_coordinator",)))


def test_employee_rules_calculate_priorities_and_day_primary_mpr():
    rules = EmployeeRuleSet()
    mpr = Employee("МПР", competencies=("mpr_coordinator",))
    support = Employee("Сотрудник", competencies=("support",))

    assert rules.evening_mpr_priority(mpr, "ВД") == 0
    assert rules.evening_mpr_priority(support, "ВД") == 1
    assert rules.evening_mpr_priority(mpr, "ДД") == 1
    assert rules.holiday_work_manager_priority(Employee("Руководитель", role="manager")) == 1
    assert rules.holiday_work_manager_priority(support) == 0
    assert rules.is_day_primary_mpr("МПР", "ДД", {"МПР": mpr})
    assert not rules.is_day_primary_mpr("МПР", "ДР", {"МПР": mpr})
