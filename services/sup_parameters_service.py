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
        },
        "release_monitor": {
            "prefixes": _admin_prefix_rows(release_monitor.get("prefixes")),
        },
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
    return {
        "success": True,
        "title": "СУП-параметры",
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
            "standard_systems": ["CLM", "EMRM", "АИСТ", "AI-Агенты", "Фокус"],
        },
    }


def _validate_emails(values: Any, label: str, errors: List[str]) -> List[str]:
    emails = _normalize_string_list(values)
    invalid = [email for email in emails if not EMAIL_PATTERN.match(email)]
    if invalid:
        errors.append(f"{label}: некорректные email: {', '.join(invalid)}")
    return emails


def _validate_time(value: Any, label: str, errors: List[str]) -> str:
    raw_value = str(value or "").strip()
    if not TIME_PATTERN.match(raw_value):
        errors.append(f"{label}: время должно быть в формате HH:MM")
        return "16:00"
    hours, minutes = [int(part) for part in raw_value.split(":", 1)]
    if hours > 23 or minutes > 59:
        errors.append(f"{label}: время должно быть в диапазоне 00:00-23:59")
        return "16:00"
    return raw_value


def _validate_non_negative_int(value: Any, label: str, errors: List[str]) -> int:
    if isinstance(value, bool):
        errors.append(f"{label}: должно быть неотрицательное число")
        return 0
    try:
        result = int(value)
    except (TypeError, ValueError):
        errors.append(f"{label}: должно быть неотрицательное число")
        return 0
    if result < 0:
        errors.append(f"{label}: должно быть неотрицательное число")
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
        },
        "release_monitor": {"prefixes": []},
    }

    for key in MAINTENANCE_KEYS:
        value = maintenance.get(key)
        if not isinstance(value, bool):
            errors.append(f"Режим обслуживания/{key}: значение должно быть true или false")
            value = DEFAULT_FEATURE_FLAGS["maintenance"].get(key, False)
        normalized["maintenance"][key] = value

    if not isinstance(unassigned.get("enabled"), bool):
        errors.append("Письма без ответственного/enabled: значение должно быть true или false")
    normalized["automation"]["release_monitor_unassigned_email"]["enabled"] = _coerce_bool(
        unassigned.get("enabled")
    )
    normalized["automation"]["release_monitor_unassigned_email"]["recipients"] = _validate_emails(
        unassigned.get("recipients"),
        "Получатели писем без ответственного",
        errors,
    )
    if not isinstance(unassigned.get("weekly_reminder_enabled"), bool):
        errors.append("Понедельничное письмо без назначений/enabled: значение должно быть true или false")
    normalized["automation"]["release_monitor_unassigned_email"]["weekly_reminder_enabled"] = _coerce_bool(
        unassigned.get("weekly_reminder_enabled")
    )
    normalized["automation"]["release_monitor_unassigned_email"]["weekly_reminder_time"] = _validate_time(
        unassigned.get("weekly_reminder_time"),
        "Понедельничное письмо без назначений/time",
        errors,
    )
    normalized["automation"]["release_monitor_unassigned_email"]["weekly_reminder_recipients"] = _validate_emails(
        unassigned.get("weekly_reminder_recipients"),
        "Получатели понедельничного письма без назначений",
        errors,
    )

    if not isinstance(responsible.get("enabled"), bool):
        errors.append("Персональные письма/enabled: значение должно быть true или false")
    if not isinstance(responsible.get("weekly_digest_enabled"), bool):
        errors.append("Weekly digest/enabled: значение должно быть true или false")
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
        "Получатели weekly digest",
        errors,
    )
    responsible_target["assignment_email_delay_minutes"] = _validate_non_negative_int(
        responsible.get("assignment_email_delay_minutes"),
        "Задержка персональных писем",
        errors,
    )
    responsible_target["personal_email_send_interval_seconds"] = _validate_non_negative_int(
        responsible.get("personal_email_send_interval_seconds"),
        "Пауза между персональными письмами",
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
            errors.append(f"Сотрудник #{index}: некорректная запись")
            continue
        name = str(row.get("name") or "").strip()
        if not name:
            errors.append(f"Сотрудник #{index}: ФИО обязательно")
            continue
        lowered = name.lower()
        if lowered in seen_employee_names:
            errors.append(f"Сотрудники: дубль ФИО {name}")
            continue
        seen_employee_names.add(lowered)
        enabled = row.get("enabled")
        if not isinstance(enabled, bool):
            errors.append(f"Сотрудник {name}: enabled должен быть true или false")
            enabled = True
        emails = _validate_emails(row.get("emails"), f"Сотрудник {name}", errors)
        if enabled and not emails:
            errors.append(f"Сотрудник {name}: для включенного сотрудника нужен хотя бы один email")
        employee_map[name] = {"enabled": enabled, "emails": emails}
    responsible_target["employee_recipients"] = employee_map

    prefixes = release_monitor.get("prefixes")
    prefix_rows = prefixes if isinstance(prefixes, list) else []
    seen_prefixes = set()
    enabled_count = 0
    for index, row in enumerate(prefix_rows, start=1):
        if not isinstance(row, dict):
            errors.append(f"Prefix #{index}: некорректная запись")
            continue
        prefix = str(row.get("prefix") or "").strip().upper()
        if not prefix:
            errors.append(f"Prefix #{index}: prefix обязателен")
            continue
        if not PREFIX_PATTERN.match(prefix):
            errors.append(f"Prefix {prefix}: допустимы только A-Z, 0-9 и _")
            continue
        if prefix in seen_prefixes:
            errors.append(f"Release prefixes: дубль prefix {prefix}")
            continue
        seen_prefixes.add(prefix)
        enabled = row.get("enabled")
        if not isinstance(enabled, bool):
            errors.append(f"Prefix {prefix}: enabled должен быть true или false")
            enabled = True
        jira_domain = str(row.get("jira_domain") or "").strip().lower()
        if jira_domain not in JIRA_DOMAIN_CONFIGS:
            errors.append(f"Prefix {prefix}: jira_domain должен быть sberbank или delta")
            jira_domain = "sberbank"
        system = str(row.get("system") or "").strip()
        if not system:
            errors.append(f"Prefix {prefix}: system обязателен")
            system = "Другое"
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
        errors.append("Release prefixes: должен быть включен хотя бы один prefix")

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
    merged["automation"] = automation_target

    release_monitor_target = merged.get("release_monitor")
    if not isinstance(release_monitor_target, dict):
        release_monitor_target = {}
    release_monitor_target["prefixes"] = managed["release_monitor"]["prefixes"]
    merged["release_monitor"] = release_monitor_target

    return merged


def save_sup_parameters(managed_config: Any, expected_revision: str) -> Dict[str, Any]:
    current_payload, current_data, exists, read_error = _read_current_json()
    current_revision = _file_hash(current_data)
    if expected_revision and expected_revision != current_revision:
        raise SupParametersConflictError(
            "СУП-параметры были изменены в другом окне. Обновите страницу и повторите сохранение."
        )

    normalized = _validate_managed_config(managed_config)
    _backup_existing_file(current_data, read_error)
    base_payload = current_payload if not read_error else copy.deepcopy(DEFAULT_FEATURE_FLAGS)
    next_payload = _merge_managed_config(base_payload, normalized)
    _atomic_write_json(next_payload)
    reload_feature_flags()
    return get_sup_parameters_data()
