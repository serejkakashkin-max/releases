from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict, List, Tuple

from config import OPLOT_VALUES
from services.employee_directory_repository import read_directory_snapshot
from services.employee_directory_service import (
    get_release_monitor_names as get_directory_release_monitor_names,
)
from services.feature_flags_service import get_employee_directory_consumer_mode


LOGGER = logging.getLogger(__name__)
_diagnostic_lock = threading.Lock()
_last_diagnostic_key: Tuple[str, str] | None = None
_effective_names_lock = threading.Lock()
_effective_names_cache: Tuple[str, ...] | None = None
_effective_names_cache_until = 0.0
_EFFECTIVE_NAMES_CACHE_SECONDS = 1.0


def get_release_monitor_names() -> List[str]:
    """Return the effective release list with safe legacy fallback."""
    global _effective_names_cache, _effective_names_cache_until

    now = time.monotonic()
    with _effective_names_lock:
        if _effective_names_cache is not None and now < _effective_names_cache_until:
            return list(_effective_names_cache)

        names = _resolve_release_monitor_names()
        _effective_names_cache = tuple(names)
        _effective_names_cache_until = time.monotonic() + _EFFECTIVE_NAMES_CACHE_SECONDS
        return list(_effective_names_cache)


def invalidate_release_monitor_employee_cache() -> None:
    global _effective_names_cache, _effective_names_cache_until
    with _effective_names_lock:
        _effective_names_cache = None
        _effective_names_cache_until = 0.0


def _resolve_release_monitor_names() -> List[str]:
    legacy_names = list(OPLOT_VALUES)
    mode = get_employee_directory_consumer_mode("release_monitor")
    if mode == "legacy":
        return legacy_names

    snapshot = read_directory_snapshot()
    comparison, directory_names = _build_comparison(snapshot, legacy_names)
    _log_comparison_once(mode, comparison)
    if mode == "directory" and comparison["status"] == "available":
        if directory_names:
            return directory_names
    return legacy_names


def get_release_monitor_comparison() -> Dict[str, Any]:
    legacy_names = list(OPLOT_VALUES)
    snapshot = read_directory_snapshot()
    comparison, _directory_names = _build_comparison(snapshot, legacy_names)
    return comparison


def _build_comparison(snapshot, legacy_names: List[str]):
    if snapshot.status != "available":
        return (
            {
                "matches": False,
                "status": snapshot.status,
                "reason": "employee_directory_not_available",
                "legacy_count": len(legacy_names),
                "directory_count": 0,
            },
            [],
        )

    directory_names = get_directory_release_monitor_names(snapshot=snapshot)
    return (
        {
            "matches": directory_names == legacy_names,
            "status": "available",
            "reason": "exact_match" if directory_names == legacy_names else "projection_mismatch",
            "legacy_count": len(legacy_names),
            "directory_count": len(directory_names),
        },
        directory_names,
    )


def get_release_monitor_adapter_readiness() -> Dict[str, Any]:
    comparison = get_release_monitor_comparison()
    mode = get_employee_directory_consumer_mode("release_monitor")
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
        "Employee directory release_monitor comparison: mode=%s status=%s legacy_count=%s directory_count=%s",
        mode,
        status,
        comparison["legacy_count"],
        comparison["directory_count"],
    )
