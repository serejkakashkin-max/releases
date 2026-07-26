from __future__ import annotations

from typing import Any, Dict, List, Optional

from services.employee_directory_service import (
    EmployeeDirectoryRuntimeContext,
    EmployeeDirectoryUnavailableError,
    get_va_members,
    get_va_schedule_display_name,
    load_employee_directory_context,
)
from VA.schedule_manager.models.employee import Employee
from VA.schedule_manager.models.managed_employee import ManagedVaEmployee
from VA.schedule_manager.repositories.employee_settings_repository import (
    EmployeeSettingsRepository,
    EmployeeSettingsSnapshot,
    effective_employee_settings,
)


class VaSettingsMigrationRequiredError(RuntimeError):
    pass


def get_managed_va_employees(
    context: Optional[EmployeeDirectoryRuntimeContext] = None,
    settings_snapshot: Optional[EmployeeSettingsSnapshot] = None,
) -> List[ManagedVaEmployee]:
    resolved = context or load_employee_directory_context()
    members = get_va_members(resolved)
    settings = settings_snapshot or EmployeeSettingsRepository().read()
    if settings.status != "available" or settings.payload is None:
        raise VaSettingsMigrationRequiredError("va_settings_migration_required")
    migration = settings.payload.get("migration") or {}
    if (
        migration.get("status") not in {"complete", "not_required"}
        or int(migration.get("unresolved") or 0)
        or int(migration.get("ambiguous") or 0)
        or int(migration.get("conflicts") or 0)
    ):
        raise VaSettingsMigrationRequiredError("va_settings_migration_required")

    explicit = settings.payload.get("employees") or {}
    result: List[ManagedVaEmployee] = []
    schedule_names = set()
    for employee in members:
        employee_id = employee["employee_id"]
        effective = effective_employee_settings(explicit.get(employee_id))
        schedule_name = get_va_schedule_display_name(employee, resolved)
        key = schedule_name.casefold()
        if not schedule_name or key in schedule_names:
            raise VaSettingsMigrationRequiredError("va_schedule_identity_invalid")
        schedule_names.add(key)
        emails = list(employee.get("emails") or [])
        membership = employee["memberships"]["va_schedule_manager"]
        result.append(
            ManagedVaEmployee(
                employee_id=employee_id,
                name=schedule_name,
                email=emails[0] if emails else "",
                phone=employee.get("phone") or "",
                personnel_number=employee.get("personnel_number") or None,
                location=employee.get("location") or "moscow",
                enabled=True,
                order=int(membership["order"]),
                status=effective["status"],
                role=effective["role"],
                competencies=tuple(effective["competencies"]),
                overtime_ready=bool(effective["overtime_ready"]),
            )
        )
    return result


def managed_to_employee(value: ManagedVaEmployee) -> Employee:
    return Employee(
        employee_id=value.employee_id,
        name=value.name,
        email=value.email,
        phone=value.phone,
        status=value.status,
        personnel_number=value.personnel_number,
        role=value.role,
        location=value.location,
        competencies=value.competencies,
        overtime_ready=value.overtime_ready,
    )


def is_va_employee_directory_managed() -> bool:
    context = load_employee_directory_context()
    return context.status == "available"


def get_va_employee_directory_write_state() -> Dict[str, Any]:
    context = load_employee_directory_context()
    settings = EmployeeSettingsRepository().read()
    migration = (settings.payload or {}).get("migration") or {}
    writable = (
        context.status == "available"
        and settings.status == "available"
        and migration.get("status") in {"complete", "not_required"}
    )
    return {
        "writable": writable,
        "status": "ready" if writable else (
            "va_settings_migration_required"
            if context.status == "available"
            else f"employee_directory_{context.status}"
        ),
        "revision": context.revision,
        "etag": context.etag,
        "settings_revision": settings.revision,
        "settings_etag": settings.etag,
    }


def get_va_schedule_manager_health(
    context: Optional[EmployeeDirectoryRuntimeContext] = None,
) -> Dict[str, Any]:
    resolved = context or load_employee_directory_context()
    if resolved.status != "available":
        return {
            "status": "unavailable",
            "count": 0,
            **resolved.version_token,
        }
    try:
        members = get_managed_va_employees(resolved)
    except (EmployeeDirectoryUnavailableError, VaSettingsMigrationRequiredError) as exc:
        return {
            "status": str(exc),
            "count": 0,
            **resolved.version_token,
        }
    return {
        "status": "ready" if members else "empty_membership",
        "count": len(members),
        **resolved.version_token,
    }
