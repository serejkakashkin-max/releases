from __future__ import annotations

import logging
import threading
from typing import Any, Dict, Iterable, List, Mapping, Tuple

from services.employee_directory_repository import normalize_text, read_directory_snapshot
from services.employee_directory_service import (
    get_release_notification_recipients as get_directory_release_notification_recipients,
)
from services.feature_flags_service import get_employee_directory_consumer_mode


LOGGER = logging.getLogger(__name__)
_diagnostic_lock = threading.Lock()
_last_diagnostic_key: Tuple[str, str] | None = None


def get_release_notification_recipients(
    legacy_recipients: Mapping[str, Iterable[str]],
) -> Dict[str, List[str]]:
    """Return effective release recipients with safe legacy fallback."""
    legacy_map = _copy_recipients(legacy_recipients)
    mode = get_employee_directory_consumer_mode("release_notifications")
    if mode == "legacy":
        return legacy_map

    comparison = get_release_notification_comparison(legacy_map)
    _log_comparison_once(mode, comparison)
    if mode == "directory" and comparison["status"] == "available":
        directory_map = get_directory_release_notification_recipients()
        if directory_map:
            return _copy_recipients(directory_map)
    return legacy_map


def get_release_notification_comparison(
    legacy_recipients: Mapping[str, Iterable[str]],
) -> Dict[str, Any]:
    legacy_map = _copy_recipients(legacy_recipients)
    snapshot = read_directory_snapshot()
    if snapshot.status != "available":
        return {
            "matches": False,
            "status": snapshot.status,
            "reason": "employee_directory_not_available",
            "legacy_count": len(legacy_map),
            "directory_count": 0,
        }

    directory_map = get_directory_release_notification_recipients()
    matches = _canonical_recipients(legacy_map) == _canonical_recipients(directory_map)
    return {
        "matches": matches,
        "status": "available",
        "reason": "exact_match" if matches else "projection_mismatch",
        "legacy_count": len(legacy_map),
        "directory_count": len(directory_map),
    }


def get_release_notification_adapter_readiness(
    legacy_recipients: Mapping[str, Iterable[str]],
) -> Dict[str, Any]:
    comparison = get_release_notification_comparison(legacy_recipients)
    mode = get_employee_directory_consumer_mode("release_notifications")
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


def _copy_recipients(values: Mapping[str, Iterable[str]]) -> Dict[str, List[str]]:
    return {
        str(name): [str(email) for email in emails]
        for name, emails in values.items()
    }


def _canonical_recipients(values: Mapping[str, Iterable[str]]) -> Dict[str, List[str]]:
    return {
        normalize_text(name).casefold(): sorted(
            {
                normalize_text(email).lower()
                for email in emails
                if normalize_text(email)
            }
        )
        for name, emails in values.items()
        if normalize_text(name)
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
        "Employee directory release_notifications comparison: mode=%s status=%s legacy_count=%s directory_count=%s",
        mode,
        status,
        comparison["legacy_count"],
        comparison["directory_count"],
    )
