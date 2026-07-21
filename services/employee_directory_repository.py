from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional
from uuid import UUID, uuid4

from services.cross_process_file_lock import CrossProcessFileLock


PROJECT_ROOT = Path(__file__).resolve().parent.parent
EMPLOYEE_DIRECTORY_FILE = PROJECT_ROOT / "employee_directory.json"
BACKUP_DIR = PROJECT_ROOT / "cache" / "employee_directory_backups"
LOCK_FILE = BACKUP_DIR / "employee_directory.lock"
SUPPORTED_SCHEMA_VERSION = 1
MAX_BACKUPS = 30

ROOT_KEYS = {
    "schema_version",
    "revision",
    "created_at",
    "created_by",
    "updated_at",
    "updated_by",
    "employees",
}
EMPLOYEE_KEYS = {
    "employee_id",
    "enabled",
    "full_name",
    "release_name",
    "jira_names",
    "aliases",
    "emails",
    "phone",
    "location",
    "personnel_number",
    "memberships",
    "source_refs",
}
MEMBERSHIP_KEYS = {
    "release_monitor",
    "release_zni",
    "duty_dashboard",
    "release_notifications",
    "va_schedule_manager",
}
ALIAS_TYPES = {"full", "release", "jira", "schedule", "va"}
JIRA_DOMAINS = {"delta", "sberbank"}
DASHBOARD_ROLES = {"primary", "extra", "none"}
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
SOURCE_REF_PATTERN = re.compile(
    r"^(?:config:(?:OPLOT_VALUES|DASHBOARD_ASSIGNEES|DASHBOARD_EXTRA_ASSIGNEES)"
    r"|feature_flags:employee_recipients|va:employees|duty_schedule:employee"
    r"|release_zni:eligible):.+$"
)


class EmployeeDirectoryError(RuntimeError):
    pass


class EmployeeDirectoryConflictError(EmployeeDirectoryError):
    pass


class EmployeeDirectoryStateError(EmployeeDirectoryError):
    pass


class EmployeeDirectoryValidationError(EmployeeDirectoryError):
    def __init__(self, errors: List[Dict[str, str]]):
        super().__init__("Employee directory validation failed.")
        self.errors = errors


@dataclass(frozen=True)
class DirectorySnapshot:
    status: str
    revision: Optional[int]
    etag: str
    payload: Optional[Dict[str, Any]]
    data: bytes
    validation_errors: List[Dict[str, str]]


def normalize_text(value: Any) -> str:
    return " ".join(unicodedata.normalize("NFC", str(value or "")).strip().split())


def canonical_source_ref(namespace: str, source: str, value: Any) -> str:
    return f"{normalize_text(namespace)}:{normalize_text(source)}:{normalize_text(value)}"


def calculate_etag(data: bytes, *, exists: bool = True) -> str:
    if not exists:
        return "missing"
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def read_directory_snapshot(path: Path = EMPLOYEE_DIRECTORY_FILE) -> DirectorySnapshot:
    path = Path(path)
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return DirectorySnapshot("missing", 0, "missing", None, b"", [])
    etag = calculate_etag(data)
    if not data.strip():
        return DirectorySnapshot("empty", 0, etag, None, data, [])
    try:
        payload = json.loads(data.decode("utf-8-sig"))
    except Exception:
        return DirectorySnapshot("invalid", None, etag, None, data, [_error("root", "invalid_json")])
    if not isinstance(payload, dict):
        return DirectorySnapshot("invalid", None, etag, None, data, [_error("root", "object_required")])
    schema_version = payload.get("schema_version")
    if schema_version != SUPPORTED_SCHEMA_VERSION:
        revision = payload.get("revision") if isinstance(payload.get("revision"), int) else None
        return DirectorySnapshot("unsupported_schema", revision, etag, payload, data, [])
    errors = validate_directory(payload)
    revision = payload.get("revision") if isinstance(payload.get("revision"), int) else None
    if errors:
        return DirectorySnapshot("invalid", revision, etag, payload, data, errors)
    return DirectorySnapshot("available", revision, etag, payload, data, [])


def normalize_employee(raw_employee: Any) -> Dict[str, Any]:
    employee = raw_employee if isinstance(raw_employee, dict) else {}
    raw_jira_names = employee.get("jira_names") if isinstance(employee.get("jira_names"), dict) else {}
    raw_memberships = employee.get("memberships") if isinstance(employee.get("memberships"), dict) else {}

    aliases = []
    for raw_alias in employee.get("aliases") if isinstance(employee.get("aliases"), list) else []:
        if not isinstance(raw_alias, dict):
            aliases.append(raw_alias)
            continue
        alias_type = normalize_text(raw_alias.get("type")).lower()
        aliases.append(
            {
                "value": normalize_text(raw_alias.get("value")),
                "type": alias_type,
                "jira_domain": normalize_text(raw_alias.get("jira_domain")).lower(),
            }
        )

    emails = []
    seen_emails = set()
    for value in employee.get("emails") if isinstance(employee.get("emails"), list) else []:
        email = normalize_text(value).lower()
        if email and email not in seen_emails:
            emails.append(email)
            seen_emails.add(email)

    source_refs = sorted(
        {
            normalize_text(value)
            for value in employee.get("source_refs") if isinstance(employee.get("source_refs"), list)
            if normalize_text(value)
        }
    )

    return {
        "employee_id": normalize_text(employee.get("employee_id")),
        "enabled": employee.get("enabled") if isinstance(employee.get("enabled"), bool) else True,
        "full_name": normalize_text(employee.get("full_name")),
        "release_name": normalize_text(employee.get("release_name")),
        "jira_names": {
            "delta": normalize_text(raw_jira_names.get("delta")),
            "sberbank": normalize_text(raw_jira_names.get("sberbank")),
        },
        "aliases": aliases,
        "emails": emails,
        "phone": normalize_text(employee.get("phone")),
        "location": normalize_text(employee.get("location")),
        "personnel_number": normalize_text(employee.get("personnel_number")),
        "memberships": {
            "release_monitor": _normalize_ordered_membership(raw_memberships.get("release_monitor")),
            "release_zni": _normalize_enabled_membership(raw_memberships.get("release_zni")),
            "duty_dashboard": _normalize_dashboard_membership(raw_memberships.get("duty_dashboard")),
            "release_notifications": _normalize_enabled_membership(raw_memberships.get("release_notifications")),
            "va_schedule_manager": _normalize_enabled_membership(raw_memberships.get("va_schedule_manager")),
        },
        "source_refs": source_refs,
    }


def validate_directory(payload: Any) -> List[Dict[str, str]]:
    errors: List[Dict[str, str]] = []
    if not isinstance(payload, dict):
        return [_error("root", "object_required")]
    if set(payload) != ROOT_KEYS:
        errors.append(_error("root", "exact_fields_required"))
    if payload.get("schema_version") != SUPPORTED_SCHEMA_VERSION:
        errors.append(_error("schema_version", "unsupported"))
    revision = payload.get("revision")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 0:
        errors.append(_error("revision", "non_negative_integer_required"))
    for field in ("created_at", "created_by", "updated_at", "updated_by"):
        if not isinstance(payload.get(field), str):
            errors.append(_error(field, "string_required"))
    employees = payload.get("employees")
    if not isinstance(employees, list):
        errors.append(_error("employees", "list_required"))
        return errors

    employee_ids = set()
    active_release_names: Dict[str, int] = {}
    active_jira_names = {domain: {} for domain in JIRA_DOMAINS}
    active_emails: Dict[str, int] = {}
    active_aliases: Dict[tuple, int] = {}
    release_orders: Dict[int, int] = {}
    dashboard_orders = {"primary": {}, "extra": {}}

    for index, employee in enumerate(employees):
        path = f"employees[{index}]"
        if not isinstance(employee, dict):
            errors.append(_error(path, "object_required"))
            continue
        if set(employee) != EMPLOYEE_KEYS:
            errors.append(_error(path, "exact_fields_required"))
        employee_id = employee.get("employee_id")
        try:
            UUID(str(employee_id))
        except (ValueError, TypeError, AttributeError):
            errors.append(_error(f"{path}.employee_id", "valid_uuid_required"))
        else:
            if employee_id in employee_ids:
                errors.append(_error(f"{path}.employee_id", "duplicate"))
            employee_ids.add(employee_id)
        enabled = employee.get("enabled")
        if not isinstance(enabled, bool):
            errors.append(_error(f"{path}.enabled", "boolean_required"))
            enabled = False
        for string_field in ("full_name", "release_name", "phone", "location", "personnel_number"):
            if not isinstance(employee.get(string_field), str):
                errors.append(_error(f"{path}.{string_field}", "string_required"))
        full_name = normalize_text(employee.get("full_name"))
        if not full_name:
            errors.append(_error(f"{path}.full_name", "required"))

        jira_names = employee.get("jira_names")
        if not isinstance(jira_names, dict) or set(jira_names) != JIRA_DOMAINS:
            errors.append(_error(f"{path}.jira_names", "delta_and_sberbank_required"))
            jira_names = {}
        for domain in JIRA_DOMAINS:
            if not isinstance(jira_names.get(domain), str):
                errors.append(_error(f"{path}.jira_names.{domain}", "string_required"))
            jira_name = normalize_text(jira_names.get(domain))
            if enabled and jira_name:
                key = jira_name.casefold()
                if key in active_jira_names[domain]:
                    errors.append(_error(f"{path}.jira_names.{domain}", "duplicate_active"))
                active_jira_names[domain][key] = index

        emails = employee.get("emails")
        if not isinstance(emails, list):
            errors.append(_error(f"{path}.emails", "list_required"))
            emails = []
        local_emails = set()
        for email_index, raw_email in enumerate(emails):
            email = normalize_text(raw_email).lower()
            email_path = f"{path}.emails[{email_index}]"
            if not email or not EMAIL_PATTERN.match(email):
                errors.append(_error(email_path, "invalid"))
                continue
            if email in local_emails:
                errors.append(_error(email_path, "duplicate_in_employee"))
            local_emails.add(email)
            if enabled:
                if email in active_emails:
                    errors.append(_error(email_path, "duplicate_active"))
                active_emails[email] = index

        aliases = employee.get("aliases")
        if not isinstance(aliases, list):
            errors.append(_error(f"{path}.aliases", "list_required"))
            aliases = []
        local_aliases = set()
        for alias_index, alias in enumerate(aliases):
            alias_path = f"{path}.aliases[{alias_index}]"
            if not isinstance(alias, dict) or set(alias) != {"value", "type", "jira_domain"}:
                errors.append(_error(alias_path, "typed_alias_required"))
                continue
            value = normalize_text(alias.get("value"))
            alias_type = normalize_text(alias.get("type")).lower()
            domain = normalize_text(alias.get("jira_domain")).lower()
            for alias_field in ("value", "type", "jira_domain"):
                if not isinstance(alias.get(alias_field), str):
                    errors.append(_error(f"{alias_path}.{alias_field}", "string_required"))
            if not value:
                errors.append(_error(f"{alias_path}.value", "required"))
            if alias_type not in ALIAS_TYPES:
                errors.append(_error(f"{alias_path}.type", "unsupported"))
            if alias_type == "jira" and domain not in JIRA_DOMAINS:
                errors.append(_error(f"{alias_path}.jira_domain", "required_for_jira"))
            if alias_type != "jira" and domain:
                errors.append(_error(f"{alias_path}.jira_domain", "must_be_empty"))
            alias_key = (alias_type, domain, value.casefold())
            if alias_key in local_aliases:
                errors.append(_error(alias_path, "duplicate_in_employee"))
            local_aliases.add(alias_key)
            if enabled and value and alias_type in ALIAS_TYPES:
                if alias_key in active_aliases:
                    errors.append(_error(alias_path, "duplicate_active"))
                active_aliases[alias_key] = index

        memberships = employee.get("memberships")
        if not isinstance(memberships, dict) or set(memberships) != MEMBERSHIP_KEYS:
            errors.append(_error(f"{path}.memberships", "exact_memberships_required"))
            memberships = {}

        release = memberships.get("release_monitor")
        if not _valid_membership_shape(release, {"enabled", "order"}):
            errors.append(_error(f"{path}.memberships.release_monitor", "invalid"))
            release = {}
        if release.get("enabled") is True:
            release_name = normalize_text(employee.get("release_name"))
            if not release_name:
                errors.append(_error(f"{path}.release_name", "required_for_release_monitor"))
            elif enabled:
                key = release_name.casefold()
                if key in active_release_names:
                    errors.append(_error(f"{path}.release_name", "duplicate_active"))
                active_release_names[key] = index
            _validate_order(release.get("order"), f"{path}.memberships.release_monitor.order", release_orders, errors)

        for membership_name in ("release_zni", "release_notifications", "va_schedule_manager"):
            membership = memberships.get(membership_name)
            if not _valid_membership_shape(membership, {"enabled"}):
                errors.append(_error(f"{path}.memberships.{membership_name}", "invalid"))

        notifications = memberships.get("release_notifications") or {}
        if notifications.get("enabled") is True and not local_emails:
            errors.append(_error(f"{path}.emails", "required_for_release_notifications"))

        dashboard = memberships.get("duty_dashboard")
        if not _valid_membership_shape(dashboard, {"enabled", "role", "order"}):
            errors.append(_error(f"{path}.memberships.duty_dashboard", "invalid"))
            dashboard = {}
        role = dashboard.get("role")
        if role not in DASHBOARD_ROLES:
            errors.append(_error(f"{path}.memberships.duty_dashboard.role", "unsupported"))
        if dashboard.get("enabled") is True:
            if role not in {"primary", "extra"}:
                errors.append(_error(f"{path}.memberships.duty_dashboard.role", "primary_or_extra_required"))
            if not normalize_text(jira_names.get("delta")):
                errors.append(_error(f"{path}.jira_names.delta", "required_for_duty_dashboard"))
            if role in dashboard_orders:
                _validate_order(
                    dashboard.get("order"),
                    f"{path}.memberships.duty_dashboard.order",
                    dashboard_orders[role],
                    errors,
                )
        elif role != "none":
            errors.append(_error(f"{path}.memberships.duty_dashboard.role", "none_required_when_disabled"))

        source_refs = employee.get("source_refs")
        if not isinstance(source_refs, list):
            errors.append(_error(f"{path}.source_refs", "list_required"))
        else:
            local_source_refs = set()
            for source_index, source_ref in enumerate(source_refs):
                source_path = f"{path}.source_refs[{source_index}]"
                if not isinstance(source_ref, str) or not SOURCE_REF_PATTERN.match(normalize_text(source_ref)):
                    errors.append(_error(source_path, "invalid_canonical_source_ref"))
                    continue
                normalized_ref = normalize_text(source_ref)
                if normalized_ref in local_source_refs:
                    errors.append(_error(source_path, "duplicate_in_employee"))
                local_source_refs.add(normalized_ref)

    return errors


def get_employee_directory_admin_data(path: Path = EMPLOYEE_DIRECTORY_FILE) -> Dict[str, Any]:
    snapshot = read_directory_snapshot(path)
    payload = copy.deepcopy(snapshot.payload) if snapshot.status == "available" else None
    return {
        "success": True,
        "status": snapshot.status,
        "revision": snapshot.revision,
        "etag": snapshot.etag,
        "directory": payload,
        "validation_errors": snapshot.validation_errors,
    }


def save_employee_directory(
    employees: Any,
    *,
    expected_revision: Any,
    expected_etag: str,
    writer: str = "sup_employee_directory",
    path: Path = EMPLOYEE_DIRECTORY_FILE,
    allow_invalid_overwrite: bool = False,
    pre_write_check: Optional[Callable[[], bool]] = None,
) -> Dict[str, Any]:
    path = Path(path)
    lock_path = BACKUP_DIR / "employee_directory.lock" if path == EMPLOYEE_DIRECTORY_FILE else path.parent / ".employee_directory.lock"
    with CrossProcessFileLock(lock_path):
        current = read_directory_snapshot(path)
        _assert_expected_version(current, expected_revision, expected_etag)
        if pre_write_check is not None and not pre_write_check():
            raise EmployeeDirectoryStateError("Legacy source changed before employee directory write.")
        if current.status == "unsupported_schema":
            raise EmployeeDirectoryStateError("Unsupported employee directory schema.")
        if current.status == "invalid" and not allow_invalid_overwrite:
            raise EmployeeDirectoryStateError("Invalid employee directory requires explicit recovery.")
        if current.status not in {"missing", "empty", "invalid", "available"}:
            raise EmployeeDirectoryStateError("Employee directory cannot be written in its current state.")
        if not isinstance(employees, list):
            raise EmployeeDirectoryValidationError([_error("employees", "list_required")])

        now = datetime.now().astimezone().isoformat(timespec="seconds")
        if current.status == "available" and current.payload:
            created_at = str(current.payload.get("created_at") or "")
            created_by = str(current.payload.get("created_by") or "")
            current_revision = int(current.payload.get("revision") or 0)
        else:
            created_at = now
            created_by = writer
            current_revision = 0
        raw_payload = {
            "schema_version": SUPPORTED_SCHEMA_VERSION,
            "revision": current_revision + 1,
            "created_at": created_at,
            "created_by": created_by,
            "updated_at": now,
            "updated_by": writer,
            "employees": copy.deepcopy(employees),
        }
        raw_errors = validate_directory(raw_payload)
        if raw_errors:
            raise EmployeeDirectoryValidationError(raw_errors)
        normalized_employees = [normalize_employee(employee) for employee in employees]
        next_payload = {**raw_payload, "employees": normalized_employees}
        errors = validate_directory(next_payload)
        if errors:
            raise EmployeeDirectoryValidationError(errors)

        backup_dir = BACKUP_DIR if path == EMPLOYEE_DIRECTORY_FILE else path.parent / "cache" / "employee_directory_backups"
        _backup_current_bytes(current, broken=current.status == "invalid", backup_dir=backup_dir)
        _atomic_write_json(path, next_payload)
        saved = read_directory_snapshot(path)
        if saved.status != "available":
            raise EmployeeDirectoryStateError("Employee directory verification failed after write.")
        return get_employee_directory_admin_data(path)


def _assert_expected_version(snapshot: DirectorySnapshot, expected_revision: Any, expected_etag: str) -> None:
    if snapshot.revision is None:
        normalized_revision = None if expected_revision is None else expected_revision
    else:
        try:
            normalized_revision = int(expected_revision)
        except (TypeError, ValueError):
            raise EmployeeDirectoryConflictError("Employee directory revision does not match.")
    if normalized_revision != snapshot.revision or str(expected_etag or "") != snapshot.etag:
        raise EmployeeDirectoryConflictError("Employee directory was changed by another process.")


def _backup_current_bytes(snapshot: DirectorySnapshot, *, broken: bool, backup_dir: Path = BACKUP_DIR) -> None:
    if snapshot.status == "missing":
        return
    backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    suffix = "broken" if broken else "json"
    backup_path = backup_dir / f"employee_directory_{timestamp}.{suffix}"
    with backup_path.open("wb") as handle:
        handle.write(snapshot.data)
        handle.flush()
        os.fsync(handle.fileno())
    backups = sorted(
        [item for item in backup_dir.glob("employee_directory_*.*") if item.is_file()],
        key=lambda item: item.stat().st_mtime,
        reverse=True,
    )
    for old_backup in backups[MAX_BACKUPS:]:
        try:
            old_backup.unlink()
        except OSError:
            pass


def _atomic_write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temp_path.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass


def _normalize_enabled_membership(value: Any) -> Dict[str, bool]:
    source = value if isinstance(value, dict) else {}
    return {"enabled": source.get("enabled") if isinstance(source.get("enabled"), bool) else False}


def _normalize_ordered_membership(value: Any) -> Dict[str, Any]:
    source = value if isinstance(value, dict) else {}
    return {
        "enabled": source.get("enabled") if isinstance(source.get("enabled"), bool) else False,
        "order": source.get("order") if isinstance(source.get("order"), int) and not isinstance(source.get("order"), bool) else None,
    }


def _normalize_dashboard_membership(value: Any) -> Dict[str, Any]:
    source = value if isinstance(value, dict) else {}
    enabled = source.get("enabled") if isinstance(source.get("enabled"), bool) else False
    role = normalize_text(source.get("role")).lower()
    return {
        "enabled": enabled,
        "role": role if role in DASHBOARD_ROLES else ("none" if not enabled else role),
        "order": source.get("order") if isinstance(source.get("order"), int) and not isinstance(source.get("order"), bool) else None,
    }


def _valid_membership_shape(value: Any, expected_keys: set) -> bool:
    return isinstance(value, dict) and set(value) == expected_keys and isinstance(value.get("enabled"), bool)


def _validate_order(value: Any, path: str, used: Dict[int, int], errors: List[Dict[str, str]]) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        errors.append(_error(path, "non_negative_integer_required"))
        return
    if value in used:
        errors.append(_error(path, "duplicate_active"))
    used[value] = 1


def _error(path: str, code: str) -> Dict[str, str]:
    return {"path": path, "code": code}
