from __future__ import annotations

import copy
from pathlib import Path
from typing import Any, Dict, Iterable
from uuid import uuid4

from services.employee_directory_repository import (
    EmployeeDirectoryStateError,
    EMPLOYEE_DIRECTORY_FILE,
    canonical_source_ref,
    normalize_text,
    read_directory_snapshot,
    save_employee_directory,
)


class VaEmployeeDirectoryCommandError(RuntimeError):
    pass


def get_va_employee_directory_write_state(
    path: Path = EMPLOYEE_DIRECTORY_FILE,
) -> Dict[str, Any]:
    snapshot = read_directory_snapshot(path)
    return {
        "status": snapshot.status,
        "revision": snapshot.revision,
        "etag": snapshot.etag,
        "writable": snapshot.status in {"missing", "empty", "available"},
    }


def ensure_va_employee_in_directory(
    *,
    full_name: str,
    email: str,
    phone: str,
    location: str,
    expected_revision: Any,
    expected_etag: str,
    path: Path = EMPLOYEE_DIRECTORY_FILE,
) -> Dict[str, Any]:
    """Create a VA-only central identity or enable VA for an existing one."""
    normalized_name = normalize_text(full_name)
    if not normalized_name:
        raise VaEmployeeDirectoryCommandError("ФИО обязательно.")

    snapshot = read_directory_snapshot(path)
    if snapshot.status not in {"missing", "empty", "available"}:
        raise EmployeeDirectoryStateError(
            "Центральный справочник недоступен для добавления сотрудника."
        )
    employees = copy.deepcopy((snapshot.payload or {}).get("employees") or [])
    matches = [
        employee
        for employee in employees
        if _matches_identity(employee, normalized_name)
    ]
    if len(matches) > 1:
        raise VaEmployeeDirectoryCommandError(
            "Найдено несколько центральных записей сотрудника. Уточните aliases в СУП."
        )

    if matches:
        employee = matches[0]
        membership = employee["memberships"]["va_schedule_manager"]
        if membership.get("enabled") is True:
            return {
                "created": False,
                "membership_enabled": False,
                "already_enabled": True,
                "employee_name": _preferred_va_name(employee),
                "central_full_name": normalize_text(employee.get("full_name")),
                "revision": snapshot.revision,
                "etag": snapshot.etag,
            }
        membership["enabled"] = True
        membership["order"] = None
        created = False
    else:
        employees.append(
            _new_va_directory_employee(
                full_name=normalized_name,
                email=email,
                phone=phone,
                location=location,
            )
        )
        created = True

    saved = save_employee_directory(
        employees,
        expected_revision=expected_revision,
        expected_etag=expected_etag,
        writer="sup_employee_directory",
        path=path,
    )
    return {
        "created": created,
        "membership_enabled": not created,
        "already_enabled": False,
        "employee_name": normalized_name if created else _preferred_va_name(employee),
        "central_full_name": normalized_name if created else normalize_text(employee.get("full_name")),
        "revision": saved["revision"],
        "etag": saved["etag"],
    }


def update_va_employee_in_directory(
    *,
    original_name: str,
    full_name: str,
    email: str,
    phone: str,
    location: str,
    expected_revision: Any,
    expected_etag: str,
    fields: Iterable[str] = ("va_name", "email", "phone", "location"),
    path: Path = EMPLOYEE_DIRECTORY_FILE,
) -> Dict[str, Any]:
    """Synchronize VA-owned edits of common employee fields."""
    old_name = normalize_text(original_name)
    new_name = normalize_text(full_name)
    if not old_name or not new_name:
        raise VaEmployeeDirectoryCommandError("ФИО обязательно.")

    snapshot, employees, employee = _editable_employee(old_name, path)
    managed_fields = set(fields)
    changed = False
    current_name = _preferred_va_name(employee)
    if "va_name" in managed_fields and current_name != new_name:
        _append_va_alias(employee, current_name)
        _append_va_alias(employee, new_name)
        new_source_ref = canonical_source_ref("va", "employees", new_name)
        source_refs = {
            source_ref
            for source_ref in employee.get("source_refs") or []
            if not _is_va_source_ref_for(source_ref, old_name)
        }
        source_refs.add(new_source_ref)
        employee["source_refs"] = sorted(source_refs)
        changed = True

    submitted_email = normalize_text(email).lower()
    current_emails = list(employee.get("emails") or [])
    current_primary = normalize_text(current_emails[0]).lower() if current_emails else ""
    if "email" in managed_fields and current_primary != submitted_email:
        remaining = [
            normalize_text(value).lower()
            for value in current_emails[1:]
            if normalize_text(value)
            and normalize_text(value).lower() != submitted_email
        ]
        employee["emails"] = ([submitted_email] if submitted_email else []) + remaining
        changed = True

    submitted_phone = normalize_text(phone)
    if "phone" in managed_fields and normalize_text(employee.get("phone")) != submitted_phone:
        employee["phone"] = submitted_phone
        changed = True

    submitted_location = _central_location(location)
    if (
        "location" in managed_fields
        and _central_location(employee.get("location")) != submitted_location
    ):
        employee["location"] = submitted_location
        changed = True

    if not changed:
        return {
            "changed": False,
            "employee_name": _preferred_va_name(employee),
            "revision": snapshot.revision,
            "etag": snapshot.etag,
        }

    saved = save_employee_directory(
        employees,
        expected_revision=expected_revision,
        expected_etag=expected_etag,
        writer="sup_employee_directory",
        path=path,
    )
    return {
        "changed": True,
        "employee_name": new_name,
        "revision": saved["revision"],
        "etag": saved["etag"],
    }


def disable_va_employee_in_directory(
    *,
    name: str,
    expected_revision: Any,
    expected_etag: str,
    path: Path = EMPLOYEE_DIRECTORY_FILE,
) -> Dict[str, Any]:
    """Remove only VA membership while preserving the central identity."""
    snapshot, employees, employee = _editable_employee(name, path)
    membership = employee["memberships"]["va_schedule_manager"]
    if membership.get("enabled") is not True:
        return {
            "changed": False,
            "revision": snapshot.revision,
            "etag": snapshot.etag,
        }
    membership["enabled"] = False
    membership["order"] = None
    saved = save_employee_directory(
        employees,
        expected_revision=expected_revision,
        expected_etag=expected_etag,
        writer="sup_employee_directory",
        path=path,
    )
    return {
        "changed": True,
        "revision": saved["revision"],
        "etag": saved["etag"],
    }


def _editable_employee(
    name: str,
    path: Path,
) -> tuple[Any, list[Dict[str, Any]], Dict[str, Any]]:
    normalized_name = normalize_text(name)
    snapshot = read_directory_snapshot(path)
    if snapshot.status != "available" or not snapshot.payload:
        raise EmployeeDirectoryStateError(
            "Центральный справочник недоступен для изменения сотрудника."
        )
    employees = copy.deepcopy(snapshot.payload.get("employees") or [])
    matches = [employee for employee in employees if _matches_identity(employee, normalized_name)]
    if not matches:
        raise VaEmployeeDirectoryCommandError("Сотрудник не найден в центральном справочнике.")
    if len(matches) > 1:
        raise VaEmployeeDirectoryCommandError(
            "Найдено несколько центральных записей сотрудника. Уточните aliases в СУП."
        )
    return snapshot, employees, matches[0]


def _append_va_alias(employee: Dict[str, Any], value: str) -> None:
    normalized = normalize_text(value)
    existing = {
        (normalize_text(alias.get("value")).casefold(), alias.get("type"))
        for alias in employee.get("aliases") or []
    }
    if not normalized or (normalized.casefold(), "va") in existing:
        return
    employee.setdefault("aliases", []).append(
        {"value": normalized, "type": "va", "jira_domain": ""}
    )


def _central_location(value: Any) -> str:
    normalized = normalize_text(value)
    aliases = {
        "moscow": "Москва",
        "москва": "Москва",
        "khabarovsk": "Хабаровск",
        "хабаровск": "Хабаровск",
    }
    return aliases.get(normalized.casefold(), normalized)


def _matches_identity(employee: Dict[str, Any], normalized_name: str) -> bool:
    candidate = normalized_name.casefold()
    values = {normalize_text(employee.get("full_name")).casefold()}
    values.update(
        normalize_text(alias.get("value")).casefold()
        for alias in employee.get("aliases") or []
        if alias.get("type") in {"full", "schedule", "va"}
    )
    values.update(
        normalize_text(source_ref.split(":", 2)[2]).casefold()
        for source_ref in employee.get("source_refs") or []
        if normalize_text(source_ref).startswith("va:employees:")
        and len(source_ref.split(":", 2)) == 3
    )
    return candidate in values


def _new_va_directory_employee(
    *,
    full_name: str,
    email: str,
    phone: str,
    location: str,
) -> Dict[str, Any]:
    normalized_email = normalize_text(email).lower()
    return {
        "employee_id": str(uuid4()),
        "enabled": True,
        "full_name": full_name,
        "release_name": "",
        "jira_names": {"delta": "", "sberbank": ""},
        "aliases": [],
        "emails": [normalized_email] if normalized_email else [],
        "phone": normalize_text(phone),
        "location": _central_location(location),
        "personnel_number": "",
        "memberships": {
            "release_monitor": {"enabled": False, "order": None},
            "release_zni": {"enabled": False},
            "duty_dashboard": {"enabled": False, "role": "none", "order": None},
            "release_notifications": {"enabled": False},
            "va_schedule_manager": {"enabled": True, "order": None},
        },
        "source_refs": [canonical_source_ref("va", "employees", full_name)],
    }


def _preferred_va_name(employee: Dict[str, Any]) -> str:
    source_name = next(
        (
            normalize_text(source_ref.split(":", 2)[2])
            for source_ref in employee.get("source_refs") or []
            if normalize_text(source_ref).startswith("va:employees:")
            and len(source_ref.split(":", 2)) == 3
        ),
        "",
    )
    if source_name:
        return source_name
    aliases = employee.get("aliases") or []
    return next(
        (
            normalize_text(alias.get("value"))
            for alias_type in ("va", "schedule", "full")
            for alias in aliases
            if alias.get("type") == alias_type and normalize_text(alias.get("value"))
        ),
        normalize_text(employee.get("full_name")),
    )


def _is_va_source_ref_for(source_ref: Any, name: str) -> bool:
    parts = normalize_text(source_ref).split(":", 2)
    return (
        len(parts) == 3
        and parts[0] == "va"
        and parts[1] == "employees"
        and normalize_text(parts[2]).casefold() == normalize_text(name).casefold()
    )
