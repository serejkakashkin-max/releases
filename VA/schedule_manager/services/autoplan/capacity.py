from typing import List


class DutyCapacityDiagnostics:
    def __init__(self, preferred_month_duty_blocks: int) -> None:
        self.preferred_month_duty_blocks = preferred_month_duty_blocks
        self._overloaded_assignments = []
        self._warnings = []
        self._period = ""
        self._week_overloaded = []
        self._week_candidates = set()
        self._week_existing_workers = set()
        self._blocks_count = 0

    def start_week(self, period: str) -> None:
        self._period = period
        self._week_overloaded = []
        self._week_candidates = set()
        self._week_existing_workers = set()
        self._blocks_count = 0

    def note_shift_candidates(self, shift_code: str, candidates: List[str]) -> None:
        self._week_candidates.update(candidates or [])

    def note_planned_assignment(self, employee_name: str, shift_code: str, candidates: List[str], current_month_duty_blocks: int, historical_load: int) -> None:
        self._blocks_count += 1
        if current_month_duty_blocks < self.preferred_month_duty_blocks:
            return
        self._week_overloaded.append({
            "employee_name": employee_name,
            "shift_code": shift_code,
            "period": self._period,
            "blocks_before": current_month_duty_blocks,
            "candidate_count": len(candidates or []),
            "historical_load": historical_load,
            "other_candidates": [{"name": name} for name in (candidates or []) if name != employee_name][:5],
        })

    def note_existing_block(self, employee_name: str, shift_code: str) -> None:
        self._blocks_count += 1
        self._week_existing_workers.add(employee_name)

    def finish_week(self) -> None:
        if not self._week_overloaded:
            return
        available_count = len(self._week_candidates | self._week_existing_workers)
        self._warnings.append(
            f"На неделе {self._period} доступных дежурных {available_count}, дежурных блоков {self._blocks_count} — "
            f"ровное распределение (не более {self.preferred_month_duty_blocks} блоков на человека) недостижимо."
        )
        self._overloaded_assignments.extend(self._week_overloaded)

    def build(self) -> dict:
        if not self._overloaded_assignments:
            return {}
        return {"overloaded_assignments": self._overloaded_assignments, "warnings": self._warnings}
