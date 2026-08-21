from __future__ import annotations

from typing import Any, Iterable


HIDDEN_HINT_CODES = {"8", "п", "праздник", "о", "отпуск"}


def normalize_autoplan_artifact(
    value: Any,
    *,
    year: int,
    month: int,
    employee_names: Iterable[str],
    valid_days: Iterable[int],
) -> dict:
    if not isinstance(value, dict) or value.get("source") != "autoplanner":
        return {}
    try:
        artifact_year = int(value.get("year"))
        artifact_month = int(value.get("month"))
    except (TypeError, ValueError):
        return {}
    if artifact_year != int(year) or artifact_month != int(month):
        return {}

    employees = {
        _clean_text(name, 300) for name in employee_names if _clean_text(name, 300)
    }
    days = {int(day) for day in valid_days}
    explanations = []
    raw_explanations = value.get("assignment_explanations")
    if not isinstance(raw_explanations, list):
        raw_explanations = []
    for raw in raw_explanations:
        if not isinstance(raw, dict):
            continue
        employee_name = _clean_text(raw.get("employee_name"), 300)
        shift_code = _clean_text(raw.get("shift_code"), 80)
        if not employee_name or employee_name not in employees or not shift_code:
            continue
        explanation_days = []
        raw_days = raw.get("days")
        if not isinstance(raw_days, list):
            raw_days = []
        for raw_day in raw_days:
            try:
                day = int(raw_day)
            except (TypeError, ValueError):
                continue
            if day in days and day not in explanation_days:
                explanation_days.append(day)
        if not explanation_days:
            continue
        explanations.append(
            {
                "employee_name": employee_name,
                "shift_code": shift_code,
                "shift_name": _clean_text(raw.get("shift_name"), 200),
                "period": _clean_text(raw.get("period"), 200),
                "days": explanation_days,
                "reason": _clean_text(raw.get("reason"), 1200),
                "candidate_count": _non_negative_int(
                    raw.get("candidate_count")
                ),
                "load_before": _non_negative_int(raw.get("load_before")),
            }
        )
        if len(explanations) >= 5000:
            break

    stop_cells = []
    raw_stop_cells = value.get("stop_cells")
    if not isinstance(raw_stop_cells, list):
        raw_stop_cells = []
    for raw in raw_stop_cells:
        if not isinstance(raw, dict):
            continue
        employee_name = _clean_text(raw.get("employee_name"), 300)
        if not employee_name or employee_name not in employees:
            continue
        try:
            day = int(raw.get("day"))
        except (TypeError, ValueError):
            continue
        if day not in days:
            continue
        stop_cells.append(
            {
                "employee_name": employee_name,
                "day": day,
                "shift_code": _clean_text(raw.get("shift_code"), 80),
                "message": _clean_text(raw.get("message"), 1200),
            }
        )
        if len(stop_cells) >= 500:
            break

    reasons = []
    raw_reasons = value.get("reasons")
    if not isinstance(raw_reasons, list):
        raw_reasons = []
    for raw_reason in raw_reasons:
        reason = _clean_text(raw_reason, 1200)
        if reason:
            reasons.append(reason)
        if len(reasons) >= 50:
            break

    return {
        "source": "autoplanner",
        "created_at": _clean_text(value.get("created_at"), 80),
        "title": _clean_text(value.get("title"), 300)
        or "График сформирован автопланировщиком",
        "summary": _clean_text(value.get("summary"), 2000),
        "reasons": reasons,
        "violation_count": _non_negative_int(value.get("violation_count")),
        "year": artifact_year,
        "month": artifact_month,
        "assignment_explanations": explanations,
        "stop_cells": stop_cells,
    }


def build_autoplan_hints(artifact: Any) -> dict:
    if not isinstance(artifact, dict):
        return {}
    hints = {}
    for explanation in artifact.get("assignment_explanations") or []:
        if not isinstance(explanation, dict):
            continue
        shift_code = str(explanation.get("shift_code") or "").strip()
        if shift_code.casefold() in HIDDEN_HINT_CODES:
            continue
        employee_name = str(explanation.get("employee_name") or "").strip()
        if not employee_name:
            continue
        text = autoplan_hint_text(explanation)
        for raw_day in explanation.get("days") or []:
            try:
                day = int(raw_day)
            except (TypeError, ValueError):
                continue
            hints[f"{employee_name}|{day}"] = text
    return hints


def build_autoplan_stop_cells(artifact: Any) -> dict:
    if not isinstance(artifact, dict):
        return {}
    cells = {}
    for stop_cell in artifact.get("stop_cells") or []:
        if not isinstance(stop_cell, dict):
            continue
        employee_name = str(stop_cell.get("employee_name") or "").strip()
        if not employee_name:
            continue
        try:
            day = int(stop_cell.get("day"))
        except (TypeError, ValueError):
            continue
        message = str(stop_cell.get("message") or "").strip()
        cells[f"{employee_name}|{day}"] = message
    return cells


def autoplan_hint_text(explanation: dict) -> str:
    shift_code = str(explanation.get("shift_code") or "").strip()
    shift_name = str(explanation.get("shift_name") or "").strip()
    period = str(explanation.get("period") or "").strip()
    reason = str(explanation.get("reason") or "").strip()
    title = shift_code
    if shift_name and shift_name != shift_code:
        title = f"{title} · {shift_name}"
    parts = [f"Автоплан: {title}"]
    if period:
        parts.append(period)
    if reason:
        parts.append(reason)
    parts.append(
        "Кандидатов: "
        f"{explanation.get('candidate_count', 0)}; "
        "нагрузка до назначения: "
        f"{explanation.get('load_before', 0)}."
    )
    return " ".join(parts)


def _clean_text(value: Any, limit: int) -> str:
    return " ".join(str(value or "").strip().split())[:limit]


def _non_negative_int(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0
