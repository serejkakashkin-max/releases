from VA.schedule_manager.models.employee import Employee
from VA.schedule_manager.services.autoplan.scoring import CandidateScorer


def test_candidate_scorer_penalizes_third_month_duty_block():
    employees = {
        "Уже два блока": Employee("Уже два блока", status="active", location="moscow"),
        "Один блок": Employee("Один блок", status="active", location="moscow"),
    }
    scorer = CandidateScorer()

    ranked = scorer.rank_weekday_candidates(
        ["Уже два блока", "Один блок"],
        employees,
        "ДД",
        historical_load={"Уже два блока": 1, "Один блок": 5},
        current_month_duty_blocks={"Уже два блока": 2, "Один блок": 1},
        evening_priority=lambda _employee, _shift_code: 1,
    )

    assert ranked[0] == "Один блок"
