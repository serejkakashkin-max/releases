from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from services.employee_directory_service import (
    EmployeeDirectoryRuntimeContext,
    EmployeeDirectoryUnavailableError,
    get_dashboard_projection as _get_dashboard_projection,
    load_employee_directory_context,
    resolve_employee_identity,
)


def get_duty_dashboard_projection(
    context: Optional[EmployeeDirectoryRuntimeContext] = None,
) -> Dict[str, Any]:
    resolved = context or load_employee_directory_context()
    try:
        return _get_dashboard_projection(resolved)
    except EmployeeDirectoryUnavailableError as exc:
        return {
            "status": "unavailable",
            "reason": f"employee_directory_{exc.status}",
            "primary_jira": [],
            "extra_jira": [],
            "visible_jira": [],
            "primary_display": [],
            "extra_display": [],
            "visible_display": [],
            **resolved.version_token,
        }


def get_dashboard_primary_jira_names() -> List[str]:
    return list(get_duty_dashboard_projection()["primary_jira"])


def get_dashboard_extra_jira_names() -> List[str]:
    return list(get_duty_dashboard_projection()["extra_jira"])


def get_dashboard_visible_jira_names() -> List[str]:
    return list(get_duty_dashboard_projection()["visible_jira"])


def get_dashboard_primary_display_names() -> List[str]:
    return list(get_duty_dashboard_projection()["primary_display"])


def get_dashboard_visible_display_names() -> List[str]:
    return list(get_duty_dashboard_projection()["visible_display"])


def get_dashboard_assignee_display_name(
    name: str,
    context: Optional[EmployeeDirectoryRuntimeContext] = None,
) -> str:
    if not name:
        return name
    resolved = context or load_employee_directory_context()
    if resolved.status != "available":
        return name
    result = resolve_employee_identity(
        name,
        context=resolved,
        identity_type="jira",
        jira_domain="delta",
        include_disabled_for_history=True,
    )
    if result.status == "unresolved":
        result = resolve_employee_identity(
            name,
            context=resolved,
            include_disabled_for_history=True,
        )
    return (
        str((result.employee or {}).get("full_name") or name)
        if result.status == "resolved"
        else name
    )


def get_duty_dashboard_projection_token() -> str:
    projection = get_duty_dashboard_projection()
    return json.dumps(
        {
            "status": projection["status"],
            "revision": projection.get("revision"),
            "etag": projection.get("etag"),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
