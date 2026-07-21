from __future__ import annotations

import logging
import threading
from typing import Any, Dict, List, Tuple

from config import (
    DASHBOARD_ASSIGNEES,
    DASHBOARD_ASSIGNEES_DISPLAY,
    DASHBOARD_EXTRA_ASSIGNEES,
    DASHBOARD_VISIBLE_ASSIGNEES,
    DASHBOARD_VISIBLE_ASSIGNEES_DISPLAY,
    get_dashboard_assignee_display_name as get_legacy_dashboard_display_name,
)
from services.employee_directory_repository import read_directory_snapshot
from services.employee_directory_service import (
    get_dashboard_display_names as get_directory_primary_display_names,
    get_dashboard_extra_display_names as get_directory_extra_display_names,
    get_dashboard_extra_jira_names as get_directory_extra_jira_names,
    get_dashboard_primary_jira_names as get_directory_primary_jira_names,
    get_dashboard_visible_display_names as get_directory_visible_display_names,
    get_dashboard_visible_jira_names as get_directory_visible_jira_names,
)
from services.feature_flags_service import get_employee_directory_consumer_mode


LOGGER = logging.getLogger(__name__)
_diagnostic_lock = threading.Lock()
_last_diagnostic_key: Tuple[str, str] | None = None


def get_dashboard_primary_jira_names() -> List[str]:
    return _effective_projection()["primary_jira"]


def get_dashboard_extra_jira_names() -> List[str]:
    return _effective_projection()["extra_jira"]


def get_dashboard_visible_jira_names() -> List[str]:
    return _effective_projection()["visible_jira"]


def get_dashboard_primary_display_names() -> List[str]:
    return _effective_projection()["primary_display"]


def get_dashboard_visible_display_names() -> List[str]:
    return _effective_projection()["visible_display"]


def get_dashboard_assignee_display_name(name: str) -> str:
    if not name:
        return name
    projection = _effective_projection()
    display_map = dict(zip(projection["visible_jira"], projection["visible_display"]))
    return display_map.get(name, get_legacy_dashboard_display_name(name))


def get_duty_dashboard_comparison() -> Dict[str, Any]:
    legacy = _legacy_projection()
    snapshot = read_directory_snapshot()
    if snapshot.status != "available":
        return {
            "matches": False,
            "status": snapshot.status,
            "reason": "employee_directory_not_available",
            "legacy_count": len(legacy["visible_jira"]),
            "directory_count": 0,
            "checks": {},
        }

    directory = _directory_projection()
    checks = {key: directory[key] == legacy[key] for key in legacy}
    matches = all(checks.values())
    return {
        "matches": matches,
        "status": "available",
        "reason": "exact_match" if matches else "projection_mismatch",
        "legacy_count": len(legacy["visible_jira"]),
        "directory_count": len(directory["visible_jira"]),
        "checks": checks,
    }


def get_duty_dashboard_adapter_readiness() -> Dict[str, Any]:
    comparison = get_duty_dashboard_comparison()
    mode = get_employee_directory_consumer_mode("duty_dashboard")
    if comparison["status"] != "available" or comparison["directory_count"] == 0:
        return {
            "ready": False,
            "reason": comparison["reason"],
            "allowed_modes": ["legacy"],
            "comparison": comparison,
        }

    if mode == "directory":
        return {
            "ready": True,
            "reason": "directory_active",
            "allowed_modes": ["legacy", "compare", "directory"],
            "comparison": comparison,
        }

    exact_match = bool(comparison["matches"])
    allowed_modes = ["legacy", "compare"]
    reason = "compare_ready" if exact_match else comparison["reason"]
    if mode == "compare" and exact_match:
        allowed_modes.append("directory")
        reason = "directory_ready"
    return {
        "ready": exact_match,
        "reason": reason,
        "allowed_modes": allowed_modes if exact_match else ["legacy"],
        "comparison": comparison,
    }


def get_duty_dashboard_projection_token() -> str:
    if get_employee_directory_consumer_mode("duty_dashboard") != "directory":
        return "legacy"
    snapshot = read_directory_snapshot()
    if snapshot.status != "available":
        return "legacy:fallback"
    directory = _directory_projection()
    if not directory["primary_jira"]:
        return "legacy:fallback"
    return f"directory:{snapshot.etag}"


def _effective_projection() -> Dict[str, List[str]]:
    legacy = _legacy_projection()
    mode = get_employee_directory_consumer_mode("duty_dashboard")
    if mode == "legacy":
        return legacy
    comparison = get_duty_dashboard_comparison()
    _log_comparison_once(mode, comparison)
    if mode == "directory" and comparison["status"] == "available":
        directory = _directory_projection()
        if directory["primary_jira"]:
            return directory
    return legacy


def _legacy_projection() -> Dict[str, List[str]]:
    return {
        "primary_jira": list(DASHBOARD_ASSIGNEES),
        "extra_jira": list(DASHBOARD_EXTRA_ASSIGNEES),
        "visible_jira": list(DASHBOARD_VISIBLE_ASSIGNEES),
        "primary_display": list(DASHBOARD_ASSIGNEES_DISPLAY),
        "visible_display": list(DASHBOARD_VISIBLE_ASSIGNEES_DISPLAY),
    }


def _directory_projection() -> Dict[str, List[str]]:
    return {
        "primary_jira": get_directory_primary_jira_names(),
        "extra_jira": get_directory_extra_jira_names(),
        "visible_jira": get_directory_visible_jira_names(),
        "primary_display": get_directory_primary_display_names(),
        "visible_display": get_directory_visible_display_names(),
    }


def _log_comparison_once(mode: str, comparison: Dict[str, Any]) -> None:
    global _last_diagnostic_key
    if mode == "directory" and comparison["status"] == "available":
        status = "directory_active"
    else:
        status = "match" if comparison["matches"] else comparison["reason"]
    key = (mode, status)
    with _diagnostic_lock:
        if key == _last_diagnostic_key:
            return
        _last_diagnostic_key = key

    log = LOGGER.info if comparison["matches"] or status == "directory_active" else LOGGER.warning
    log(
        "Employee directory duty_dashboard comparison: mode=%s status=%s legacy_count=%s directory_count=%s",
        mode,
        status,
        comparison["legacy_count"],
        comparison["directory_count"],
    )
