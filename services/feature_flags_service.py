import copy
import json
import logging
import threading
from pathlib import Path
from typing import Any, Dict


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
    },
}

_flags_lock = threading.RLock()
_cached_flags = copy.deepcopy(DEFAULT_FEATURE_FLAGS)
_cached_mtime_ns = None
_last_load_error_key = None


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
