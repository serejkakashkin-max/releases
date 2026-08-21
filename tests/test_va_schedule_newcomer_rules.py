from datetime import date

from VA.schedule_manager.models.employee import Employee
from VA.schedule_manager.models.schedule_grid import ScheduleDay, ScheduleGrid, ScheduleRow
from VA.schedule_manager.models.schedule_snapshot import ScheduleSnapshot, grid_to_dict
from VA.schedule_manager.repositories.schedule_repository import ScheduleRepository
from VA.schedule_manager.repositories.shift_repository import ShiftRepository
from VA.schedule_manager.services.schedule_autoplan_service import ScheduleAutoplanService
from VA.schedule_manager.services.schedule_validator import build_validation_rules, validate_schedule
from VA.schedule_manager.services.shift_service import ShiftService


class _EmployeeRepository:
    def __init__(self, employees):
        self._employees = employees

    def load_all(self):
        return list(self._employees)


def _service(tmp_path, employees):
    return ScheduleAutoplanService(
        ScheduleRepository(tmp_path / "schedule.json"),
        _EmployeeRepository(employees),
        ShiftService(ShiftRepository(tmp_path / "shifts.json")),
    )


def _grid(employees):
    days = [ScheduleDay(1, "пн", date(2026, 8, 3))]
    return ScheduleGrid(
        "Август 2026",
        2026,
        8,
        days,
        [ScheduleRow(employee.name, 0, {1: ""}) for employee in employees],
    )


def test_autoplan_uses_newcomer_only_for_shift_seen_in_history(tmp_path):
    employees = [
        Employee("Новичок Н.Н.", status="active", location="moscow", competencies=("support", "newcomer")),
        Employee("Сотрудник С.С.", status="active", location="moscow", competencies=("support",)),
    ]
    service = _service(tmp_path, employees)
    grid = _grid(employees)
    employee_map = {employee.name: employee for employee in employees}

    without_history = service._employee_candidates(
        grid,
        employee_map,
        counters={employee.name: 0 for employee in employees},
        unavailable=set(),
        week_assigned=set(),
        location="moscow",
        allow_manager=False,
        shift_code="ДД",
        newcomer_shift_history={},
    )
    with_history = service._employee_candidates(
        grid,
        employee_map,
        counters={employee.name: 0 for employee in employees},
        unavailable=set(),
        week_assigned=set(),
        location="moscow",
        allow_manager=False,
        shift_code="ДД",
        newcomer_shift_history={"Новичок Н.Н.": {"ДД"}},
    )

    assert "Новичок Н.Н." not in without_history
    assert "Новичок Н.Н." in with_history


def test_autoplan_uses_newcomer_for_holiday_work_only_when_seen_in_history(tmp_path):
    employees = [
        Employee(
            "Новичок Н.Н.",
            status="active",
            location="moscow",
            competencies=("support", "newcomer"),
            overtime_ready=True,
        ),
    ]
    service = _service(tmp_path, employees)
    grid = _grid(employees)
    employee_map = {employee.name: employee for employee in employees}

    without_history = service._holiday_candidates(
        grid,
        employee_map,
        holiday_work_counters={},
        current_month_holiday_work_counts={},
        unavailable=set(),
        newcomer_shift_history={},
    )
    with_history = service._holiday_candidates(
        grid,
        employee_map,
        holiday_work_counters={},
        current_month_holiday_work_counts={},
        unavailable=set(),
        newcomer_shift_history={"Новичок Н.Н.": {"ВХ"}},
    )

    assert without_history == []
    assert with_history == ["Новичок Н.Н."]


def test_autoplan_alternates_khabarovsk_shift_types_when_possible(tmp_path):
    employees = [
        Employee("Андреев В. Ю.", status="active", location="khabarovsk", competencies=("support",)),
        Employee("Сафронов К. Е.", status="active", location="khabarovsk", competencies=("support",)),
    ]
    service = _service(tmp_path, employees)
    days = [
        ScheduleDay(3, "пн", date(2026, 8, 3)),
        ScheduleDay(4, "вт", date(2026, 8, 4)),
        ScheduleDay(5, "ср", date(2026, 8, 5)),
        ScheduleDay(6, "чт", date(2026, 8, 6)),
        ScheduleDay(7, "пт", date(2026, 8, 7)),
        ScheduleDay(10, "пн", date(2026, 8, 10)),
        ScheduleDay(11, "вт", date(2026, 8, 11)),
        ScheduleDay(12, "ср", date(2026, 8, 12)),
        ScheduleDay(13, "чт", date(2026, 8, 13)),
        ScheduleDay(14, "пт", date(2026, 8, 14)),
        ScheduleDay(17, "пн", date(2026, 8, 17)),
        ScheduleDay(18, "вт", date(2026, 8, 18)),
        ScheduleDay(19, "ср", date(2026, 8, 19)),
        ScheduleDay(20, "чт", date(2026, 8, 20)),
        ScheduleDay(21, "пт", date(2026, 8, 21)),
    ]
    grid = ScheduleGrid(
        "Август 2026",
        2026,
        8,
        days,
        [
            ScheduleRow(
                "Андреев В. Ю.",
                0,
                {day.day: ("ХД" if day.day == 3 else "") for day in days},
            ),
            ScheduleRow("Сафронов К. Е.", 0, {day.day: "" for day in days}),
        ],
    )

    planned, _explanations, _stop_cells = service._plan_grid(
        grid,
        {employee.name: employee for employee in employees},
    )
    by_name = {row.employee_name: row for row in planned.employees}

    assert {by_name["Андреев В. Ю."].assignments[3], by_name["Сафронов К. Е."].assignments[3]} == {"ХД", "ХР"}
    assert by_name["Андреев В. Ю."].assignments[10] != by_name["Андреев В. Ю."].assignments[3]
    assert by_name["Сафронов К. Е."].assignments[10] != by_name["Сафронов К. Е."].assignments[3]
    assert by_name["Андреев В. Ю."].assignments[17] == by_name["Андреев В. Ю."].assignments[3]
    assert by_name["Сафронов К. Е."].assignments[17] == by_name["Сафронов К. Е."].assignments[3]


def test_autoplan_continues_previous_month_shift_until_vacation_stop(tmp_path):
    employees = [
        Employee("ДР Старый", status="active", location="moscow", competencies=("support",)),
        Employee("ДД Сотрудник", status="active", location="moscow", competencies=("support",)),
        Employee("ДР Новый", status="active", location="moscow", competencies=("support",)),
        Employee("ВД Сотрудник", status="active", location="moscow", competencies=("support",)),
        Employee("ВР Сотрудник", status="active", location="moscow", competencies=("support",)),
    ]
    service = _service(tmp_path, employees)
    august = ScheduleGrid(
        "Август 2026",
        2026,
        8,
        [ScheduleDay(31, "пн", date(2026, 8, 31))],
        [
            ScheduleRow("ДР Старый", 0, {31: "ДР"}),
            *[ScheduleRow(employee.name, 0, {31: ""}) for employee in employees[1:]],
        ],
    )
    september_days = [
        ScheduleDay(1, "вт", date(2026, 9, 1)),
        ScheduleDay(2, "ср", date(2026, 9, 2)),
        ScheduleDay(3, "чт", date(2026, 9, 3)),
        ScheduleDay(4, "пт", date(2026, 9, 4)),
        ScheduleDay(5, "сб", date(2026, 9, 5)),
        ScheduleDay(6, "вс", date(2026, 9, 6)),
    ]
    september = ScheduleGrid(
        "Сентябрь 2026",
        2026,
        9,
        september_days,
        [
            ScheduleRow("ДР Старый", 0, {1: "", 2: "", 3: "", 4: "отпуск", 5: "", 6: ""}),
            *[
                ScheduleRow(employee.name, 0, {day.day: "8" for day in september_days})
                for employee in employees[1:]
            ],
        ],
    )
    snapshot = ScheduleSnapshot(
        employees=[],
        original_filename="fixture",
        stored_filename="",
        uploaded_at="",
        month_schedules=[
            {
                "year": 2026,
                "month": 8,
                "month_name": "Август",
                "sheet_name": "Август 2026",
                "label": "Август 2026",
                "grid": grid_to_dict(august),
            },
            {
                "year": 2026,
                "month": 9,
                "month_name": "Сентябрь",
                "sheet_name": "Сентябрь 2026",
                "label": "Сентябрь 2026",
                "grid": grid_to_dict(september),
            },
        ],
    )
    service.schedule_service = type("ScheduleService", (), {"get_current": lambda self: snapshot})()

    progress_messages = []
    planned, explanations, stop_cells = service._plan_grid(
        september,
        {employee.name: employee for employee in employees},
        progress=progress_messages.append,
    )
    by_name = {row.employee_name: row for row in planned.employees}

    assert {by_name["ДР Старый"].assignments[day] for day in (1, 2, 3)} == {"ДР"}
    assert by_name["ДР Старый"].assignments[4] == "отпуск"
    assert not any(
        row.employee_name != "ДР Старый" and any(row.assignments[day] == "ДР" for day in (1, 2, 3, 4))
        for row in planned.employees
    )
    assert any("нужно распределить вручную" in explanation["reason"] for explanation in explanations)
    assert stop_cells == [
        {
            "employee_name": "ДР Старый",
            "day": 4,
            "shift_code": "ДР",
            "stopper_day": 4,
            "stopper_value": "отпуск",
            "message": "ДР: переходящая смена остановлена 4 числа (отпуск). Дни 4 число нужно распределить вручную.",
        },
    ]
    assert any("требуют ручного распределения" in message for message in progress_messages)
    assert any("предыдущем графике" in message for message in progress_messages)


def test_historical_load_counts_duty_blocks_not_days(tmp_path):
    employees = [
        Employee("Фисан К.Ю.", status="active", location="moscow", competencies=("support",)),
    ]
    service = _service(tmp_path, employees)
    august_days = [
        ScheduleDay(3, "пн", date(2026, 8, 3)),
        ScheduleDay(4, "вт", date(2026, 8, 4)),
        ScheduleDay(5, "ср", date(2026, 8, 5)),
        ScheduleDay(6, "чт", date(2026, 8, 6)),
        ScheduleDay(7, "пт", date(2026, 8, 7)),
        ScheduleDay(8, "сб", date(2026, 8, 8)),
        ScheduleDay(24, "пн", date(2026, 8, 24)),
        ScheduleDay(25, "вт", date(2026, 8, 25)),
    ]
    august = ScheduleGrid(
        "Август 2026",
        2026,
        8,
        august_days,
        [
            ScheduleRow(
                "Фисан К.Ю.",
                0,
                {3: "ВД", 4: "ВД", 5: "ВД", 6: "ВД", 7: "ВД", 8: "ВХ", 24: "ДД", 25: "ДД"},
            )
        ],
    )
    september = ScheduleGrid(
        "Сентябрь 2026",
        2026,
        9,
        [ScheduleDay(1, "вт", date(2026, 9, 1))],
        [ScheduleRow("Фисан К.Ю.", 0, {1: ""})],
    )
    snapshot = ScheduleSnapshot(
        employees=[],
        original_filename="fixture",
        stored_filename="",
        uploaded_at="",
        month_schedules=[
            {
                "year": 2026,
                "month": 8,
                "month_name": "Август",
                "sheet_name": "Август 2026",
                "label": "Август 2026",
                "grid": grid_to_dict(august),
            },
            {
                "year": 2026,
                "month": 9,
                "month_name": "Сентябрь",
                "sheet_name": "Сентябрь 2026",
                "label": "Сентябрь 2026",
                "grid": grid_to_dict(september),
            },
        ],
    )
    service.schedule_service = type("ScheduleService", (), {"get_current": lambda self: snapshot})()

    assert service._historical_load(september)["Фисан К.Ю."] == 3


def test_candidate_ranking_does_not_ignore_historical_load(tmp_path):
    employees = [
        Employee("Исторически нагружен", status="active", location="moscow", competencies=("support",)),
        Employee("Менее нагружен", status="active", location="moscow", competencies=("support",)),
    ]
    service = _service(tmp_path, employees)
    grid = _grid(employees)

    candidates = service._employee_candidates(
        grid,
        {employee.name: employee for employee in employees},
        counters={"Исторически нагружен": 10, "Менее нагружен": 1},
        unavailable=set(),
        week_assigned=set(),
        location="moscow",
        allow_manager=False,
        shift_code="ДД",
        current_month_duty_blocks={"Исторически нагружен": 0, "Менее нагружен": 1},
    )

    assert candidates[0] == "Менее нагружен"


def test_candidate_ranking_avoids_third_month_block_when_alternative_exists(tmp_path):
    employees = [
        Employee("Уже два блока", status="active", location="moscow", competencies=("support",)),
        Employee("Один блок", status="active", location="moscow", competencies=("support",)),
    ]
    service = _service(tmp_path, employees)
    grid = _grid(employees)

    candidates = service._employee_candidates(
        grid,
        {employee.name: employee for employee in employees},
        counters={"Уже два блока": 1, "Один блок": 5},
        unavailable=set(),
        week_assigned=set(),
        location="moscow",
        allow_manager=False,
        shift_code="ДД",
        current_month_duty_blocks={"Уже два блока": 2, "Один блок": 1},
    )

    assert candidates[0] == "Один блок"


def test_holiday_work_does_not_consume_weekday_duty_block_limit(tmp_path):
    employees = [
        Employee("С ВХ", status="active", location="moscow", competencies=("support",), overtime_ready=True),
        Employee("Без ВХ", status="active", location="moscow", competencies=("support",), overtime_ready=True),
    ]
    service = _service(tmp_path, employees)
    days = [
        ScheduleDay(1, "сб", date(2026, 8, 1)),
        ScheduleDay(3, "пн", date(2026, 8, 3)),
        ScheduleDay(4, "вт", date(2026, 8, 4)),
        ScheduleDay(5, "ср", date(2026, 8, 5)),
        ScheduleDay(6, "чт", date(2026, 8, 6)),
        ScheduleDay(7, "пт", date(2026, 8, 7)),
    ]
    grid = ScheduleGrid(
        "Август 2026",
        2026,
        8,
        days,
        [
            ScheduleRow("С ВХ", 0, {1: "ВХ", 3: "", 4: "", 5: "", 6: "", 7: ""}),
            ScheduleRow("Без ВХ", 0, {day.day: "" for day in days}),
        ],
    )

    planned, _explanations, _stop_cells = service._plan_grid(
        grid,
        {employee.name: employee for employee in employees},
    )
    by_name = {row.employee_name: row for row in planned.employees}

    assert any(by_name["С ВХ"].assignments[day] in {"ДД", "ДР", "ВД", "ВР"} for day in (3, 4, 5, 6, 7))


def test_validator_marks_unfilled_transition_workdays_until_manual_fix():
    employees = [
        Employee("ДР Старый", status="active", location="moscow", competencies=("support",)),
        Employee("ДР Новый", status="active", location="moscow", competencies=("support",)),
    ]
    august = ScheduleGrid(
        "Август 2026",
        2026,
        8,
        [ScheduleDay(31, "пн", date(2026, 8, 31))],
        [
            ScheduleRow("ДР Старый", 0, {31: "ДР"}),
            ScheduleRow("ДР Новый", 0, {31: ""}),
        ],
    )
    september_days = [
        ScheduleDay(1, "вт", date(2026, 9, 1)),
        ScheduleDay(2, "ср", date(2026, 9, 2)),
        ScheduleDay(3, "чт", date(2026, 9, 3)),
        ScheduleDay(4, "пт", date(2026, 9, 4)),
        ScheduleDay(5, "сб", date(2026, 9, 5)),
        ScheduleDay(6, "вс", date(2026, 9, 6)),
    ]
    september = ScheduleGrid(
        "Сентябрь 2026",
        2026,
        9,
        september_days,
        [
            ScheduleRow("ДР Старый", 0, {1: "ДР", 2: "ДР", 3: "ДР", 4: "отпуск", 5: "", 6: ""}),
            ScheduleRow("ДР Новый", 0, {day.day: "" for day in september_days}),
        ],
    )
    rules = build_validation_rules(employees=employees)

    violations = validate_schedule(september, rules, previous_grid=august)
    transition_violations = [
        violation
        for violation in violations
        if "Переходящая смена" in violation.message
    ]
    assert [(violation.day, violation.employee_name) for violation in transition_violations] == [
        (4, "ДР Старый")
    ]

    september.employees[1].assignments[4] = "ДР"
    fixed_violations = validate_schedule(september, rules, previous_grid=august)
    assert not any("Переходящая смена" in violation.message for violation in fixed_violations)


def test_validator_allows_newcomer_only_for_historically_allowed_shift():
    employees = [
        Employee(
            "Новичок Н.Н.",
            status="active",
            location="moscow",
            competencies=("support", "newcomer"),
            overtime_ready=True,
        ),
        Employee("Сотрудник С.С.", status="active", location="moscow", competencies=("support",), overtime_ready=True),
    ]
    grid = ScheduleGrid(
        "Август 2026",
        2026,
        8,
        [
            ScheduleDay(3, "пн", date(2026, 8, 3)),
            ScheduleDay(8, "сб", date(2026, 8, 8)),
        ],
        [
            ScheduleRow("Новичок Н.Н.", 0, {3: "ДД", 8: "ВХ"}),
            ScheduleRow("Сотрудник С.С.", 0, {3: "8", 8: "Вых"}),
        ],
    )

    blocked_messages = [
        violation.message
        for violation in validate_schedule(grid, build_validation_rules(employees=employees))
    ]
    allowed_messages = [
        violation.message
        for violation in validate_schedule(
            grid,
            build_validation_rules(
                employees=employees,
                newcomer_allowed_shift_codes={"Новичок Н.Н.": {"ДД", "ВХ"}},
            ),
        )
    ]

    assert blocked_messages.count("Новичка нельзя назначать в дежурные смены и ВХ.") == 2
    assert "Новичка нельзя назначать в дежурные смены и ВХ." not in allowed_messages
