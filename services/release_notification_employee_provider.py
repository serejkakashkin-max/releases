from __future__ import annotations

from typing import Any, Dict, List, Optional

from services.employee_directory_service import (
    EmployeeDirectoryRuntimeContext,
    EmployeeDirectoryUnavailableError,
    get_release_notification_recipients as _get_recipients,
    load_employee_directory_context,
    resolve_employee_identity,
)


def get_release_notification_recipients(
    context: Optional[EmployeeDirectoryRuntimeContext] = None,
) -> Dict[str, List[str]]:
    return _get_recipients(context)


def resolve_release_notification_emails(
    value: Any,
    context: Optional[EmployeeDirectoryRuntimeContext] = None,
) -> Dict[str, Any]:
    resolved = context or load_employee_directory_context()
    if resolved.status != "available":
        return {
            "status": "unavailable",
            "reason": f"employee_directory_{resolved.status}",
            "emails": [],
        }
    result = resolve_employee_identity(
        value,
        context=resolved,
        identity_type="release",
        include_disabled_for_history=True,
    )
    if result.status == "unresolved":
        result = resolve_employee_identity(
            value,
            context=resolved,
            include_disabled_for_history=True,
        )
    if result.status != "resolved":
        return {"status": result.status, "reason": result.status, "emails": []}
    employee = result.employee or {}
    if not employee.get("enabled"):
        return {"status": "disabled", "reason": "employee_disabled", "emails": []}
    membership = (employee.get("memberships") or {}).get("release_notifications") or {}
    if not membership.get("enabled"):
        return {"status": "disabled", "reason": "membership_disabled", "emails": []}
    emails = list(employee.get("emails") or [])
    if not emails:
        return {"status": "no_email", "reason": "no_email", "emails": []}
    return {
        "status": "resolved",
        "reason": "",
        "employee_id": result.employee_id,
        "emails": emails,
    }
