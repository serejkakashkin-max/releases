import copy
import json
import logging
import threading
from pathlib import Path
from typing import Any, Dict, List


FEATURE_FLAGS_FILE = Path(__file__).resolve().parent.parent / "feature_flags.json"

DEFAULT_FEATURE_FLAGS = {
    "maintenance": {
        "index": False,
        "release_monitor": False,
        "duty_dashboard": False,
        "chatbot": False,
    },
    "automation": {
        "release_monitor_unassigned_email": {
            "enabled": False,
            "recipients": [],
        },
        "release_monitor_responsible_email": {
            "enabled": False,
            "employee_recipients": {},
            "weekly_digest_enabled": True,
            "weekly_digest_time": "16:00",
            "assignment_email_delay_minutes": 6,
            "personal_email_send_interval_seconds": 5,
        },
    },
}

_flags_lock = threading.RLock()
_cached_flags = copy.deepcopy(DEFAULT_FEATURE_FLAGS)
_cached_mtime_ns = None
_last_load_error_key = None


def _normalize_string_list(value: Any) -> List[str]:
    values = value if isinstance(value, (list, tuple, set)) else [value]
    normalized = []
    for item in values:
        clean_item = str(item or "").strip()
        if clean_item:
            normalized.append(clean_item)
    return normalized


def _normalize_non_negative_int(value: Any, default: int) -> int:
    if isinstance(value, bool):
        return default
    try:
        normalized = int(value)
    except (TypeError, ValueError):
        return default
    return normalized if normalized >= 0 else default


def _normalize_flags(payload: Any) -> Dict[str, Dict[str, Any]]:
    payload = payload if isinstance(payload, dict) else {}
    normalized = copy.deepcopy(DEFAULT_FEATURE_FLAGS)

    maintenance = payload.get("maintenance")
    if isinstance(maintenance, dict):
        for key in DEFAULT_FEATURE_FLAGS["maintenance"]:
            if isinstance(maintenance.get(key), bool):
                normalized["maintenance"][key] = maintenance[key]

    automation = payload.get("automation")
    if isinstance(automation, dict):
        email_source = automation.get("release_monitor_unassigned_email")
        if isinstance(email_source, dict):
            if isinstance(email_source.get("enabled"), bool):
                normalized["automation"]["release_monitor_unassigned_email"]["enabled"] = (
                    email_source["enabled"]
                )
            recipients = email_source.get("recipients")
            if isinstance(recipients, list):
                normalized["automation"]["release_monitor_unassigned_email"]["recipients"] = [
                    str(value or "").strip()
                    for value in recipients
                    if str(value or "").strip()
                ]
        responsible_email_source = automation.get("release_monitor_responsible_email")
        if isinstance(responsible_email_source, dict):
            target = normalized["automation"]["release_monitor_responsible_email"]
            if isinstance(responsible_email_source.get("enabled"), bool):
                target["enabled"] = responsible_email_source["enabled"]
            employee_recipients = responsible_email_source.get("employee_recipients")
            if isinstance(employee_recipients, dict):
                normalized_recipients = {}
                for name, addresses in employee_recipients.items():
                    clean_name = str(name or "").strip()
                    clean_addresses = _normalize_string_list(addresses)
                    if clean_name and clean_addresses:
                        normalized_recipients[clean_name] = clean_addresses
                target["employee_recipients"] = normalized_recipients
            if isinstance(responsible_email_source.get("weekly_digest_enabled"), bool):
                target["weekly_digest_enabled"] = responsible_email_source[
                    "weekly_digest_enabled"
                ]
            weekly_digest_time = str(
                responsible_email_source.get("weekly_digest_time") or ""
            ).strip()
            if weekly_digest_time:
                target["weekly_digest_time"] = weekly_digest_time
            target["assignment_email_delay_minutes"] = _normalize_non_negative_int(
                responsible_email_source.get("assignment_email_delay_minutes"),
                DEFAULT_FEATURE_FLAGS["automation"]["release_monitor_responsible_email"][
                    "assignment_email_delay_minutes"
                ],
            )
            target["personal_email_send_interval_seconds"] = _normalize_non_negative_int(
                responsible_email_source.get("personal_email_send_interval_seconds"),
                DEFAULT_FEATURE_FLAGS["automation"]["release_monitor_responsible_email"][
                    "personal_email_send_interval_seconds"
                ],
            )
    return normalized


def _load_flags_if_changed() -> None:
    global _cached_flags, _cached_mtime_ns, _last_load_error_key

    try:
        mtime_ns = FEATURE_FLAGS_FILE.stat().st_mtime_ns
    except OSError as exc:
        error_key = ("missing", type(exc).__name__, str(exc))
        if error_key != _last_load_error_key:
            logging.warning(
                "Feature flags: %s is unavailable; safe defaults are used: %s",
                FEATURE_FLAGS_FILE,
                exc,
            )
            _last_load_error_key = error_key
        _cached_flags = copy.deepcopy(DEFAULT_FEATURE_FLAGS)
        _cached_mtime_ns = None
        return

    if _cached_mtime_ns == mtime_ns:
        return

    try:
        with FEATURE_FLAGS_FILE.open("r", encoding="utf-8-sig") as handle:
            payload = json.load(handle)
        _cached_flags = _normalize_flags(payload)
        _cached_mtime_ns = mtime_ns
        _last_load_error_key = None
        logging.info("Feature flags: loaded %s", FEATURE_FLAGS_FILE)
    except Exception as exc:
        error_key = (mtime_ns, type(exc).__name__, str(exc))
        if error_key != _last_load_error_key:
            logging.error(
                "Feature flags: failed to read %s; safe defaults are used: %s",
                FEATURE_FLAGS_FILE,
                exc,
            )
            _last_load_error_key = error_key
        _cached_flags = copy.deepcopy(DEFAULT_FEATURE_FLAGS)
        _cached_mtime_ns = mtime_ns


def get_feature_flags() -> Dict[str, Dict[str, Any]]:
    with _flags_lock:
        _load_flags_if_changed()
        return copy.deepcopy(_cached_flags)


def is_feature_enabled(section: str, key: str) -> bool:
    flags = get_feature_flags()
    return bool((flags.get(section) or {}).get(key, False))


def is_maintenance_enabled(scope: str) -> bool:
    return is_feature_enabled("maintenance", scope)


def is_automation_enabled(name: str) -> bool:
    config = get_automation_config(name)
    if isinstance(config, dict):
        return bool(config.get("enabled", False))
    return bool(config)


def get_automation_config(name: str) -> Any:
    flags = get_feature_flags()
    return copy.deepcopy((flags.get("automation") or {}).get(name, False))
