from datetime import date

import pytest

from VA.schedule_manager.models.employee import Employee
from VA.schedule_manager.models.schedule_grid import ScheduleDay, ScheduleGrid, ScheduleRow
from VA.schedule_manager.models.schedule_snapshot import ScheduleSnapshot
from VA.schedule_manager.services.autoplan.candidates import CandidateGenerator, WeekdayCandidateRequest
from VA.schedule_manager.services.autoplan.scoring import CandidateScorer
from VA.schedule_manager.services.autoplan_contract import AUTOPLAN_CONTRACT
from VA.schedule_manager.services.newcomer_history import collect_newcomer_shift_codes
from VA.schedule_manager.services.schedule_validator import build_validation_rules, validate_schedule


def _grid(code=""):
    return ScheduleGrid(
        "Сентябрь 2026", 2026, 9,
        [ScheduleDay(1, "вт", date(2026, 9, 1))],
        [ScheduleRow("Новичок", 0, {1: code}), ScheduleRow("Сотрудник", 0, {1: "8"})],
    )


def _generator():
    return CandidateGenerator(
        CandidateScorer(),
        is_manager=lambda employee: False,
        is_newcomer=lambda employee: "newcomer" in employee.competencies,
        is_mpr_coordinator=lambda employee: False,
        evening_priority=lambda employee, shift_code: 0,
        holiday_work_manager_priority=lambda employee: 0,
        evening_shift_codes={"ВД", "ВР"},
        khabarovsk_shifts={"ХД", "ХР"},
        holiday_work_code="ВХ",
        newcomer_trainee_shift_codes={"ДР", "ВР"},
    )


def _request(code):
    newcomer = Employee("Новичок", status="active", location="moscow", competencies=("newcomer",))
    employee = Employee("Сотрудник", status="active", location="moscow")
    return WeekdayCandidateRequest(
        grid=_grid(), employees={newcomer.name: newcomer, employee.name: employee}, counters={},
        unavailable=set(), week_assigned=set(), location="moscow", allow_manager=False,
        shift_code=code, monthly_shift_usage={}, previous_week_evening=set(),
        current_week_evening=set(), current_week_day_primary_mpr=set(), current_month_duty_blocks={},
        newcomer_shift_history={}, previous_week_shift_workers={},
    )


@pytest.mark.parametrize("code", ["ДР", "ВР"])
def test_newcomer_without_history_can_work_trainee_shift(code):
    assert "Новичок" in _generator().weekday_candidates(_request(code))


@pytest.mark.parametrize("code", ["ДД", "ВД", "ХД", "ХР"])
def test_newcomer_without_history_cannot_work_other_weekday_shift(code):
    assert "Новичок" not in _generator().weekday_candidates(_request(code))


@pytest.mark.parametrize("code", ["ДД", "ХД", "ВХ"])
def test_current_month_manual_shift_is_not_collected_as_newcomer_history(code):
    grid = _grid(code)
    snapshot = ScheduleSnapshot(
        employees=[], original_filename="", stored_filename="", uploaded_at="",
        month_schedules=[{"year": 2026, "month": 9, "sheet_name": grid.title, "grid": {
            "title": grid.title, "year": 2026, "month": 9,
            "days": [{"day": 1, "weekday": "вт", "date": "2026-09-01"}],
            "employees": [{"employee_name": "Новичок", "hours": 0, "assignments": {"1": ""}},
                          {"employee_name": "Сотрудник", "hours": 0, "assignments": {"1": "8"}}],
        }}],
    )
    history = collect_newcomer_shift_codes(snapshot, grid, lambda code: code, set(AUTOPLAN_CONTRACT.load_shift_codes))
    assert history == {}
    rules = build_validation_rules(
        employees=[Employee("Новичок", status="active", location="moscow", competencies=("newcomer",)),
                   Employee("Сотрудник", status="active", location="moscow")],
        newcomer_allowed_shift_codes=history,
        newcomer_trainee_shift_codes=set(AUTOPLAN_CONTRACT.newcomer_trainee_shift_codes),
    )
    assert any(
        violation.message == "Новичка нельзя назначать в дежурные смены и ВХ."
        for violation in validate_schedule(grid, rules)
    )


def test_previous_saved_month_allows_code_in_current_month():
    grid = _grid("ДД")
    previous_grid = _grid("ДД")
    previous_grid = ScheduleGrid(
        "Август 2026", 2026, 8, previous_grid.days, previous_grid.employees
    )
    snapshot = ScheduleSnapshot(
        employees=[], original_filename="", stored_filename="", uploaded_at="",
        month_schedules=[
            {"year": 2026, "month": 8, "sheet_name": previous_grid.title, "grid": {
                "title": previous_grid.title, "year": 2026, "month": 8,
                "days": [{"day": 1, "weekday": "вт", "date": "2026-08-01"}],
                "employees": [{"employee_name": "Новичок", "hours": 0, "assignments": {"1": "ДД"}},
                              {"employee_name": "Сотрудник", "hours": 0, "assignments": {"1": "8"}}],
            }},
            {"year": 2026, "month": 9, "sheet_name": grid.title, "grid": {
                "title": grid.title, "year": 2026, "month": 9,
                "days": [{"day": 1, "weekday": "вт", "date": "2026-09-01"}],
                "employees": [{"employee_name": "Новичок", "hours": 0, "assignments": {"1": ""}},
                              {"employee_name": "Сотрудник", "hours": 0, "assignments": {"1": "8"}}],
            }},
        ],
    )
    history = collect_newcomer_shift_codes(snapshot, grid, lambda value: value, set(AUTOPLAN_CONTRACT.load_shift_codes))
    assert history == {"Новичок": {"ДД"}}


def test_validator_allows_trainee_and_historical_codes_but_rejects_unknown_code():
    newcomer = Employee("Новичок", status="active", location="moscow", competencies=("newcomer",))
    employee = Employee("Сотрудник", status="active", location="moscow")
    rules = build_validation_rules(
        employees=[newcomer, employee],
        newcomer_allowed_shift_codes={"Новичок": {"ДД"}},
        newcomer_trainee_shift_codes={"ДР", "ВР"},
    )
    assert not any(v.message == "Новичка нельзя назначать в дежурные смены и ВХ." for v in validate_schedule(_grid("ДР"), rules))
    assert not any(v.message == "Новичка нельзя назначать в дежурные смены и ВХ." for v in validate_schedule(_grid("ДД"), rules))
    assert any(v.message == "Новичка нельзя назначать в дежурные смены и ВХ." for v in validate_schedule(_grid("ХД"), rules))
