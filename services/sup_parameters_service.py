import copy
import hashlib
import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Tuple
from uuid import uuid4

from services.feature_flags_service import (
    DEFAULT_FEATURE_FLAGS,
    DEFAULT_RELEASE_PREFIX_CONFIGS,
    FEATURE_FLAGS_FILE,
    JIRA_DOMAIN_CONFIGS,
    PREFIX_PATTERN,
    reload_feature_flags,
)


BACKUP_DIR = Path(__file__).resolve().parent.parent / "cache" / "sup_parameters_backups"
MAX_BACKUPS = 10
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
TIME_PATTERN = re.compile(r"^\d{2}:\d{2}$")

MAINTENANCE_KEYS = ("index", "release_monitor", "duty_dashboard", "chatbot")


class SupParametersValidationError(ValueError):
    def __init__(self, errors: List[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


class SupParametersConflictError(RuntimeError):
    pass


def _file_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_file_bytes() -> Tuple[bytes, bool]:
    try:
        return FEATURE_FLAGS_FILE.read_bytes(), True
    except FileNotFoundError:
        return b"", False


def _read_current_json() -> Tuple[Dict[str, Any], bytes, bool, str]:
    data, exists = _read_file_bytes()
    if not exists:
        return copy.deepcopy(DEFAULT_FEATURE_FLAGS), data, False, ""
    try:
        payload = json.loads(data.decode("utf-8-sig") or "{}")
        if not isinstance(payload, dict):
            raise ValueError("root JSON value must be an object")
        return payload, data, True, ""
    except Exception as exc:
        return copy.deepcopy(DEFAULT_FEATURE_FLAGS), data, True, str(exc)


def _normalize_string_list(value: Any) -> List[str]:
    raw_values = value if isinstance(value, list) else [value]
    normalized = []
    seen = set()
    for raw_value in raw_values:
        clean_value = str(raw_value or "").strip()
        key = clean_value.lower()
        if clean_value and key not in seen:
            normalized.append(clean_value)
            seen.add(key)
    return normalized


def _normalize_int(value: Any, default: int) -> int:
    if isinstance(value, bool):
        return default
    try:
        result = int(value)
    except (TypeError, ValueError):
        return default
    return result if result >= 0 else default


def _coerce_bool(value: Any, default: bool = False) -> bool:
    return value if isinstance(value, bool) else default


def _admin_employee_rows(raw_value: Any) -> List[Dict[str, Any]]:
    if not isinstance(raw_value, dict):
        return []
    rows = []
    for name, raw_config in raw_value.items():
        clean_name = str(name or "").strip()
        if not clean_name:
            continue
        enabled = True
        raw_emails = raw_config
        if isinstance(raw_config, dict):
            enabled = _coerce_bool(raw_config.get("enabled"), True)
            raw_emails = raw_config.get("emails")
        emails = _normalize_string_list(raw_emails)
        rows.append({"name": clean_name, "enabled": enabled, "emails": emails})
    return sorted(rows, key=lambda row: row["name"].lower())


def _admin_prefix_rows(raw_value: Any) -> List[Dict[str, Any]]:
    if not isinstance(raw_value, list):
        return copy.deepcopy(DEFAULT_RELEASE_PREFIX_CONFIGS)
    rows = []
    seen = set()
    for raw_entry in raw_value:
        if not isinstance(raw_entry, dict):
            continue
        prefix = str(raw_entry.get("prefix") or "").strip().upper()
        if not prefix or prefix in seen:
            continue
        rows.append(
            {
                "prefix": prefix,
                "enabled": _coerce_bool(raw_entry.get("enabled"), True),
                "jira_domain": str(raw_entry.get("jira_domain") or "sberbank").strip().lower(),
                "system": str(raw_entry.get("system") or "").strip(),
            }
        )
        seen.add(prefix)
    return rows


def _admin_sbertrack_user_rows(raw_value: Any) -> List[Dict[str, Any]]:
    if not isinstance(raw_value, dict):
        return []
    rows = []
    for email, raw_config in raw_value.items():
        clean_email = str(email or "").strip().lower()
        if not clean_email:
            continue
        if isinstance(raw_config, dict):
            rows.append(
                {
                    "email": clean_email,
                    "name": str(raw_config.get("name") or "").strip(),
                    "sbertrack_user_id": str(
                        raw_config.get("sbertrack_user_id") or ""
                    ).strip(),
                    "enabled": _coerce_bool(raw_config.get("enabled"), True),
                }
            )
        else:
            rows.append(
                {
                    "email": clean_email,
                    "name": "",
                    "sbertrack_user_id": str(raw_config or "").strip(),
                    "enabled": True,
                }
            )
    return sorted(rows, key=lambda row: row["email"])


def _admin_email_to_sbertrack(raw_value: Any) -> Dict[str, Any]:
    source = raw_value if isinstance(raw_value, dict) else {}
    defaults = DEFAULT_FEATURE_FLAGS["automation"]["email_to_sbertrack"]
    routes = []
    for index, raw_route in enumerate(source.get("routes") or [], start=1):
        if not isinstance(raw_route, dict):
            continue
        target_system = str(raw_route.get("target_system") or "sbertrack").strip().lower()
        if target_system not in {"jira", "sbertrack"}:
            target_system = "sbertrack"
        route_name = str(raw_route.get("name") or f"route_{index}").strip()
        subject_triggers = _normalize_string_list(raw_route.get("subject_triggers"))
        summary_template = str(raw_route.get("summary_template") or "{subject}").strip()
        spaces = _normalize_string_list(
            raw_route.get("jira_projects") if target_system == "jira" else raw_route.get("spaces")
        )
        jira_issue_type = str(raw_route.get("jira_issue_type") or "Story").strip() or "Story"
        jira_issue_type_id = str(raw_route.get("jira_issue_type_id") or "").strip()
        jira_epic_name_field = str(raw_route.get("jira_epic_name_field") or "").strip()
        jira_team = raw_route.get("jira_team") if isinstance(raw_route.get("jira_team"), dict) else {}
        is_legacy_emrm_story = (
            target_system == "jira"
            and any(str(item).strip().upper() == "EMRM" for item in spaces)
            and jira_issue_type.lower() == "story"
            and str(jira_team.get("value_id") or "").strip() == "4681"
        )
        if is_legacy_emrm_story:
            route_name = "EMRM"
            subject_triggers = ["EMRM"]
            summary_template = "{subject}"
            jira_issue_type = "Task"
            jira_issue_type_id = "3"
            jira_epic_name_field = ""
            jira_team = {
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
        raw_epic_link = raw_route.get("jira_epic_link") if isinstance(raw_route.get("jira_epic_link"), dict) else {}
        jira_epic_link = {
            "field_id": str(raw_epic_link.get("field_id") or "").strip(),
            "key": str(raw_epic_link.get("key") or "").strip(),
        }
        if target_system == "jira" and any(str(item).strip().upper() == "EMRM" for item in spaces):
            jira_epic_link["field_id"] = jira_epic_link["field_id"] or "customfield_10006"
            jira_epic_link["key"] = jira_epic_link["key"] or "EMRM-40162"
        routes.append(
            {
                "enabled": _coerce_bool(raw_route.get("enabled"), True),
                "name": route_name,
                "target_system": target_system,
                "subject_triggers": subject_triggers,
                "spaces": spaces if target_system == "sbertrack" else [],
                "jira_projects": spaces if target_system == "jira" else [],
                "jira_domain": str(raw_route.get("jira_domain") or "sberbank").strip().lower(),
                "jira_issue_type": jira_issue_type,
                "jira_issue_type_id": jira_issue_type_id,
                "jira_epic_name_field": jira_epic_name_field,
                "jira_epic_link": jira_epic_link,
                "jira_priority": str(raw_route.get("jira_priority") or "Minor").strip(),
                "jira_labels": jira_labels,
                "jira_team": jira_team,
                "suit": str(raw_route.get("suit") or "task").strip(),
                "priority": str(raw_route.get("priority") or "low").strip(),
                "summary_template": summary_template,
            }
        )
    return {
        "enabled": _coerce_bool(source.get("enabled"), defaults["enabled"]),
        "dry_run": _coerce_bool(source.get("dry_run"), defaults["dry_run"]),
        "poll_interval_seconds": _normalize_int(
            source.get("poll_interval_seconds"),
            defaults["poll_interval_seconds"],
        ),
        "lookback_limit": _normalize_int(
            source.get("lookback_limit"),
            defaults["lookback_limit"],
        ),
        "max_pending_per_cycle": _normalize_int(
            source.get("max_pending_per_cycle"),
            defaults["max_pending_per_cycle"],
        ),
        "body_max_chars": _normalize_int(
            source.get("body_max_chars"),
            defaults["body_max_chars"],
        ),
        "technical_mailboxes": [
            email.lower()
            for email in _normalize_string_list(source.get("technical_mailboxes"))
        ],
        "routes": routes,
    }


def _admin_config_from_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    maintenance = payload.get("maintenance") if isinstance(payload.get("maintenance"), dict) else {}
    automation = payload.get("automation") if isinstance(payload.get("automation"), dict) else {}
    unassigned = (
        automation.get("release_monitor_unassigned_email")
        if isinstance(automation.get("release_monitor_unassigned_email"), dict)
        else {}
    )
    responsible = (
        automation.get("release_monitor_responsible_email")
        if isinstance(automation.get("release_monitor_responsible_email"), dict)
        else {}
    )
    email_to_sbertrack = (
        automation.get("email_to_sbertrack")
        if isinstance(automation.get("email_to_sbertrack"), dict)
        else {}
    )
    release_monitor = (
        payload.get("release_monitor") if isinstance(payload.get("release_monitor"), dict) else {}
    )
    return {
        "maintenance": {
            key: _coerce_bool(
                maintenance.get(key),
                DEFAULT_FEATURE_FLAGS["maintenance"].get(key, False),
            )
            for key in MAINTENANCE_KEYS
        },
        "automation": {
            "release_monitor_unassigned_email": {
                "enabled": _coerce_bool(
                    unassigned.get("enabled"),
                    DEFAULT_FEATURE_FLAGS["automation"]["release_monitor_unassigned_email"][
                        "enabled"
                    ],
                ),
                "recipients": _normalize_string_list(unassigned.get("recipients")),
                "weekly_reminder_enabled": _coerce_bool(
                    unassigned.get("weekly_reminder_enabled"),
                    DEFAULT_FEATURE_FLAGS["automation"]["release_monitor_unassigned_email"][
                        "weekly_reminder_enabled"
                    ],
                ),
                "weekly_reminder_time": str(
                    unassigned.get("weekly_reminder_time")
                    or DEFAULT_FEATURE_FLAGS["automation"][
                        "release_monitor_unassigned_email"
                    ]["weekly_reminder_time"]
                ).strip(),
                "weekly_reminder_recipients": _normalize_string_list(
                    unassigned.get("weekly_reminder_recipients")
                ),
            },
            "release_monitor_responsible_email": {
                "enabled": _coerce_bool(
                    responsible.get("enabled"),
                    DEFAULT_FEATURE_FLAGS["automation"]["release_monitor_responsible_email"][
                        "enabled"
                    ],
                ),
                "weekly_digest_enabled": _coerce_bool(
                    responsible.get("weekly_digest_enabled"),
                    DEFAULT_FEATURE_FLAGS["automation"]["release_monitor_responsible_email"][
                        "weekly_digest_enabled"
                    ],
                ),
                "weekly_digest_time": str(
                    responsible.get("weekly_digest_time")
                    or DEFAULT_FEATURE_FLAGS["automation"][
                        "release_monitor_responsible_email"
                    ]["weekly_digest_time"]
                ).strip(),
                "weekly_digest_recipients": _normalize_string_list(
                    responsible.get("weekly_digest_recipients")
                ),
                "assignment_email_delay_minutes": _normalize_int(
                    responsible.get("assignment_email_delay_minutes"),
                    DEFAULT_FEATURE_FLAGS["automation"]["release_monitor_responsible_email"][
                        "assignment_email_delay_minutes"
                    ],
                ),
                "personal_email_send_interval_seconds": _normalize_int(
                    responsible.get("personal_email_send_interval_seconds"),
                    DEFAULT_FEATURE_FLAGS["automation"]["release_monitor_responsible_email"][
                        "personal_email_send_interval_seconds"
                    ],
                ),
                "employee_recipients": _admin_employee_rows(
                    responsible.get("employee_recipients")
                ),
            },
            "email_to_sbertrack": _admin_email_to_sbertrack(email_to_sbertrack),
        },
        "release_monitor": {
            "prefixes": _admin_prefix_rows(release_monitor.get("prefixes")),
        },
        "sbertrack_users": _admin_sbertrack_user_rows(payload.get("sbertrack_users")),
    }


def get_sup_parameters_data() -> Dict[str, Any]:
    payload, data, exists, read_error = _read_current_json()
    revision = _file_hash(data)
    config = _admin_config_from_payload(payload)
    file_mtime = ""
    file_mtime_display = ""
    if exists:
        try:
            mtime_dt = datetime.fromtimestamp(FEATURE_FLAGS_FILE.stat().st_mtime)
            file_mtime = mtime_dt.isoformat(timespec="seconds")
            file_mtime_display = mtime_dt.strftime("%d.%m.%Y %H:%M:%S")
        except OSError:
            file_mtime = ""
            file_mtime_display = ""
    try:
        backup_count = len(list(BACKUP_DIR.glob("feature_flags_*.*")))
    except OSError:
        backup_count = 0
    try:
        from services.email_to_sbertrack_service import get_email_to_sbertrack_status

        email_to_sbertrack_status = get_email_to_sbertrack_status()
    except Exception as exc:
        email_to_sbertrack_status = {
            "enabled": False,
            "dry_run": False,
            "mode": "error",
            "last_error": str(exc),
        }
    return {
        "success": True,
        "title": "РЎРЈРџ-РїР°СЂР°РјРµС‚СЂС‹",
        "file_exists": exists,
        "path": str(FEATURE_FLAGS_FILE),
        "revision": revision,
        "read_error": read_error,
        "backup_dir": str(BACKUP_DIR),
        "config": config,
        "raw_json_preview": json.dumps(payload, ensure_ascii=False, indent=2),
        "metadata": {
            "file_name": FEATURE_FLAGS_FILE.name,
            "file_mtime": file_mtime,
            "file_mtime_display": file_mtime_display,
            "backup_count": backup_count,
            "weekly_digest_uses_fallback": not bool(
                config["automation"]["release_monitor_responsible_email"][
                    "weekly_digest_recipients"
                ]
            ),
            "jira_domains": list(JIRA_DOMAIN_CONFIGS.keys()),
            "standard_systems": ["CLM", "EMRM", "РђРРЎРў", "AI-РђРіРµРЅС‚С‹", "Р¤РѕРєСѓСЃ"],
            "email_to_sbertrack_status": email_to_sbertrack_status,
        },
    }


def _validate_emails(values: Any, label: str, errors: List[str]) -> List[str]:
    emails = _normalize_string_list(values)
    invalid = [email for email in emails if not EMAIL_PATTERN.match(email)]
    if invalid:
        errors.append(f"{label}: РЅРµРєРѕСЂСЂРµРєС‚РЅС‹Рµ email: {', '.join(invalid)}")
    return emails


def _validate_time(value: Any, label: str, errors: List[str]) -> str:
    raw_value = str(value or "").strip()
    if not TIME_PATTERN.match(raw_value):
        errors.append(f"{label}: РІСЂРµРјСЏ РґРѕР»Р¶РЅРѕ Р±С‹С‚СЊ РІ С„РѕСЂРјР°С‚Рµ HH:MM")
        return "16:00"
    hours, minutes = [int(part) for part in raw_value.split(":", 1)]
    if hours > 23 or minutes > 59:
        errors.append(f"{label}: РІСЂРµРјСЏ РґРѕР»Р¶РЅРѕ Р±С‹С‚СЊ РІ РґРёР°РїР°Р·РѕРЅРµ 00:00-23:59")
        return "16:00"
    return raw_value


def _validate_non_negative_int(value: Any, label: str, errors: List[str]) -> int:
    if isinstance(value, bool):
        errors.append(f"{label}: РґРѕР»Р¶РЅРѕ Р±С‹С‚СЊ РЅРµРѕС‚СЂРёС†Р°С‚РµР»СЊРЅРѕРµ С‡РёСЃР»Рѕ")
        return 0
    try:
        result = int(value)
    except (TypeError, ValueError):
        errors.append(f"{label}: РґРѕР»Р¶РЅРѕ Р±С‹С‚СЊ РЅРµРѕС‚СЂРёС†Р°С‚РµР»СЊРЅРѕРµ С‡РёСЃР»Рѕ")
        return 0
    if result < 0:
        errors.append(f"{label}: РґРѕР»Р¶РЅРѕ Р±С‹С‚СЊ РЅРµРѕС‚СЂРёС†Р°С‚РµР»СЊРЅРѕРµ С‡РёСЃР»Рѕ")
        return 0
    return result


def _validate_managed_config(raw_config: Any) -> Dict[str, Any]:
    errors: List[str] = []
    raw_config = raw_config if isinstance(raw_config, dict) else {}
    maintenance = (
        raw_config.get("maintenance") if isinstance(raw_config.get("maintenance"), dict) else {}
    )
    automation = (
        raw_config.get("automation") if isinstance(raw_config.get("automation"), dict) else {}
    )
    unassigned = (
        automation.get("release_monitor_unassigned_email")
        if isinstance(automation.get("release_monitor_unassigned_email"), dict)
        else {}
    )
    responsible = (
        automation.get("release_monitor_responsible_email")
        if isinstance(automation.get("release_monitor_responsible_email"), dict)
        else {}
    )
    email_to_sbertrack = (
        automation.get("email_to_sbertrack")
        if isinstance(automation.get("email_to_sbertrack"), dict)
        else {}
    )
    release_monitor = (
        raw_config.get("release_monitor")
        if isinstance(raw_config.get("release_monitor"), dict)
        else {}
    )

    normalized = {
        "maintenance": {},
        "automation": {
            "release_monitor_unassigned_email": {},
            "release_monitor_responsible_email": {},
            "email_to_sbertrack": {},
        },
        "release_monitor": {"prefixes": []},
        "sbertrack_users": {},
    }

    for key in MAINTENANCE_KEYS:
        value = maintenance.get(key)
        if not isinstance(value, bool):
            errors.append(f"Р РµР¶РёРј РѕР±СЃР»СѓР¶РёРІР°РЅРёСЏ/{key}: Р·РЅР°С‡РµРЅРёРµ РґРѕР»Р¶РЅРѕ Р±С‹С‚СЊ true РёР»Рё false")
            value = DEFAULT_FEATURE_FLAGS["maintenance"].get(key, False)
        normalized["maintenance"][key] = value

    if not isinstance(unassigned.get("enabled"), bool):
        errors.append("РџРёСЃСЊРјР° Р±РµР· РѕС‚РІРµС‚СЃС‚РІРµРЅРЅРѕРіРѕ/enabled: Р·РЅР°С‡РµРЅРёРµ РґРѕР»Р¶РЅРѕ Р±С‹С‚СЊ true РёР»Рё false")
    normalized["automation"]["release_monitor_unassigned_email"]["enabled"] = _coerce_bool(
        unassigned.get("enabled")
    )
    normalized["automation"]["release_monitor_unassigned_email"]["recipients"] = _validate_emails(
        unassigned.get("recipients"),
        "РџРѕР»СѓС‡Р°С‚РµР»Рё РїРёСЃРµРј Р±РµР· РѕС‚РІРµС‚СЃС‚РІРµРЅРЅРѕРіРѕ",
        errors,
    )
    if not isinstance(unassigned.get("weekly_reminder_enabled"), bool):
        errors.append("РџРѕРЅРµРґРµР»СЊРЅРёС‡РЅРѕРµ РїРёСЃСЊРјРѕ Р±РµР· РЅР°Р·РЅР°С‡РµРЅРёР№/enabled: Р·РЅР°С‡РµРЅРёРµ РґРѕР»Р¶РЅРѕ Р±С‹С‚СЊ true РёР»Рё false")
    normalized["automation"]["release_monitor_unassigned_email"]["weekly_reminder_enabled"] = _coerce_bool(
        unassigned.get("weekly_reminder_enabled")
    )
    normalized["automation"]["release_monitor_unassigned_email"]["weekly_reminder_time"] = _validate_time(
        unassigned.get("weekly_reminder_time"),
        "РџРѕРЅРµРґРµР»СЊРЅРёС‡РЅРѕРµ РїРёСЃСЊРјРѕ Р±РµР· РЅР°Р·РЅР°С‡РµРЅРёР№/time",
        errors,
    )
    normalized["automation"]["release_monitor_unassigned_email"]["weekly_reminder_recipients"] = _validate_emails(
        unassigned.get("weekly_reminder_recipients"),
        "РџРѕР»СѓС‡Р°С‚РµР»Рё РїРѕРЅРµРґРµР»СЊРЅРёС‡РЅРѕРіРѕ РїРёСЃСЊРјР° Р±РµР· РЅР°Р·РЅР°С‡РµРЅРёР№",
        errors,
    )

    if not isinstance(responsible.get("enabled"), bool):
        errors.append("РџРµСЂСЃРѕРЅР°Р»СЊРЅС‹Рµ РїРёСЃСЊРјР°/enabled: Р·РЅР°С‡РµРЅРёРµ РґРѕР»Р¶РЅРѕ Р±С‹С‚СЊ true РёР»Рё false")
    if not isinstance(responsible.get("weekly_digest_enabled"), bool):
        errors.append("Weekly digest/enabled: Р·РЅР°С‡РµРЅРёРµ РґРѕР»Р¶РЅРѕ Р±С‹С‚СЊ true РёР»Рё false")
    responsible_target = normalized["automation"]["release_monitor_responsible_email"]
    responsible_target["enabled"] = _coerce_bool(responsible.get("enabled"))
    responsible_target["weekly_digest_enabled"] = _coerce_bool(
        responsible.get("weekly_digest_enabled"),
        True,
    )
    responsible_target["weekly_digest_time"] = _validate_time(
        responsible.get("weekly_digest_time"),
        "Weekly digest time",
        errors,
    )
    responsible_target["weekly_digest_recipients"] = _validate_emails(
        responsible.get("weekly_digest_recipients"),
        "РџРѕР»СѓС‡Р°С‚РµР»Рё weekly digest",
        errors,
    )
    responsible_target["assignment_email_delay_minutes"] = _validate_non_negative_int(
        responsible.get("assignment_email_delay_minutes"),
        "Р—Р°РґРµСЂР¶РєР° РїРµСЂСЃРѕРЅР°Р»СЊРЅС‹С… РїРёСЃРµРј",
        errors,
    )
    responsible_target["personal_email_send_interval_seconds"] = _validate_non_negative_int(
        responsible.get("personal_email_send_interval_seconds"),
        "РџР°СѓР·Р° РјРµР¶РґСѓ РїРµСЂСЃРѕРЅР°Р»СЊРЅС‹РјРё РїРёСЃСЊРјР°РјРё",
        errors,
    )

    employees = responsible.get("employee_recipients")
    if isinstance(employees, dict):
        employee_rows = [
            {"name": name, **(value if isinstance(value, dict) else {"emails": value})}
            for name, value in employees.items()
        ]
    elif isinstance(employees, list):
        employee_rows = employees
    else:
        employee_rows = []
    employee_map: Dict[str, Dict[str, Any]] = {}
    seen_employee_names = set()
    for index, row in enumerate(employee_rows, start=1):
        if not isinstance(row, dict):
            errors.append(f"РЎРѕС‚СЂСѓРґРЅРёРє #{index}: РЅРµРєРѕСЂСЂРµРєС‚РЅР°СЏ Р·Р°РїРёСЃСЊ")
            continue
        name = str(row.get("name") or "").strip()
        if not name:
            errors.append(f"РЎРѕС‚СЂСѓРґРЅРёРє #{index}: Р¤РРћ РѕР±СЏР·Р°С‚РµР»СЊРЅРѕ")
            continue
        lowered = name.lower()
        if lowered in seen_employee_names:
            errors.append(f"РЎРѕС‚СЂСѓРґРЅРёРєРё: РґСѓР±Р»СЊ Р¤РРћ {name}")
            continue
        seen_employee_names.add(lowered)
        enabled = row.get("enabled")
        if not isinstance(enabled, bool):
            errors.append(f"РЎРѕС‚СЂСѓРґРЅРёРє {name}: enabled РґРѕР»Р¶РµРЅ Р±С‹С‚СЊ true РёР»Рё false")
            enabled = True
        emails = _validate_emails(row.get("emails"), f"РЎРѕС‚СЂСѓРґРЅРёРє {name}", errors)
        if enabled and not emails:
            errors.append(f"РЎРѕС‚СЂСѓРґРЅРёРє {name}: РґР»СЏ РІРєР»СЋС‡РµРЅРЅРѕРіРѕ СЃРѕС‚СЂСѓРґРЅРёРєР° РЅСѓР¶РµРЅ С…РѕС‚СЏ Р±С‹ РѕРґРёРЅ email")
        employee_map[name] = {"enabled": enabled, "emails": emails}
    responsible_target["employee_recipients"] = employee_map

    email_to_sbertrack_target = normalized["automation"]["email_to_sbertrack"]
    for key in ("enabled", "dry_run"):
        if not isinstance(email_to_sbertrack.get(key), bool):
            errors.append(f"Email в†’ SberTrack/{key}: Р·РЅР°С‡РµРЅРёРµ РґРѕР»Р¶РЅРѕ Р±С‹С‚СЊ true РёР»Рё false")
    email_to_sbertrack_target["enabled"] = _coerce_bool(email_to_sbertrack.get("enabled"))
    email_to_sbertrack_target["dry_run"] = _coerce_bool(
        email_to_sbertrack.get("dry_run"),
        True,
    )
    email_to_sbertrack_target["poll_interval_seconds"] = _validate_non_negative_int(
        email_to_sbertrack.get("poll_interval_seconds"),
        "Email в†’ SberTrack/poll_interval_seconds",
        errors,
    )
    email_to_sbertrack_target["lookback_limit"] = _validate_non_negative_int(
        email_to_sbertrack.get("lookback_limit"),
        "Email в†’ SberTrack/lookback_limit",
        errors,
    )
    email_to_sbertrack_target["max_pending_per_cycle"] = _validate_non_negative_int(
        email_to_sbertrack.get("max_pending_per_cycle"),
        "Email в†’ SberTrack/max_pending_per_cycle",
        errors,
    )
    email_to_sbertrack_target["body_max_chars"] = _validate_non_negative_int(
        email_to_sbertrack.get("body_max_chars"),
        "Email в†’ SberTrack/body_max_chars",
        errors,
    )
    if email_to_sbertrack_target["poll_interval_seconds"] <= 0:
        errors.append("Email в†’ SberTrack: interval РґРѕР»Р¶РµРЅ Р±С‹С‚СЊ Р±РѕР»СЊС€Рµ 0 СЃРµРєСѓРЅРґ")
        email_to_sbertrack_target["poll_interval_seconds"] = 300
    if email_to_sbertrack_target["lookback_limit"] <= 0:
        errors.append("Email в†’ SberTrack: lookback РґРѕР»Р¶РµРЅ Р±С‹С‚СЊ Р±РѕР»СЊС€Рµ 0")
        email_to_sbertrack_target["lookback_limit"] = 20
    if email_to_sbertrack_target["max_pending_per_cycle"] <= 0:
        errors.append("Email в†’ SberTrack: pending retry РґРѕР»Р¶РµРЅ Р±С‹С‚СЊ Р±РѕР»СЊС€Рµ 0")
        email_to_sbertrack_target["max_pending_per_cycle"] = 10
    if email_to_sbertrack_target["body_max_chars"] < 1000:
        errors.append("Email в†’ SberTrack: body limit РґРѕР»Р¶РµРЅ Р±С‹С‚СЊ РЅРµ РјРµРЅСЊС€Рµ 1000")
        email_to_sbertrack_target["body_max_chars"] = 6000
    email_to_sbertrack_target["technical_mailboxes"] = [
        email.lower()
        for email in _validate_emails(
            email_to_sbertrack.get("technical_mailboxes"),
            "Email в†’ SberTrack technical mailboxes",
            errors,
        )
    ]

    route_rows = email_to_sbertrack.get("routes")
    route_rows = route_rows if isinstance(route_rows, list) else []
    normalized_routes = []
    seen_route_names = set()
    for index, row in enumerate(route_rows, start=1):
        if not isinstance(row, dict):
            errors.append(f"Email в†’ SberTrack route #{index}: РЅРµРєРѕСЂСЂРµРєС‚РЅР°СЏ Р·Р°РїРёСЃСЊ")
            continue
        enabled = row.get("enabled")
        if not isinstance(enabled, bool):
            errors.append(f"Email в†’ SberTrack route #{index}: enabled РґРѕР»Р¶РµРЅ Р±С‹С‚СЊ true РёР»Рё false")
            enabled = True
        name = str(row.get("name") or "").strip()
        if not name:
            errors.append(f"Email в†’ SberTrack route #{index}: name РѕР±СЏР·Р°С‚РµР»РµРЅ")
            name = f"route_{index}"
        name_key = name.lower()
        if name_key in seen_route_names:
            errors.append(f"Email в†’ SberTrack: РґСѓР±Р»СЊ route name {name}")
        seen_route_names.add(name_key)
        triggers = _normalize_string_list(row.get("subject_triggers"))
        summary_template = str(row.get("summary_template") or "").strip() or "{subject}"
        target_system = str(row.get("target_system") or "sbertrack").strip().lower()
        if target_system not in {"jira", "sbertrack"}:
            errors.append(f"Email route {name}: target_system must be jira or sbertrack")
            target_system = "sbertrack"
        spaces = _normalize_string_list(
            row.get("jira_projects") if target_system == "jira" else row.get("spaces")
        )
        if enabled and not triggers:
            errors.append(f"Email в†’ SberTrack route {name}: РЅСѓР¶РµРЅ С…РѕС‚СЏ Р±С‹ РѕРґРёРЅ trigger")
        if enabled and not spaces:
            errors.append(f"Email в†’ SberTrack route {name}: РЅСѓР¶РЅРѕ С…РѕС‚СЏ Р±С‹ РѕРґРЅРѕ space")
        jira_domain = str(row.get("jira_domain") or "sberbank").strip().lower()
        if target_system == "jira" and jira_domain not in JIRA_DOMAIN_CONFIGS:
            errors.append(f"Email route {name}: unknown Jira domain")
            jira_domain = "sberbank"
        jira_issue_type = str(row.get("jira_issue_type") or "").strip()
        jira_issue_type_id = str(row.get("jira_issue_type_id") or "").strip()
        jira_epic_name_field = str(row.get("jira_epic_name_field") or "").strip()
        jira_priority = str(row.get("jira_priority") or "").strip()
        if target_system == "jira" and not jira_issue_type:
            errors.append(f"Email route {name}: jira_issue_type is required")
        if target_system == "jira" and not jira_priority:
            errors.append(f"Email route {name}: jira_priority is required")
        if target_system == "jira" and jira_issue_type.lower() == "epic":
            jira_issue_type_id = jira_issue_type_id or "10000"
            jira_epic_name_field = jira_epic_name_field or "customfield_10007"
        raw_team = row.get("jira_team") if isinstance(row.get("jira_team"), dict) else {}
        jira_team = {
            "field_id": str(raw_team.get("field_id") or "").strip(),
            "value_id": str(raw_team.get("value_id") or "").strip(),
            "name": str(raw_team.get("name") or "").strip(),
        }
        is_emrm_route = target_system == "jira" and any(
            str(item).strip().upper() == "EMRM" for item in spaces
        )
        is_legacy_emrm_story = (
            is_emrm_route
            and jira_issue_type.lower() == "story"
            and jira_team["value_id"] == "4681"
        )
        if is_legacy_emrm_story:
            name = "EMRM"
            triggers = ["EMRM"]
            summary_template = "{subject}"
            jira_issue_type = "Task"
            jira_issue_type_id = "3"
            jira_epic_name_field = ""
            jira_team = {
                "field_id": "customfield_11902",
                "value_id": "6651",
                "name": "[\u0424\u043e\u043a\u0443\u0441] ForREST",
            }
        for team_key, team_value in jira_team.items():
            if len(team_value) > 200 or any(ord(char) < 32 for char in team_value):
                errors.append(f"Email route {name}: jira_team.{team_key} contains unsafe value")
        raw_epic_link = row.get("jira_epic_link") if isinstance(row.get("jira_epic_link"), dict) else {}
        jira_epic_link = {
            "field_id": str(raw_epic_link.get("field_id") or "").strip(),
            "key": str(raw_epic_link.get("key") or "").strip(),
        }
        if is_emrm_route and target_system == "jira":
            jira_epic_link["field_id"] = jira_epic_link["field_id"] or "customfield_10006"
            jira_epic_link["key"] = jira_epic_link["key"] or "EMRM-40162"
        for link_key, link_value in jira_epic_link.items():
            if len(link_value) > 200 or any(ord(char) < 32 for char in link_value):
                errors.append(f"Email route {name}: jira_epic_link.{link_key} contains unsafe value")
        suit = str(row.get("suit") or "").strip() or "task"
        priority = str(row.get("priority") or "").strip() or "low"
        normalized_routes.append(
            {
                "enabled": enabled,
                "name": name,
                "target_system": target_system,
                "subject_triggers": triggers,
                "spaces": spaces if target_system == "sbertrack" else [],
                "jira_projects": spaces if target_system == "jira" else [],
                "jira_domain": jira_domain,
                "jira_issue_type": jira_issue_type or "Story",
                "jira_issue_type_id": jira_issue_type_id,
                "jira_epic_name_field": jira_epic_name_field,
                "jira_epic_link": jira_epic_link,
                "jira_priority": jira_priority or "Minor",
                "jira_labels": _normalize_string_list(row.get("jira_labels")),
                "jira_team": jira_team,
                "suit": suit,
                "priority": priority,
                "summary_template": summary_template,
            }
        )
    email_to_sbertrack_target["routes"] = normalized_routes

    raw_sbertrack_users = raw_config.get("sbertrack_users")
    user_rows = (
        [
            {"email": email, **(value if isinstance(value, dict) else {"sbertrack_user_id": value})}
            for email, value in raw_sbertrack_users.items()
        ]
        if isinstance(raw_sbertrack_users, dict)
        else raw_sbertrack_users
    )
    user_rows = user_rows if isinstance(user_rows, list) else []
    seen_user_emails = set()
    for index, row in enumerate(user_rows, start=1):
        if not isinstance(row, dict):
            errors.append(f"SberTrack user #{index}: РЅРµРєРѕСЂСЂРµРєС‚РЅР°СЏ Р·Р°РїРёСЃСЊ")
            continue
        user_email = str(row.get("email") or "").strip().lower()
        if not user_email:
            errors.append(f"SberTrack user #{index}: email РѕР±СЏР·Р°С‚РµР»РµРЅ")
            continue
        if not EMAIL_PATTERN.match(user_email):
            errors.append(f"SberTrack user {user_email}: РЅРµРєРѕСЂСЂРµРєС‚РЅС‹Р№ email")
            continue
        if user_email in seen_user_emails:
            errors.append(f"SberTrack users: РґСѓР±Р»СЊ email {user_email}")
            continue
        seen_user_emails.add(user_email)
        enabled = row.get("enabled")
        if not isinstance(enabled, bool):
            errors.append(f"SberTrack user {user_email}: enabled РґРѕР»Р¶РµРЅ Р±С‹С‚СЊ true РёР»Рё false")
            enabled = True
        normalized["sbertrack_users"][user_email] = {
            "enabled": enabled,
            "name": str(row.get("name") or "").strip(),
            "sbertrack_user_id": str(row.get("sbertrack_user_id") or "").strip(),
        }

    prefixes = release_monitor.get("prefixes")
    prefix_rows = prefixes if isinstance(prefixes, list) else []
    seen_prefixes = set()
    enabled_count = 0
    for index, row in enumerate(prefix_rows, start=1):
        if not isinstance(row, dict):
            errors.append(f"Prefix #{index}: РЅРµРєРѕСЂСЂРµРєС‚РЅР°СЏ Р·Р°РїРёСЃСЊ")
            continue
        prefix = str(row.get("prefix") or "").strip().upper()
        if not prefix:
            errors.append(f"Prefix #{index}: prefix РѕР±СЏР·Р°С‚РµР»РµРЅ")
            continue
        if not PREFIX_PATTERN.match(prefix):
            errors.append(f"Prefix {prefix}: РґРѕРїСѓСЃС‚РёРјС‹ С‚РѕР»СЊРєРѕ A-Z, 0-9 Рё _")
            continue
        if prefix in seen_prefixes:
            errors.append(f"Release prefixes: РґСѓР±Р»СЊ prefix {prefix}")
            continue
        seen_prefixes.add(prefix)
        enabled = row.get("enabled")
        if not isinstance(enabled, bool):
            errors.append(f"Prefix {prefix}: enabled РґРѕР»Р¶РµРЅ Р±С‹С‚СЊ true РёР»Рё false")
            enabled = True
        jira_domain = str(row.get("jira_domain") or "").strip().lower()
        if jira_domain not in JIRA_DOMAIN_CONFIGS:
            errors.append(f"Prefix {prefix}: jira_domain РґРѕР»Р¶РµРЅ Р±С‹С‚СЊ sberbank РёР»Рё delta")
            jira_domain = "sberbank"
        system = str(row.get("system") or "").strip()
        if not system:
            errors.append(f"Prefix {prefix}: system РѕР±СЏР·Р°С‚РµР»РµРЅ")
            system = "Р”СЂСѓРіРѕРµ"
        normalized["release_monitor"]["prefixes"].append(
            {
                "prefix": prefix,
                "enabled": enabled,
                "jira_domain": jira_domain,
                "system": system,
            }
        )
        if enabled:
            enabled_count += 1
    if enabled_count <= 0:
        errors.append("Release prefixes: РґРѕР»Р¶РµРЅ Р±С‹С‚СЊ РІРєР»СЋС‡РµРЅ С…РѕС‚СЏ Р±С‹ РѕРґРёРЅ prefix")

    if errors:
        raise SupParametersValidationError(errors)
    return normalized


def _backup_existing_file(data: bytes, read_error: str) -> None:
    if not data:
        return
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    suffix = "broken" if read_error else "json"
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    backup_path = BACKUP_DIR / f"feature_flags_{timestamp}.{suffix}"
    backup_path.write_bytes(data)
    backups = sorted(
        BACKUP_DIR.glob("feature_flags_*.*"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for old_backup in backups[MAX_BACKUPS:]:
        try:
            old_backup.unlink()
        except OSError:
            pass


def _atomic_write_json(payload: Dict[str, Any]) -> None:
    FEATURE_FLAGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    temp_path = FEATURE_FLAGS_FILE.with_name(
        f".{FEATURE_FLAGS_FILE.name}.{uuid4().hex}.tmp"
    )
    try:
        with temp_path.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, FEATURE_FLAGS_FILE)
    finally:
        if temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass


def _merge_managed_config(base_payload: Dict[str, Any], managed: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(base_payload, dict):
        base_payload = {}
    merged = copy.deepcopy(base_payload)

    maintenance_target = merged.get("maintenance")
    if not isinstance(maintenance_target, dict):
        maintenance_target = {}
    for key, value in managed["maintenance"].items():
        maintenance_target[key] = value
    merged["maintenance"] = maintenance_target

    automation_target = merged.get("automation")
    if not isinstance(automation_target, dict):
        automation_target = {}
    unassigned_target = automation_target.get("release_monitor_unassigned_email")
    if not isinstance(unassigned_target, dict):
        unassigned_target = {}
    unassigned_target.update(managed["automation"]["release_monitor_unassigned_email"])
    automation_target["release_monitor_unassigned_email"] = unassigned_target

    responsible_target = automation_target.get("release_monitor_responsible_email")
    if not isinstance(responsible_target, dict):
        responsible_target = {}
    responsible_target.update(managed["automation"]["release_monitor_responsible_email"])
    automation_target["release_monitor_responsible_email"] = responsible_target

    email_to_sbertrack_target = automation_target.get("email_to_sbertrack")
    if not isinstance(email_to_sbertrack_target, dict):
        email_to_sbertrack_target = {}
    email_to_sbertrack_target.update(managed["automation"]["email_to_sbertrack"])
    automation_target["email_to_sbertrack"] = email_to_sbertrack_target
    merged["automation"] = automation_target

    release_monitor_target = merged.get("release_monitor")
    if not isinstance(release_monitor_target, dict):
        release_monitor_target = {}
    release_monitor_target["prefixes"] = managed["release_monitor"]["prefixes"]
    merged["release_monitor"] = release_monitor_target

    merged["sbertrack_users"] = managed["sbertrack_users"]

    return merged


def save_sup_parameters(managed_config: Any, expected_revision: str) -> Dict[str, Any]:
    current_payload, current_data, exists, read_error = _read_current_json()
    current_revision = _file_hash(current_data)
    if expected_revision and expected_revision != current_revision:
        raise SupParametersConflictError(
            "РЎРЈРџ-РїР°СЂР°РјРµС‚СЂС‹ Р±С‹Р»Рё РёР·РјРµРЅРµРЅС‹ РІ РґСЂСѓРіРѕРј РѕРєРЅРµ. РћР±РЅРѕРІРёС‚Рµ СЃС‚СЂР°РЅРёС†Сѓ Рё РїРѕРІС‚РѕСЂРёС‚Рµ СЃРѕС…СЂР°РЅРµРЅРёРµ."
        )

    normalized = _validate_managed_config(managed_config)
    _backup_existing_file(current_data, read_error)
    base_payload = current_payload if not read_error else copy.deepcopy(DEFAULT_FEATURE_FLAGS)
    next_payload = _merge_managed_config(base_payload, normalized)
    _atomic_write_json(next_payload)
    reload_feature_flags()
    return get_sup_parameters_data()
