from __future__ import annotations

import copy
from pathlib import Path
from typing import Any, Dict
from uuid import uuid4

from services.employee_directory_repository import (
    EmployeeDirectoryStateError,
    EMPLOYEE_DIRECTORY_FILE,
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

    save_employee_directory(
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
    }


def _matches_identity(employee: Dict[str, Any], normalized_name: str) -> bool:
    candidate = normalized_name.casefold()
    values = {normalize_text(employee.get("full_name")).casefold()}
    values.update(
        normalize_text(alias.get("value")).casefold()
        for alias in employee.get("aliases") or []
        if alias.get("type") in {"full", "schedule", "va"}
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
        "location": normalize_text(location),
        "personnel_number": "",
        "memberships": {
            "release_monitor": {"enabled": False, "order": None},
            "release_zni": {"enabled": False},
            "duty_dashboard": {"enabled": False, "role": "none", "order": None},
            "release_notifications": {"enabled": False},
            "va_schedule_manager": {"enabled": True, "order": None},
        },
        "source_refs": [],
    }


def _preferred_va_name(employee: Dict[str, Any]) -> str:
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
