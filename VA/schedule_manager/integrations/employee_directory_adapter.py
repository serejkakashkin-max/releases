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
from VA.schedule_manager.models.schedule_grid import ScheduleGrid, ScheduleRow
from VA.schedule_manager.models.schedule_snapshot import ScheduleSnapshot, grid_to_dict
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
                location=employee.get("location") or "",
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


def project_schedule_snapshot_to_current_directory(
    snapshot: ScheduleSnapshot,
    context: Optional[EmployeeDirectoryRuntimeContext] = None,
) -> ScheduleSnapshot:
    """Project historical row names onto current Employee Directory names.

    Schedule assignments remain unchanged. Historical names continue to resolve
    through server-managed aliases, while every Schedule Manager workflow sees
    the same current name as the Employees settings page.
    """
    resolved = context or load_employee_directory_context()
    if resolved.status != "available" or not resolved.payload:
        return snapshot

    name_map: Dict[str, str] = {}
    for month in snapshot.month_schedules:
        grid = month.get("grid") if isinstance(month, dict) else None
        for row in (grid or {}).get("employees", []):
            if not isinstance(row, dict):
                continue
            historical_name = str(row.get("employee_name") or "").strip()
            if historical_name and historical_name not in name_map:
                name_map[historical_name] = _current_name_for_historical(
                    historical_name,
                    resolved,
                )

    for employee in snapshot.employees:
        if employee.name and employee.name not in name_map:
            name_map[employee.name] = _current_name_for_historical(
                employee.name,
                resolved,
            )

    if not any(source != target for source, target in name_map.items()):
        return snapshot

    projected_months = []
    for month in snapshot.month_schedules:
        projected = dict(month)
        grid = snapshot.get_month_grid(str(month["sheet_name"]))
        projected_rows = _project_schedule_rows(grid.employees, name_map)
        projected_grid = ScheduleGrid(
            title=grid.title,
            year=grid.year,
            month=grid.month,
            days=grid.days,
            employees=projected_rows,
        )
        projected["grid"] = grid_to_dict(projected_grid)
        autoplan = projected.get("autoplan")
        if isinstance(autoplan, dict):
            projected["autoplan"] = _project_autoplan_names(autoplan, name_map)
        projected_months.append(projected)

    return ScheduleSnapshot(
        employees=_project_employee_records(snapshot.employees, name_map),
        original_filename=snapshot.original_filename,
        stored_filename=snapshot.stored_filename,
        uploaded_at=snapshot.uploaded_at,
        month_schedules=projected_months,
    )


def _project_schedule_rows(
    rows: List[ScheduleRow],
    name_map: Dict[str, str],
) -> List[ScheduleRow]:
    """Rename rows without allowing two historical rows to collapse into one.

    A collision in an old month must not cancel projection for every other
    month. Only rows participating in that month's collision keep their stored
    names; assignments and row positions are always preserved.
    """
    candidates = [name_map.get(row.employee_name, row.employee_name) for row in rows]
    counts: Dict[str, int] = {}
    for candidate in candidates:
        key = candidate.casefold()
        counts[key] = counts.get(key, 0) + 1

    result = []
    for row, candidate in zip(rows, candidates):
        projected_name = row.employee_name if counts[candidate.casefold()] > 1 else candidate
        result.append(
            ScheduleRow(
                employee_name=projected_name,
                hours=row.hours,
                assignments=dict(row.assignments),
            )
        )
    return result


def _project_employee_records(
    employees: List[Employee],
    name_map: Dict[str, str],
) -> List[Employee]:
    candidates = [name_map.get(employee.name, employee.name) for employee in employees]
    counts: Dict[str, int] = {}
    for candidate in candidates:
        key = candidate.casefold()
        counts[key] = counts.get(key, 0) + 1

    return [
        Employee(
            name=(employee.name if counts[candidate.casefold()] > 1 else candidate),
            employee_id=employee.employee_id,
            email=employee.email,
            phone=employee.phone,
            status=employee.status,
            personnel_number=employee.personnel_number,
            role=employee.role,
            location=employee.location,
            competencies=employee.competencies,
            overtime_ready=employee.overtime_ready,
        )
        for employee, candidate in zip(employees, candidates)
    ]


def _current_name_for_historical(
    historical_name: str,
    context: EmployeeDirectoryRuntimeContext,
) -> str:
    from services.employee_directory_service import (
        get_va_schedule_current_display_name,
        resolve_historical_va_employee,
    )

    identity = resolve_historical_va_employee(historical_name, context)
    if identity.status != "resolved" or not identity.employee:
        return historical_name
    return (
        get_va_schedule_current_display_name(identity.employee, context)
        or historical_name
    )


def _project_autoplan_names(value: Dict[str, Any], name_map: Dict[str, str]) -> Dict[str, Any]:
    projected = dict(value)
    explanations = []
    for raw in value.get("assignment_explanations") or []:
        if not isinstance(raw, dict):
            explanations.append(raw)
            continue
        item = dict(raw)
        employee_name = str(item.get("employee_name") or "")
        item["employee_name"] = name_map.get(employee_name, employee_name)
        explanations.append(item)
    if "assignment_explanations" in value:
        projected["assignment_explanations"] = explanations
    return projected


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
