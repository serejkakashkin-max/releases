from __future__ import annotations

from typing import Any, Dict, List, Optional

from services.employee_directory_service import (
    EmployeeDirectoryRuntimeContext,
    EmployeeDirectoryUnavailableError,
    get_release_monitor_projection as _get_projection,
    load_employee_directory_context,
)


def get_release_monitor_projection(
    context: Optional[EmployeeDirectoryRuntimeContext] = None,
) -> Dict[str, Any]:
    resolved = context or load_employee_directory_context()
    try:
        projection = _get_projection(resolved)
        return {
            **projection,
            "employee_selection_available": projection["status"] == "ready",
            "employee_selection_reason": (
                "" if projection["status"] == "ready"
                else "employee_directory_empty_membership"
            ),
        }
    except EmployeeDirectoryUnavailableError as exc:
        return {
            "status": exc.status,
            "revision": resolved.revision,
            "etag": resolved.etag,
            "count": 0,
            "names": [],
            "employee_selection_available": False,
            "employee_selection_reason": f"employee_directory_{exc.status}",
        }


def get_release_monitor_names(
    context: Optional[EmployeeDirectoryRuntimeContext] = None,
) -> List[str]:
    return list(get_release_monitor_projection(context)["names"])


def invalidate_release_monitor_employee_cache() -> None:
    # Projections are derived from a fresh ETag-scoped context and keep no fallback cache.
    return None
