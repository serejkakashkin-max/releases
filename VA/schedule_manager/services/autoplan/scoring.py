from dataclasses import dataclass
from typing import Dict, Iterable, List

from VA.schedule_manager.models.employee import Employee


@dataclass(frozen=True)
class CandidateScore:
    employee_name: str
    score: tuple
    penalties: tuple


@dataclass(frozen=True)
class ScoringPolicy:
    preferred_month_duty_blocks: int = 2
    hard_month_duty_blocks: int = 3
    overload_penalty: int = 100
    hard_overload_penalty: int = 1000


class CandidateScorer:
    """Ranks eligible candidates without deciding hard eligibility rules."""

    def __init__(self, policy: ScoringPolicy = None) -> None:
        self.policy = policy or ScoringPolicy()

    def rank_weekday_candidates(
        self,
        candidates: Iterable[str],
        employees: Dict[str, Employee],
        shift_code: str,
        historical_load: Dict[str, int],
        current_month_duty_blocks: Dict[str, int],
        evening_priority,
    ) -> List[str]:
        candidates = list(candidates)
        in_preferred_limit = [
            name
            for name in candidates
            if current_month_duty_blocks.get(name, 0) + 1 <= self.policy.preferred_month_duty_blocks
        ]
        if in_preferred_limit:
            candidates = in_preferred_limit

        scored = [
            self.weekday_score(
                name,
                employees[name],
                shift_code,
                historical_load.get(name, 0),
                current_month_duty_blocks.get(name, 0),
                evening_priority,
            )
            for name in candidates
            if name in employees
        ]
        return [item.employee_name for item in sorted(scored, key=lambda item: item.score)]

    def weekday_score(
        self,
        employee_name: str,
        employee: Employee,
        shift_code: str,
        historical_blocks: int,
        current_month_blocks: int,
        evening_priority,
    ) -> CandidateScore:
        next_month_blocks = current_month_blocks + 1
        penalties = (
            self._month_overload_penalty(next_month_blocks),
        )
        score = (
            historical_blocks + next_month_blocks + sum(penalties),
            penalties,
            current_month_blocks,
            evening_priority(employee, shift_code),
            historical_blocks,
            employee_name,
        )
        return CandidateScore(employee_name, score, penalties)

    def _month_overload_penalty(self, next_month_blocks: int) -> int:
        if next_month_blocks > self.policy.hard_month_duty_blocks:
            return self.policy.hard_overload_penalty
        if next_month_blocks > self.policy.preferred_month_duty_blocks:
            return self.policy.overload_penalty
        return 0
