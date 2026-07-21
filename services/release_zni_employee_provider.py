from __future__ import annotations

import logging
import threading
from typing import Any, Dict, List, Sequence, Tuple

from services.employee_directory_repository import read_directory_snapshot
from services.employee_directory_service import (
    get_release_zni_users as get_directory_release_zni_users,
)
from services.feature_flags_service import get_employee_directory_consumer_mode


LOGGER = logging.getLogger(__name__)
_diagnostic_lock = threading.Lock()
_last_diagnostic_key: Tuple[str, str] | None = None


def get_release_zni_users(legacy_users: Sequence[str]) -> List[str]:
    """Return the effective release ZNI users with safe legacy fallback."""
    legacy_names = list(legacy_users)
    mode = get_employee_directory_consumer_mode("release_zni")
    if mode == "legacy":
        return legacy_names

    comparison = get_release_zni_comparison(legacy_names)
    _log_comparison_once(mode, comparison)
    if mode == "directory" and comparison["status"] == "available":
        directory_names = get_directory_release_zni_users()
        if directory_names:
            return directory_names
    return legacy_names


def get_release_zni_comparison(legacy_users: Sequence[str]) -> Dict[str, Any]:
    legacy_names = list(legacy_users)
    snapshot = read_directory_snapshot()
    if snapshot.status != "available":
        return {
            "matches": False,
            "status": snapshot.status,
            "reason": "employee_directory_not_available",
            "legacy_count": len(legacy_names),
            "directory_count": 0,
        }

    directory_names = get_directory_release_zni_users()
    return {
        "matches": directory_names == legacy_names,
        "status": "available",
        "reason": "exact_match" if directory_names == legacy_names else "projection_mismatch",
        "legacy_count": len(legacy_names),
        "directory_count": len(directory_names),
    }


def get_release_zni_adapter_readiness(legacy_users: Sequence[str]) -> Dict[str, Any]:
    comparison = get_release_zni_comparison(legacy_users)
    mode = get_employee_directory_consumer_mode("release_zni")
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
        "Employee directory release_zni comparison: mode=%s status=%s legacy_count=%s directory_count=%s",
        mode,
        status,
        comparison["legacy_count"],
        comparison["directory_count"],
    )
