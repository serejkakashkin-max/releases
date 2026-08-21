from typing import Callable, Dict, Set


def collect_newcomer_shift_codes(
    snapshot,
    grid,
    normalize_code: Callable[[object], str],
    load_shift_codes: Set[str],
    include_current_month: bool = False,
) -> Dict[str, Set[str]]:
    """Collect newcomer shift codes from saved months and, optionally, the target grid."""
    if snapshot is None:
        return {}

    target_index = grid.year * 12 + grid.month
    shift_codes: Dict[str, Set[str]] = {}
    for month in snapshot.month_schedules:
        try:
            month_index = int(month["year"]) * 12 + int(month["month"])
        except (KeyError, TypeError, ValueError):
            continue
        if month_index > target_index:
            continue
        if month_index == target_index and not include_current_month:
            continue
        history_grid = grid if month_index == target_index else None
        if history_grid is None:
            try:
                history_grid = snapshot.get_month_grid(str(month["sheet_name"]))
            except KeyError:
                continue
        for row in history_grid.employees:
            for code in row.assignments.values():
                normalized = normalize_code(code)
                if normalized in load_shift_codes:
                    shift_codes.setdefault(row.employee_name, set()).add(normalized)
    return shift_codes
