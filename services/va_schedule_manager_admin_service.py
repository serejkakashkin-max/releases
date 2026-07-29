from __future__ import annotations

import copy
from typing import Any, Dict

from services.employee_directory_service import (
    EmployeeDirectoryRuntimeContext,
    load_employee_directory_context,
)
from VA.schedule_manager.repositories.competency_repository import (
    CompetencyRepository,
)
from VA.schedule_manager.repositories.employee_settings_repository import (
    DEFAULT_EMPLOYEE_SETTINGS,
    EmployeeSettingsRepository,
    effective_employee_settings,
)
from VA.schedule_manager.repositories.managed_employee_repository import (
    ManagedEmployeeRepository,
)
from VA.schedule_manager.services.competency_service import CompetencyService


def build_va_schedule_manager_admin_data(
    context: EmployeeDirectoryRuntimeContext | None = None,
) -> Dict[str, Any]:
    resolved = context or load_employee_directory_context()
    settings_snapshot = EmployeeSettingsRepository().read()
    settings_payload = settings_snapshot.payload or {}
    migration = settings_payload.get("migration") or {}
    settings_ready = (
        settings_snapshot.status == "available"
        and migration.get("status") in {"complete", "not_required"}
        and not int(migration.get("unresolved") or 0)
        and not int(migration.get("ambiguous") or 0)
        and not int(migration.get("conflicts") or 0)
    )
    explicit_settings = settings_payload.get("employees") or {}
    employees = {}
    for employee in (resolved.payload or {}).get("employees", []):
        employee_id = str(employee.get("employee_id") or "")
        if not employee_id:
            continue
        membership = (
            (employee.get("memberships") or {}).get("va_schedule_manager")
            or {}
        )
        explicit = explicit_settings.get(employee_id)
        employees[employee_id] = {
            "settings": effective_employee_settings(explicit),
            "explicit": explicit is not None,
            "editable": bool(
                settings_ready
                and resolved.status == "available"
                and employee.get("enabled")
                and membership.get("enabled")
            ),
        }

    competency_service = CompetencyService(
        CompetencyRepository(),
        ManagedEmployeeRepository(),
    )
    return {
        "success": True,
        "directory": {
            "status": resolved.status,
            "revision": resolved.revision,
            "etag": resolved.etag,
        },
        "settings": {
            "status": settings_snapshot.status,
            "revision": settings_snapshot.revision,
            "etag": settings_snapshot.etag,
            "migration_status": str(
                migration.get("status") or "required"
            ),
            "ready": settings_ready,
            "defaults": copy.deepcopy(DEFAULT_EMPLOYEE_SETTINGS),
            "employees": employees,
        },
        "competencies": competency_service.admin_snapshot(),
    }
