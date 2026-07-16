import copy
import json
import logging
import re
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional


FEATURE_FLAGS_FILE = Path(__file__).resolve().parent.parent / "feature_flags.json"

JIRA_DOMAIN_CONFIGS = {
    "sberbank": {
        "url": "https://jira.sberbank.ru",
        "token_key": "sberbank_token",
    },
    "delta": {
        "url": "https://jira.delta.sbrf.ru",
        "token_key": "delta_token",
    },
}

DEFAULT_RELEASE_PREFIX_CONFIGS = [
    {
        "prefix": "EMRM",
        "enabled": True,
        "jira_domain": "sberbank",
        "system": "EMRM",
    },
    {
        "prefix": "SMECLM",
        "enabled": True,
        "jira_domain": "sberbank",
        "system": "CLM",
    },
    {
        "prefix": "SMECSC",
        "enabled": True,
        "jira_domain": "delta",
        "system": "РђРРЎРў",
    },
    {
        "prefix": "HELPERAI",
        "enabled": True,
        "jira_domain": "delta",
        "system": "AI-РђРіРµРЅС‚С‹",
    },
    {
        "prefix": "AIGAS",
        "enabled": True,
        "jira_domain": "delta",
        "system": "AI-РђРіРµРЅС‚С‹",
    },
    {
        "prefix": "DRMMMB",
        "enabled": True,
        "jira_domain": "sberbank",
        "system": "AI-РђРіРµРЅС‚С‹",
    },
]

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
            "weekly_reminder_enabled": False,
            "weekly_reminder_time": "09:00",
            "weekly_reminder_recipients": [],
        },
        "release_monitor_responsible_email": {
            "enabled": False,
            "employee_recipients": {},
            "weekly_digest_enabled": True,
            "weekly_digest_time": "16:00",
            "weekly_digest_recipients": [],
            "assignment_email_delay_minutes": 6,
            "personal_email_send_interval_seconds": 5,
        },
        "email_to_sbertrack": {
            "enabled": False,
            "dry_run": True,
            "poll_interval_seconds": 300,
            "lookback_limit": 20,
            "max_pending_per_cycle": 10,
            "body_max_chars": 6000,
            "technical_mailboxes": [],
            "routes": [],
        },
    },
    "release_monitor": {
        "prefixes": copy.deepcopy(DEFAULT_RELEASE_PREFIX_CONFIGS),
    },
    "modules": {
        "va_schedule_manager": {
            "enabled": False,
        },
    },
    "sbertrack_users": {},
}

PREFIX_PATTERN = re.compile(r"^[A-Z0-9_]+$")

_flags_lock = threading.RLock()
_cached_flags = copy.deepcopy(DEFAULT_FEATURE_FLAGS)
_cached_mtime_ns = None
_last_load_error_key = None


def _normalize_string_list(value: Any) -> List[str]:
    values = value if isinstance(value, (list, tuple, set)) else [value]
    normalized = []
    seen = set()
    for item in values:
        clean_item = str(item or "").strip()
        lowered = clean_item.lower()
        if clean_item and lowered not in seen:
            normalized.append(clean_item)
            seen.add(lowered)
    return normalized


def _normalize_non_negative_int(value: Any, default: int) -> int:
    if isinstance(value, bool):
        return default
    try:
        normalized = int(value)
    except (TypeError, ValueError):
        return default
    return normalized if normalized >= 0 else default


def _normalize_employee_recipients(value: Any, *, enabled_only: bool) -> Dict[str, List[str]]:
    normalized = {}
    if not isinstance(value, dict):
        return normalized

    for name, raw_config in value.items():
        clean_name = str(name or "").strip()
        if not clean_name:
            continue

        enabled = True
        raw_emails = raw_config
        if isinstance(raw_config, dict):
            if isinstance(raw_config.get("enabled"), bool):
                enabled = raw_config["enabled"]
            raw_emails = raw_config.get("emails")

        if enabled_only and not enabled:
            continue

        emails = _normalize_string_list(raw_emails)
        if emails:
            normalized[clean_name] = emails

    return normalized


def _normalize_prefix_entries(value: Any) -> List[Dict[str, Any]]:
    if not isinstance(value, list):
        return copy.deepcopy(DEFAULT_RELEASE_PREFIX_CONFIGS)

    normalized = []
    seen = set()
    for raw_entry in value:
        if not isinstance(raw_entry, dict):
            continue
        prefix = str(raw_entry.get("prefix") or "").strip().upper()
        if not prefix or not PREFIX_PATTERN.match(prefix) or prefix in seen:
            continue
        jira_domain = str(raw_entry.get("jira_domain") or "").strip().lower()
        if jira_domain not in JIRA_DOMAIN_CONFIGS:
            continue
        system = str(raw_entry.get("system") or "").strip()
        if not system:
            continue
        enabled = raw_entry.get("enabled")
        normalized.append(
            {
                "prefix": prefix,
                "enabled": enabled if isinstance(enabled, bool) else True,
                "jira_domain": jira_domain,
                "system": system,
            }
        )
        seen.add(prefix)
    return normalized


def _normalize_email_to_sbertrack_config(value: Any) -> Dict[str, Any]:
    source = value if isinstance(value, dict) else {}
    target = copy.deepcopy(DEFAULT_FEATURE_FLAGS["automation"]["email_to_sbertrack"])
    if isinstance(source.get("enabled"), bool):
        target["enabled"] = source["enabled"]
    if isinstance(source.get("dry_run"), bool):
        target["dry_run"] = source["dry_run"]
    target["poll_interval_seconds"] = _normalize_non_negative_int(
        source.get("poll_interval_seconds"),
        target["poll_interval_seconds"],
    )
    target["lookback_limit"] = max(
        1,
        _normalize_non_negative_int(
            source.get("lookback_limit"),
            target["lookback_limit"],
        ),
    )
    target["max_pending_per_cycle"] = max(
        1,
        _normalize_non_negative_int(
            source.get("max_pending_per_cycle"),
            target["max_pending_per_cycle"],
        ),
    )
    target["body_max_chars"] = max(
        1000,
        _normalize_non_negative_int(
            source.get("body_max_chars"),
            target["body_max_chars"],
        ),
    )
    target["technical_mailboxes"] = [
        value.lower() for value in _normalize_string_list(source.get("technical_mailboxes"))
    ]
    routes = []
    for index, raw_route in enumerate(source.get("routes") or [], start=1):
        if not isinstance(raw_route, dict):
            continue
        name = str(raw_route.get("name") or f"route_{index}").strip()
        triggers = _normalize_string_list(raw_route.get("subject_triggers"))
        target_system = str(raw_route.get("target_system") or "sbertrack").strip().lower()
        if target_system not in {"sbertrack", "jira"}:
            target_system = "sbertrack"
        spaces = _normalize_string_list(
            raw_route.get("jira_projects") if target_system == "jira" else raw_route.get("spaces")
        )
        suit = str(raw_route.get("suit") or "task").strip()
        priority = str(raw_route.get("priority") or "low").strip()
        summary_template = str(raw_route.get("summary_template") or "{subject}").strip()
        if not name or not triggers or not spaces:
            continue
        jira_issue_type = str(raw_route.get("jira_issue_type") or "Story").strip() or "Story"
        jira_issue_type_id = str(raw_route.get("jira_issue_type_id") or "").strip()
        jira_epic_name_field = str(raw_route.get("jira_epic_name_field") or "").strip()
        raw_team = raw_route.get("jira_team") if isinstance(raw_route.get("jira_team"), dict) else {}
        is_emrm_route = target_system == "jira" and any(
            str(item).strip().upper() == "EMRM" for item in spaces
        )
        is_legacy_emrm_story = (
            is_emrm_route
            and jira_issue_type.lower() == "story"
            and str(raw_team.get("value_id") or "").strip() == "4681"
        )
        if is_legacy_emrm_story:
            name = "EMRM"
            triggers = ["EMRM"]
            summary_template = "{subject}"
            jira_issue_type = "Task"
            jira_issue_type_id = "3"
            jira_epic_name_field = ""
            raw_team = {
                "field_id": "customfield_11902",
                "value_id": "6651",
                "name": "[\u0424\u043e\u043a\u0443\u0441] ForREST",
            }
        if jira_issue_type.lower() == "epic":
            jira_issue_type_id = jira_issue_type_id or "10000"
            jira_epic_name_field = jira_epic_name_field or "customfield_10007"
        jira_labels = _normalize_string_list(raw_route.get("jira_labels"))
        if is_legacy_emrm_story and jira_labels == ["MPR"]:
            jira_labels = ["FromChannel"]
        jira_epic_link = raw_route.get("jira_epic_link") if isinstance(raw_route.get("jira_epic_link"), dict) else {}
        if is_emrm_route and target_system == "jira":
            jira_epic_link = {
                "field_id": str(jira_epic_link.get("field_id") or "customfield_10006").strip(),
                "key": str(jira_epic_link.get("key") or "EMRM-40162").strip(),
            }
        else:
            jira_epic_link = {
                "field_id": str(jira_epic_link.get("field_id") or "").strip(),
                "key": str(jira_epic_link.get("key") or "").strip(),
            }
        routes.append(
            {
                "enabled": raw_route.get("enabled")
                if isinstance(raw_route.get("enabled"), bool)
                else True,
                "name": name,
                "target_system": target_system,
                "subject_triggers": triggers,
                "spaces": spaces,
                "jira_projects": spaces if target_system == "jira" else [],
                "jira_domain": str(raw_route.get("jira_domain") or "sberbank").strip().lower(),
                "jira_issue_type": jira_issue_type,
                "jira_issue_type_id": jira_issue_type_id,
                "jira_epic_name_field": jira_epic_name_field,
                "jira_epic_link": jira_epic_link,
                "jira_priority": str(raw_route.get("jira_priority") or "Minor").strip() or "Minor",
                "jira_labels": jira_labels,
                "jira_team": copy.deepcopy(raw_team),
                "suit": suit or "task",
                "priority": priority or "low",
                "summary_template": summary_template or "{subject}",
            }
        )
    target["routes"] = routes
    return target


def _normalize_sbertrack_users(value: Any, *, enabled_only: bool = False) -> Dict[str, Dict[str, Any]]:
    normalized: Dict[str, Dict[str, Any]] = {}
    if not isinstance(value, dict):
        return normalized
    for raw_email, raw_config in value.items():
        email = str(raw_email or "").strip().lower()
        if not email:
            continue
        if isinstance(raw_config, dict):
            enabled = raw_config.get("enabled")
            normalized[email] = {
                "name": str(raw_config.get("name") or "").strip(),
                "sbertrack_user_id": str(raw_config.get("sbertrack_user_id") or "").strip(),
                "enabled": enabled if isinstance(enabled, bool) else True,
            }
        else:
            normalized[email] = {
                "name": "",
                "sbertrack_user_id": str(raw_config or "").strip(),
                "enabled": True,
            }
        if enabled_only and (
            not normalized[email]["enabled"] or not normalized[email]["sbertrack_user_id"]
        ):
            normalized.pop(email, None)
    return normalized


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
            target = normalized["automation"]["release_monitor_unassigned_email"]
            if isinstance(email_source.get("enabled"), bool):
                target["enabled"] = email_source["enabled"]
            target["recipients"] = _normalize_string_list(email_source.get("recipients"))
            if isinstance(email_source.get("weekly_reminder_enabled"), bool):
                target["weekly_reminder_enabled"] = email_source[
                    "weekly_reminder_enabled"
                ]
            weekly_reminder_time = str(
                email_source.get("weekly_reminder_time") or ""
            ).strip()
            if weekly_reminder_time:
                target["weekly_reminder_time"] = weekly_reminder_time
            target["weekly_reminder_recipients"] = _normalize_string_list(
                email_source.get("weekly_reminder_recipients")
            )

        responsible_email_source = automation.get("release_monitor_responsible_email")
        if isinstance(responsible_email_source, dict):
            target = normalized["automation"]["release_monitor_responsible_email"]
            if isinstance(responsible_email_source.get("enabled"), bool):
                target["enabled"] = responsible_email_source["enabled"]
            target["employee_recipients"] = _normalize_employee_recipients(
                responsible_email_source.get("employee_recipients"),
                enabled_only=True,
            )
            if isinstance(responsible_email_source.get("weekly_digest_enabled"), bool):
                target["weekly_digest_enabled"] = responsible_email_source[
                    "weekly_digest_enabled"
                ]
            weekly_digest_time = str(
                responsible_email_source.get("weekly_digest_time") or ""
            ).strip()
            if weekly_digest_time:
                target["weekly_digest_time"] = weekly_digest_time
            target["weekly_digest_recipients"] = _normalize_string_list(
                responsible_email_source.get("weekly_digest_recipients")
            )
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

        normalized["automation"]["email_to_sbertrack"] = (
            _normalize_email_to_sbertrack_config(automation.get("email_to_sbertrack"))
        )

    release_monitor = payload.get("release_monitor")
    if isinstance(release_monitor, dict) and "prefixes" in release_monitor:
        normalized["release_monitor"]["prefixes"] = _normalize_prefix_entries(
            release_monitor.get("prefixes")
        )

    modules = payload.get("modules")
    if isinstance(modules, dict):
        for module_name, defaults in DEFAULT_FEATURE_FLAGS["modules"].items():
            raw_module = modules.get(module_name)
            if isinstance(raw_module, dict):
                enabled = raw_module.get("enabled")
                if isinstance(enabled, bool):
                    normalized["modules"][module_name]["enabled"] = enabled

    normalized["sbertrack_users"] = _normalize_sbertrack_users(
        payload.get("sbertrack_users")
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


def reload_feature_flags() -> None:
    global _cached_mtime_ns
    with _flags_lock:
        _cached_mtime_ns = None
        _load_flags_if_changed()


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


def is_module_enabled(name: str, *, default: bool = False) -> bool:
    flags = get_feature_flags()
    module_config = (flags.get("modules") or {}).get(name)
    if isinstance(module_config, dict):
        enabled = module_config.get("enabled")
        if isinstance(enabled, bool):
            return enabled
    return default


def get_automation_config(name: str) -> Any:
    flags = get_feature_flags()
    return copy.deepcopy((flags.get("automation") or {}).get(name, False))


def get_release_prefix_configs(*, include_disabled: bool = False) -> List[Dict[str, Any]]:
    flags = get_feature_flags()
    prefixes = list(((flags.get("release_monitor") or {}).get("prefixes")) or [])
    if not include_disabled:
        prefixes = [entry for entry in prefixes if bool(entry.get("enabled", True))]
    return copy.deepcopy(prefixes)


def get_enabled_release_prefixes() -> List[str]:
    return [
        str(entry.get("prefix") or "").strip().upper()
        for entry in get_release_prefix_configs(include_disabled=False)
        if str(entry.get("prefix") or "").strip()
    ]


def get_release_prefix_config(prefix: Any) -> Optional[Dict[str, Any]]:
    normalized_prefix = str(prefix or "").strip().upper()
    if not normalized_prefix:
        return None
    for entry in get_release_prefix_configs(include_disabled=True):
        if entry.get("prefix") == normalized_prefix:
            return copy.deepcopy(entry)
    return None


def get_release_prefix_jira_domain(prefix: Any) -> Optional[str]:
    entry = get_release_prefix_config(prefix)
    if not entry or not bool(entry.get("enabled", True)):
        return None
    domain = str(entry.get("jira_domain") or "").strip().lower()
    return domain if domain in JIRA_DOMAIN_CONFIGS else None


def get_release_prefix_system(prefix: Any) -> str:
    entry = get_release_prefix_config(prefix)
    if not entry or not bool(entry.get("enabled", True)):
        return ""
    return str(entry.get("system") or "").strip()


def get_jira_domain_config(domain_key: Any) -> Optional[Dict[str, str]]:
    key = str(domain_key or "").strip().lower()
    config = JIRA_DOMAIN_CONFIGS.get(key)
    return copy.deepcopy(config) if config else None


def get_sbertrack_users_config(*, enabled_only: bool = True) -> Dict[str, Dict[str, Any]]:
    flags = get_feature_flags()
    return _normalize_sbertrack_users(
        flags.get("sbertrack_users"),
        enabled_only=enabled_only,
    )
