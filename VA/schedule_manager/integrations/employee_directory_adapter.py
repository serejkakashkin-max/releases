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
    """Apply central common fields without changing VA schedule identity keys."""
    legacy_employees = list(employees)
    mode = get_employee_directory_consumer_mode("va_schedule_manager")
    if mode == "legacy":
        return legacy_employees

    comparison = compare_va_employees(legacy_employees)
    _log_comparison_once(mode, comparison)
    if mode != "directory" or not comparison["projection_ready"]:
        return legacy_employees

    snapshot = read_directory_snapshot()
    central_employees = _central_va_employees(snapshot.payload or {})
    projection = _build_directory_projection(legacy_employees, central_employees)
    return projection if projection is not None else legacy_employees


def prepare_va_records_for_save(
    employees: Iterable[Employee],
    legacy_employees: Iterable[Employee],
) -> List[Employee]:
    """Keep central-owned values out of VA JSON while saving VA-only fields."""
    incoming = list(employees)
    legacy = list(legacy_employees)
    if get_employee_directory_consumer_mode("va_schedule_manager") != "directory":
        return incoming

    incoming_by_name = {_normalize(employee.name): employee for employee in incoming}
    consumed_names: set[str] = set()
    result: List[Employee] = []
    for current in legacy:
        updated = incoming_by_name.get(_normalize(current.name))
        if updated is None:
            # Membership and identity are managed by the central directory.
            result.append(current)
            continue
        consumed_names.add(_normalize(updated.name))
        result.append(
            Employee(
                name=current.name,
                email=current.email,
                phone=current.phone,
                status=updated.status,
                personnel_number=current.personnel_number,
                role=updated.role,
                location=current.location,
                competencies=updated.competencies,
                overtime_ready=updated.overtime_ready,
            )
        )

    snapshot = read_directory_snapshot()
    if snapshot.status != "available" or not snapshot.payload:
        return result
    comparison = compare_va_employees(legacy)
    if not comparison["projection_ready"]:
        return result
    central_employees = _central_va_employees(snapshot.payload)
    projection = _build_directory_projection(legacy, central_employees) or []
    central_projection_names = {_normalize(employee.name) for employee in projection}
    for updated in incoming:
        normalized_name = _normalize(updated.name)
        if normalized_name in consumed_names or normalized_name not in central_projection_names:
            continue
        # Persist only a VA shadow with VA-specific defaults/settings. Central
        # contacts continue to be projected on every read.
        result.append(
            Employee(
                name=updated.name,
                email="",
                phone="",
                status=updated.status,
                personnel_number=None,
                role=updated.role,
                location=updated.location or "moscow",
                competencies=updated.competencies,
                overtime_ready=updated.overtime_ready,
            )
        )
        consumed_names.add(normalized_name)
    return result


def is_va_employee_directory_managed() -> bool:
    if get_employee_directory_consumer_mode("va_schedule_manager") != "directory":
        return False
    return read_directory_snapshot().status == "available"


def get_va_schedule_manager_adapter_readiness(
    employees: Iterable[Employee],
) -> Dict[str, Any]:
    comparison = compare_va_employees(employees)
    ready = bool(comparison["projection_ready"])
    mode = get_employee_directory_consumer_mode("va_schedule_manager")
    if ready and comparison["central_only_count"]:
        reason = "directory_active_with_central_additions" if mode == "directory" else "compare_ready_with_central_additions"
    elif ready and comparison["legacy_only_count"]:
        reason = "directory_active_with_central_removals" if mode == "directory" else "compare_ready_with_central_removals"
    elif ready and comparison["order_mismatch"]:
        reason = "directory_active_with_central_order" if mode == "directory" else "compare_ready_with_central_order"
    elif ready and comparison["common_field_mismatch_count"]:
        reason = "directory_active_with_common_field_differences" if mode == "directory" else "compare_ready_with_common_field_differences"
    elif ready:
        reason = "directory_active" if mode == "directory" else "compare_ready"
    else:
        reason = comparison["reason"]
    return {
        "ready": ready,
        "reason": reason,
        "allowed_modes": ["legacy", "compare", "directory"] if ready else ["legacy"],
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
            "central_only_count": 0,
            "legacy_only_count": 0,
            "order_mismatch": False,
            "projection_ready": False,
        }

    central_employees = _central_va_employees(snapshot.payload)
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
    central_only_count = len(set(expected_ids) - set(resolved_ids))
    legacy_only_count = unresolved_count
    order_mismatch = (
        not central_only_count
        and not legacy_only_count
        and resolved_ids != expected_ids
    )
    projection_ready = (
        not ambiguous_count
        and len(resolved_ids) == len(set(resolved_ids))
        and not (central_only_count and legacy_only_count)
    )
    matches = (
        not unresolved_count
        and not ambiguous_count
        and len(resolved_ids) == len(set(resolved_ids))
        and resolved_ids == expected_ids
    )
    return {
        "matches": matches,
        "status": "available",
        "reason": _comparison_reason(
            matches=matches,
            projection_ready=projection_ready,
            central_only_count=central_only_count,
            legacy_only_count=legacy_only_count,
            order_mismatch=order_mismatch,
        ),
        "legacy_count": len(legacy_employees),
        "directory_count": len(central_employees),
        "unresolved_count": unresolved_count,
        "ambiguous_count": ambiguous_count,
        "common_field_mismatch_count": common_field_mismatch_count,
        "central_only_count": central_only_count,
        "legacy_only_count": legacy_only_count,
        "order_mismatch": order_mismatch,
        "projection_ready": projection_ready,
    }


def _identity_values(employee: Dict[str, Any]) -> set[str]:
    values = {_normalize(employee["full_name"])}
    values.update(
        _normalize(alias["value"])
        for alias in employee["aliases"]
        if alias["type"] in {"full", "schedule", "va"}
    )
    values.update(
        _normalize(source_ref.split(":", 2)[2])
        for source_ref in employee.get("source_refs", [])
        if normalize_text(source_ref).startswith("va:employees:")
        and len(source_ref.split(":", 2)) == 3
    )
    values.discard("")
    return values


def _comparison_reason(
    *,
    matches: bool,
    projection_ready: bool,
    central_only_count: int,
    legacy_only_count: int,
    order_mismatch: bool,
) -> str:
    if matches:
        return "exact_identity_order_match"
    if central_only_count and legacy_only_count:
        return "possible_identity_replacement"
    if not projection_ready:
        return "ambiguous_identity_match"
    if central_only_count:
        return "central_employees_added"
    if legacy_only_count:
        return "central_employees_removed"
    if order_mismatch:
        return "central_order_changed"
    return "compatible_projection"


def _build_directory_projection(
    legacy_employees: List[Employee],
    central_employees: List[Dict[str, Any]],
) -> List[Employee] | None:
    used_legacy_indexes: set[int] = set()
    projection: List[Employee] = []
    for central_employee in central_employees:
        candidates = [
            index
            for index, legacy_employee in enumerate(legacy_employees)
            if _normalize(legacy_employee.name) in _identity_values(central_employee)
        ]
        if len(candidates) > 1:
            return None
        if candidates:
            legacy_index = candidates[0]
            if legacy_index in used_legacy_indexes:
                return None
            used_legacy_indexes.add(legacy_index)
            projection.append(
                _merge_central_common_fields(
                    legacy_employees[legacy_index],
                    central_employee,
                )
            )
            continue
        projection.append(_new_va_employee(central_employee))
    return projection


def _central_va_employees(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    return sorted(
        [
            employee
            for employee in payload.get("employees", [])
            if employee["enabled"]
            and employee["memberships"]["va_schedule_manager"]["enabled"]
        ],
        key=lambda employee: employee["memberships"]["va_schedule_manager"]["order"],
    )


def _merge_central_common_fields(
    legacy: Employee,
    central: Dict[str, Any],
) -> Employee:
    central_emails = list(central.get("emails") or [])
    return Employee(
        # VA schedule rows keep using their existing, already persisted name.
        name=legacy.name,
        email=central_emails[0] if central_emails else legacy.email,
        phone=central.get("phone") or legacy.phone,
        status=legacy.status,
        personnel_number=central.get("personnel_number") or legacy.personnel_number,
        role=legacy.role,
        location=_va_location(central.get("location"), legacy.location),
        competencies=legacy.competencies,
        overtime_ready=legacy.overtime_ready,
    )


def _new_va_employee(central: Dict[str, Any]) -> Employee:
    aliases = central.get("aliases") or []
    preferred_name = next(
        (
            normalize_text(source_ref.split(":", 2)[2])
            for source_ref in central.get("source_refs") or []
            if normalize_text(source_ref).startswith("va:employees:")
            and len(source_ref.split(":", 2)) == 3
        ),
        "",
    ) or next(
        (
            normalize_text(alias.get("value"))
            for alias_type in ("va", "schedule", "full")
            for alias in aliases
            if alias.get("type") == alias_type and normalize_text(alias.get("value"))
        ),
        normalize_text(central.get("full_name")),
    )
    central_emails = list(central.get("emails") or [])
    return Employee(
        name=preferred_name,
        email=central_emails[0] if central_emails else "",
        phone=central.get("phone") or "",
        status="active",
        personnel_number=central.get("personnel_number") or None,
        role="employee",
        location=_va_location(central.get("location"), "moscow"),
        competencies=("support",),
        overtime_ready=True,
    )


def _va_location(value: Any, fallback: Any) -> str:
    normalized = _normalize(value)
    aliases = {
        "moscow": "moscow",
        "москва": "moscow",
        "khabarovsk": "khabarovsk",
        "хабаровск": "khabarovsk",
    }
    return aliases.get(normalized) or aliases.get(_normalize(fallback)) or "moscow"


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
