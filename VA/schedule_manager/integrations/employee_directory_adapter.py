from __future__ import annotations

import logging
import threading
from typing import Any, Dict, Iterable, List, Tuple

from VA.schedule_manager.models.employee import Employee
from services.employee_directory_repository import normalize_text, read_directory_snapshot
from services.feature_flags_service import get_employee_directory_consumer_mode


LOGGER = logging.getLogger(__name__)
_diagnostic_lock = threading.Lock()
_last_diagnostic_key: Tuple[str, str] | None = None


def apply_employee_directory_mode(employees: Iterable[Employee]) -> List[Employee]:
    """Compare VA identities while preserving the original runtime records."""
    legacy_employees = list(employees)
    mode = get_employee_directory_consumer_mode("va_schedule_manager")
    if mode == "legacy":
        return legacy_employees

    comparison = compare_va_employees(legacy_employees)
    _log_comparison_once(mode, comparison)

    # In-memory central projection is a separate step after compare observation.
    return legacy_employees


def get_va_schedule_manager_adapter_readiness(
    employees: Iterable[Employee],
) -> Dict[str, Any]:
    comparison = compare_va_employees(employees)
    ready = bool(comparison["matches"])
    reason = "compare_ready"
    if ready and comparison["common_field_mismatch_count"]:
        reason = "compare_ready_with_common_field_differences"
    elif not ready:
        reason = comparison["reason"]
    return {
        "ready": ready,
        "reason": reason,
        "allowed_modes": ["legacy", "compare"] if ready else ["legacy"],
        "comparison": comparison,
    }


def compare_va_employees(employees: Iterable[Employee]) -> Dict[str, Any]:
    legacy_employees = list(employees)
    snapshot = read_directory_snapshot()
    if snapshot.status != "available" or not snapshot.payload:
        return {
            "matches": False,
            "status": snapshot.status,
            "reason": "employee_directory_not_available",
            "legacy_count": len(legacy_employees),
            "directory_count": 0,
            "unresolved_count": 0,
            "ambiguous_count": 0,
            "common_field_mismatch_count": 0,
        }

    central_employees = sorted(
        [
            employee
            for employee in snapshot.payload["employees"]
            if employee["enabled"]
            and employee["memberships"]["va_schedule_manager"]["enabled"]
        ],
        key=lambda employee: employee["memberships"]["va_schedule_manager"]["order"],
    )
    resolved_ids: List[str] = []
    unresolved_count = 0
    ambiguous_count = 0
    common_field_mismatch_count = 0

    for legacy_employee in legacy_employees:
        matches = [
            central_employee
            for central_employee in central_employees
            if _normalize(legacy_employee.name) in _identity_values(central_employee)
        ]
        if not matches:
            unresolved_count += 1
            continue
        if len(matches) != 1:
            ambiguous_count += 1
            continue
        central_employee = matches[0]
        resolved_ids.append(central_employee["employee_id"])
        common_field_mismatch_count += _common_field_mismatches(
            legacy_employee,
            central_employee,
        )

    expected_ids = [employee["employee_id"] for employee in central_employees]
    matches = (
        not unresolved_count
        and not ambiguous_count
        and len(resolved_ids) == len(set(resolved_ids))
        and resolved_ids == expected_ids
    )
    return {
        "matches": matches,
        "status": "available",
        "reason": "exact_identity_order_match" if matches else "identity_or_order_mismatch",
        "legacy_count": len(legacy_employees),
        "directory_count": len(central_employees),
        "unresolved_count": unresolved_count,
        "ambiguous_count": ambiguous_count,
        "common_field_mismatch_count": common_field_mismatch_count,
    }


def _identity_values(employee: Dict[str, Any]) -> set[str]:
    values = {_normalize(employee["full_name"])}
    values.update(
        _normalize(alias["value"])
        for alias in employee["aliases"]
        if alias["type"] in {"full", "schedule", "va"}
    )
    values.discard("")
    return values


def _common_field_mismatches(legacy: Employee, central: Dict[str, Any]) -> int:
    mismatches = 0
    legacy_email = normalize_text(legacy.email).lower()
    central_emails = {normalize_text(value).lower() for value in central["emails"]}
    if legacy_email and legacy_email not in central_emails:
        mismatches += 1
    for legacy_value, central_value in (
        (legacy.phone, central["phone"]),
        (legacy.location, central["location"]),
        (legacy.personnel_number, central["personnel_number"]),
    ):
        if normalize_text(legacy_value) != normalize_text(central_value):
            mismatches += 1
    return mismatches


def _normalize(value: Any) -> str:
    return normalize_text(value).casefold()


def _log_comparison_once(mode: str, comparison: Dict[str, Any]) -> None:
    global _last_diagnostic_key
    status = "match" if comparison["matches"] else comparison["reason"]
    key = (mode, status)
    with _diagnostic_lock:
        if key == _last_diagnostic_key:
            return
        _last_diagnostic_key = key

    log = LOGGER.info if comparison["matches"] else LOGGER.warning
    log(
        "Employee directory va_schedule_manager comparison: mode=%s status=%s legacy_count=%s directory_count=%s unresolved_count=%s ambiguous_count=%s common_field_mismatch_count=%s",
        mode,
        status,
        comparison["legacy_count"],
        comparison["directory_count"],
        comparison["unresolved_count"],
        comparison["ambiguous_count"],
        comparison["common_field_mismatch_count"],
    )
