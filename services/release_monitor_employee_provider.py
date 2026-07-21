from __future__ import annotations

import logging
import threading
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


def get_release_monitor_names() -> List[str]:
    """Return the effective release list without changing legacy behavior in compare mode."""
    legacy_names = list(OPLOT_VALUES)
    mode = get_employee_directory_consumer_mode("release_monitor")
    if mode == "legacy":
        return legacy_names

    comparison = get_release_monitor_comparison()
    _log_comparison_once(mode, comparison)

    # Directory activation is a separate rollout step. A manual feature flag edit
    # must not switch production behavior before that step is implemented.
    return legacy_names


def get_release_monitor_comparison() -> Dict[str, Any]:
    legacy_names = list(OPLOT_VALUES)
    snapshot = read_directory_snapshot()
    if snapshot.status != "available":
        return {
            "matches": False,
            "status": snapshot.status,
            "reason": "employee_directory_not_available",
            "legacy_count": len(legacy_names),
            "directory_count": 0,
        }

    directory_names = get_directory_release_monitor_names()
    return {
        "matches": directory_names == legacy_names,
        "status": "available",
        "reason": "exact_match" if directory_names == legacy_names else "projection_mismatch",
        "legacy_count": len(legacy_names),
        "directory_count": len(directory_names),
    }


def get_release_monitor_adapter_readiness() -> Dict[str, Any]:
    comparison = get_release_monitor_comparison()
    ready = bool(comparison["matches"])
    return {
        "ready": ready,
        "reason": "compare_ready" if ready else comparison["reason"],
        "allowed_modes": ["legacy", "compare"] if ready else ["legacy"],
        "comparison": comparison,
    }


def _log_comparison_once(mode: str, comparison: Dict[str, Any]) -> None:
    global _last_diagnostic_key
    status = "match" if comparison["matches"] else comparison["reason"]
    key = (mode, status)
    with _diagnostic_lock:
        if key == _last_diagnostic_key:
            return
        _last_diagnostic_key = key

    log = LOGGER.info if comparison["matches"] else LOGGER.warning
    log(
        "Employee directory release_monitor comparison: mode=%s status=%s legacy_count=%s directory_count=%s",
        mode,
        status,
        comparison["legacy_count"],
        comparison["directory_count"],
    )
