from __future__ import annotations

import copy
import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Optional
from uuid import UUID, uuid4

from services.cross_process_file_lock import CrossProcessFileLock
from services.employee_directory_service import load_employee_directory_context
from services.runtime_paths import get_coordination_lock_path
from VA.schedule_manager.config import (
    BACKUP_DIR,
    EMPLOYEE_SETTINGS_DATA_FILE,
    LOCK_DIR,
)


SCHEMA_VERSION = 1
SETTINGS_CONTRACT_VERSION = 1
DEFAULT_EMPLOYEE_SETTINGS = {
    "status": "active",
    "role": "employee",
    "competencies": ["support"],
    "overtime_ready": True,
}
MIGRATION_STATUSES = {"not_required", "required", "complete", "failed"}
VA_STATUSES = {"active", "long_leave"}
VA_ROLES = {"employee", "manager"}


class EmployeeSettingsError(RuntimeError):
    pass


class EmployeeSettingsConflictError(EmployeeSettingsError):
    pass


class EmployeeSettingsValidationError(EmployeeSettingsError):
    pass


@dataclass(frozen=True)
class EmployeeSettingsSnapshot:
    status: str
    revision: Optional[int]
    etag: str
    payload: Optional[Dict[str, Any]]

    def copy_payload(self) -> Optional[Dict[str, Any]]:
        return copy.deepcopy(self.payload)


class EmployeeSettingsRepository:
    def __init__(self, path: Path = EMPLOYEE_SETTINGS_DATA_FILE) -> None:
        self.path = Path(path)
        self.lock_path = LOCK_DIR / "va_employee_settings.lock"
        self.backup_dir = (
            BACKUP_DIR / "employee_settings"
            if self.path == EMPLOYEE_SETTINGS_DATA_FILE
            else self.path.parent / ".employee_settings_backups"
        )

    def read(self) -> EmployeeSettingsSnapshot:
        try:
            data = self.path.read_bytes()
        except FileNotFoundError:
            return EmployeeSettingsSnapshot("missing", 0, "missing", None)
        etag = "sha256:" + hashlib.sha256(data).hexdigest()
        if not data.strip():
            return EmployeeSettingsSnapshot("empty", 0, etag, None)
        try:
            payload = json.loads(data.decode("utf-8-sig"))
        except Exception:
            return EmployeeSettingsSnapshot("invalid", None, etag, None)
        errors = validate_settings_payload(payload)
        if errors:
            return EmployeeSettingsSnapshot("invalid", payload.get("revision"), etag, None)
        return EmployeeSettingsSnapshot(
            "available",
            int(payload["revision"]),
            etag,
            copy.deepcopy(payload),
        )

    def save_employee_settings(
        self,
        employee_id: str,
        values: Dict[str, Any],
        *,
        expected_revision: Any,
        expected_etag: str,
        expected_directory_etag: str,
    ) -> EmployeeSettingsSnapshot:
        normalized = normalize_employee_settings(values)
        try:
            UUID(str(employee_id))
        except (ValueError, TypeError, AttributeError):
            raise EmployeeSettingsValidationError("Invalid employee ID.")

        with CrossProcessFileLock(get_coordination_lock_path()):
            context = load_employee_directory_context()
            if context.status != "available":
                raise EmployeeSettingsConflictError("Employee directory is unavailable.")
            if expected_directory_etag and context.etag != expected_directory_etag:
                raise EmployeeSettingsConflictError("Employee directory changed.")
            employee = next(
                (
                    item
                    for item in (context.payload or {}).get("employees", [])
                    if item["employee_id"] == employee_id
                ),
                None,
            )
            membership = ((employee or {}).get("memberships") or {}).get("va_schedule_manager") or {}
            if not employee or not employee.get("enabled") or not membership.get("enabled"):
                raise EmployeeSettingsConflictError("Employee is not an active VA member.")

            self.lock_path.parent.mkdir(parents=True, exist_ok=True)
            with CrossProcessFileLock(self.lock_path):
                current = self.read()
                if current.status != "available" or current.payload is None:
                    raise EmployeeSettingsValidationError("VA settings migration is required.")
                try:
                    normalized_expected_revision = int(expected_revision)
                except (TypeError, ValueError):
                    raise EmployeeSettingsConflictError("VA settings changed.")
                if (
                    normalized_expected_revision != current.revision
                    or str(expected_etag or "") != current.etag
                ):
                    raise EmployeeSettingsConflictError("VA settings changed.")
                latest_context = load_employee_directory_context()
                if latest_context.etag != context.etag:
                    raise EmployeeSettingsConflictError("Employee directory changed.")

                next_payload = current.copy_payload() or {}
                next_payload["revision"] = int(current.revision or 0) + 1
                next_payload["updated_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
                next_payload["employees"][employee_id] = normalized
                self._write_unlocked(next_payload)
                saved = self.read()
                if saved.status != "available":
                    raise EmployeeSettingsError("VA settings verification failed.")
                return saved

    def write_migration_payload(
        self,
        payload: Dict[str, Any],
        *,
        expected_etag: str,
        pre_write_check: Optional[Callable[[], bool]] = None,
    ) -> EmployeeSettingsSnapshot:
        with CrossProcessFileLock(get_coordination_lock_path()):
            self.lock_path.parent.mkdir(parents=True, exist_ok=True)
            with CrossProcessFileLock(self.lock_path):
                current = self.read()
                if str(expected_etag or "") != current.etag:
                    raise EmployeeSettingsConflictError("VA settings changed.")
                if pre_write_check is not None and not pre_write_check():
                    raise EmployeeSettingsConflictError("Migration sources changed.")
                errors = validate_settings_payload(payload)
                if errors:
                    raise EmployeeSettingsValidationError("Invalid migration payload.")
                self._write_unlocked(payload)
                return self.read()

    def _write_unlocked(self, payload: Dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            backup_dir = self.backup_dir
            backup_dir.mkdir(parents=True, exist_ok=True)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            backup = backup_dir / f"{timestamp}_employee_settings.json"
            backup.write_bytes(self.path.read_bytes())
        temporary = self.path.with_name(f".{self.path.name}.{uuid4().hex}.tmp")
        try:
            with temporary.open("w", encoding="utf-8", newline="\n") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            if temporary.exists():
                temporary.unlink()


def empty_settings_payload(*, migration_status: str, directory_etag: str = "") -> Dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "schema_name": "va_employee_settings",
        "revision": 1,
        "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "settings_contract_version": SETTINGS_CONTRACT_VERSION,
        "migration": {
            "status": migration_status,
            "completed_at": "",
            "completed_against_directory_etag": directory_etag,
            "legacy_employees_sha256": "",
            "source_records": 0,
            "resolved": 0,
            "unresolved": 0,
            "ambiguous": 0,
            "conflicts": 0,
        },
        "employees": {},
    }


def normalize_employee_settings(value: Any) -> Dict[str, Any]:
    source = value if isinstance(value, dict) else {}
    status = str(source.get("status") or "active").strip()
    role = str(source.get("role") or "employee").strip()
    competencies = []
    for item in source.get("competencies") if isinstance(source.get("competencies"), (list, tuple)) else []:
        clean = str(item or "").strip()
        if clean and clean not in competencies:
            competencies.append(clean)
    if not competencies:
        competencies = ["support"]
    if status not in VA_STATUSES:
        raise EmployeeSettingsValidationError("Invalid VA status.")
    if role not in VA_ROLES:
        raise EmployeeSettingsValidationError("Invalid VA role.")
    overtime_ready = source.get("overtime_ready")
    if not isinstance(overtime_ready, bool):
        raise EmployeeSettingsValidationError("Invalid overtime flag.")
    return {
        "status": status,
        "role": role,
        "competencies": competencies,
        "overtime_ready": overtime_ready,
    }


def effective_employee_settings(explicit: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if explicit is None:
        return copy.deepcopy(DEFAULT_EMPLOYEE_SETTINGS)
    return normalize_employee_settings(explicit)


def validate_settings_payload(payload: Any) -> list:
    errors = []
    if not isinstance(payload, dict):
        return ["object_required"]
    required = {
        "schema_version",
        "schema_name",
        "revision",
        "updated_at",
        "settings_contract_version",
        "migration",
        "employees",
    }
    if set(payload) != required:
        errors.append("exact_fields_required")
    if payload.get("schema_version") != SCHEMA_VERSION:
        errors.append("unsupported_schema")
    if payload.get("schema_name") != "va_employee_settings":
        errors.append("invalid_schema_name")
    if payload.get("settings_contract_version") != SETTINGS_CONTRACT_VERSION:
        errors.append("unsupported_settings_contract")
    if not isinstance(payload.get("revision"), int) or isinstance(payload.get("revision"), bool):
        errors.append("invalid_revision")
    if not isinstance(payload.get("updated_at"), str):
        errors.append("invalid_updated_at")
    migration = payload.get("migration")
    migration_keys = {
        "status",
        "completed_at",
        "completed_against_directory_etag",
        "legacy_employees_sha256",
        "source_records",
        "resolved",
        "unresolved",
        "ambiguous",
        "conflicts",
    }
    if not isinstance(migration, dict) or set(migration) != migration_keys:
        errors.append("invalid_migration")
    else:
        if migration.get("status") not in MIGRATION_STATUSES:
            errors.append("invalid_migration_status")
        for key in ("source_records", "resolved", "unresolved", "ambiguous", "conflicts"):
            if not isinstance(migration.get(key), int) or isinstance(migration.get(key), bool):
                errors.append(f"invalid_migration_{key}")
    employees = payload.get("employees")
    if not isinstance(employees, dict):
        errors.append("invalid_employees")
    else:
        for employee_id, settings in employees.items():
            try:
                UUID(str(employee_id))
                normalize_employee_settings(settings)
            except (ValueError, EmployeeSettingsValidationError):
                errors.append("invalid_employee_settings")
    return errors
