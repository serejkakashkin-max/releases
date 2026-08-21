from datetime import date

from VA.schedule_manager.models.schedule_grid import ScheduleDay, ScheduleGrid
from VA.schedule_manager.services.autoplan.audit import AutoplanAuditBuilder


def test_autoplan_audit_builds_assignment_explanation():
    audit = AutoplanAuditBuilder(lambda code: {"ДД": "Дневной дежурный"}.get(code, code))

    explanation = audit.assignment_explanation(
        "Фисан К.Ю.",
        "ДД",
        "1-5 числа",
        [1, 2, 3, 4, 5],
        ["Фисан К.Ю.", "Ефимов В.В."],
        7,
        ["сотрудник доступен", "нагрузка минимальна"],
    )

    assert explanation == {
        "employee_name": "Фисан К.Ю.",
        "shift_code": "ДД",
        "shift_name": "Дневной дежурный",
        "period": "1-5 числа",
        "days": [1, 2, 3, 4, 5],
        "reason": "сотрудник доступен; нагрузка минимальна.",
        "short_reason": "",
        "candidate_count": 2,
        "load_before": 7,
    }


def test_autoplan_audit_assignment_explanation_accepts_headline():
    audit = AutoplanAuditBuilder(lambda code: code)

    explanation = audit.assignment_explanation(
        "Фисан К.Ю.", "ДД", "1-5 числа", [1, 2, 3, 4, 5], [], 0, [],
        headline="Смену поставил автопланировщик.",
    )

    assert explanation["short_reason"] == "Смену поставил автопланировщик."


def test_autoplan_audit_builds_artifact_with_reasons_and_stop_cells():
    audit = AutoplanAuditBuilder(lambda code: code)
    grid = ScheduleGrid("Сентябрь 2026", 2026, 9, [ScheduleDay(1, "вт", date(2026, 9, 1))], [])

    artifact = audit.build_artifact(
        grid,
        assigned_cells_count=12,
        violation_count=1,
        assignment_explanations=[{"employee_name": "Фисан К.Ю."}],
        stop_cells=[{"employee_name": "Кашкин С.Н.", "day": 4}],
    )

    assert artifact["source"] == "autoplanner"
    assert artifact["year"] == 2026
    assert artifact["month"] == 9
    assert artifact["violation_count"] == 1
    assert artifact["assignment_explanations"] == [{"employee_name": "Фисан К.Ю."}]
    assert artifact["stop_cells"] == [{"employee_name": "Кашкин С.Н.", "day": 4}]
    assert any("Нагрузка недельных дежурств считалась блоками" in reason for reason in artifact["reasons"])
