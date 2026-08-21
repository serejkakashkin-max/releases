from datetime import date

from VA.schedule_manager.models.employee import Employee
from VA.schedule_manager.models.schedule_grid import ScheduleDay, ScheduleGrid, ScheduleRow
from VA.schedule_manager.services.autoplan.candidates import (
    CandidateGenerator,
    HolidayCandidateRequest,
    WeekdayCandidateRequest,
)
from VA.schedule_manager.services.autoplan.scoring import CandidateScorer
from VA.schedule_manager.services.competency_service import COMPETENCY_MANAGER, COMPETENCY_MPR_COORDINATOR, COMPETENCY_NEWCOMER


def _grid(names):
    return ScheduleGrid(
        "Август 2026",
        2026,
        8,
        [ScheduleDay(3, "пн", date(2026, 8, 3))],
        [ScheduleRow(name, 0, {3: ""}) for name in names],
    )


def _generator():
    return CandidateGenerator(
        CandidateScorer(),
        is_manager=lambda employee: employee.role == "manager" or COMPETENCY_MANAGER in set(employee.competencies),
        is_newcomer=lambda employee: COMPETENCY_NEWCOMER in set(employee.competencies),
        is_mpr_coordinator=lambda employee: COMPETENCY_MPR_COORDINATOR in set(employee.competencies),
        evening_priority=lambda employee, shift_code: 0 if shift_code in {"ВД", "ВР"} and COMPETENCY_MPR_COORDINATOR in set(employee.competencies) else 1,
        holiday_work_manager_priority=lambda employee: 1 if employee.role == "manager" else 0,
        evening_shift_codes={"ВД", "ВР"},
        khabarovsk_shifts={"ХД", "ХР"},
        holiday_work_code="ВХ",
    )


def test_weekday_candidates_apply_location_manager_and_newcomer_filters():
    employees = {
        "Москва": Employee("Москва", status="active", location="moscow", competencies=("support",)),
        "Хабаровск": Employee("Хабаровск", status="active", location="khabarovsk", competencies=("support",)),
        "Руководитель": Employee("Руководитель", status="active", location="moscow", role="manager"),
        "Новичок": Employee("Новичок", status="active", location="moscow", competencies=("support", "newcomer")),
    }
    request = WeekdayCandidateRequest(
        grid=_grid(employees),
        employees=employees,
        counters={name: 0 for name in employees},
        unavailable=set(),
        week_assigned=set(),
        location="moscow",
        allow_manager=False,
        shift_code="ДД",
        monthly_shift_usage={},
        previous_week_evening=set(),
        current_week_evening=set(),
        current_week_day_primary_mpr=set(),
        current_month_duty_blocks={},
        newcomer_shift_history={},
        previous_week_shift_workers={},
    )

    assert _generator().weekday_candidates(request) == ["Москва"]


def test_weekday_candidates_keep_second_mpr_out_of_evening_pair():
    employees = {
        "МПР 1": Employee("МПР 1", status="active", location="moscow", competencies=("support", "mpr_coordinator")),
        "МПР 2": Employee("МПР 2", status="active", location="moscow", competencies=("support", "mpr_coordinator")),
        "Сотрудник": Employee("Сотрудник", status="active", location="moscow", competencies=("support",)),
    }
    request = WeekdayCandidateRequest(
        grid=_grid(employees),
        employees=employees,
        counters={name: 0 for name in employees},
        unavailable=set(),
        week_assigned=set(),
        location="moscow",
        allow_manager=False,
        shift_code="ВР",
        monthly_shift_usage={},
        previous_week_evening=set(),
        current_week_evening={"МПР 1"},
        current_week_day_primary_mpr=set(),
        current_month_duty_blocks={},
        newcomer_shift_history={},
        previous_week_shift_workers={},
    )

    assert _generator().weekday_candidates(request) == ["Сотрудник"]


def test_holiday_candidates_require_overtime_ready_and_known_newcomer_history():
    employees = {
        "Готов": Employee("Готов", status="active", location="moscow", overtime_ready=True),
        "Не готов": Employee("Не готов", status="active", location="moscow", overtime_ready=False),
        "Новичок": Employee("Новичок", status="active", location="moscow", competencies=("newcomer",), overtime_ready=True),
        "Хабаровск": Employee("Хабаровск", status="active", location="khabarovsk", overtime_ready=True),
    }
    request = HolidayCandidateRequest(
        grid=_grid(employees),
        employees=employees,
        holiday_work_counters={name: 0 for name in employees},
        current_month_holiday_work_counts={name: 0 for name in employees},
        unavailable=set(),
        newcomer_shift_history={"Новичок": {"ВХ"}},
    )

    assert _generator().holiday_candidates(request) == ["Готов", "Новичок"]
