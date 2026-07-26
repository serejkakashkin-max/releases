from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List


SCRIPT_PATH = Path(__file__).resolve()
DEFAULT_ROOT = SCRIPT_PATH.parent.parent


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Migrate legacy VA employee settings to Employee Directory UUIDs."
    )
    parser.add_argument("--project-root", default=".")
    parser.add_argument("--runtime-root", default="")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--write", action="store_true")
    parser.add_argument(
        "--report",
        default="cache/employee_directory_preflight/va_settings_migration.json",
    )
    parser.add_argument("--expected-directory-etag", default="")
    parser.add_argument("--expected-legacy-sha256", default="")
    return parser


def main(argv: List[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    project_root = Path(args.project_root).resolve()
    runtime_root = (
        Path(args.runtime_root).resolve()
        if str(args.runtime_root or "").strip()
        else project_root
    )
    os.environ["RELEASE_WEB_RUNTIME_DIR"] = str(runtime_root)
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))

    from services.employee_directory_service import (
        load_employee_directory_context,
        resolve_historical_va_employee,
    )
    from VA.schedule_manager.repositories.employee_settings_repository import (
        EmployeeSettingsConflictError,
        EmployeeSettingsValidationError,
        EmployeeSettingsRepository,
        empty_settings_payload,
        normalize_employee_settings,
    )

    directory = load_employee_directory_context()
    legacy_path = runtime_root / "cache" / "va_schedule_manager" / "data" / "employees.json"
    legacy_sha = _sha256_file(legacy_path)
    settings_repository = EmployeeSettingsRepository()
    settings_before = settings_repository.read()
    legacy_records = _read_legacy_records(legacy_path)

    report: Dict[str, Any] = {
        "contract_type": "va_settings_migration",
        "mode": "write" if args.write else "dry_run",
        "status": "blocked",
        "runtime_location": "configured" if args.runtime_root else "local_default",
        "directory_status": directory.status,
        "directory_etag": directory.etag,
        "legacy_employees_sha256": legacy_sha,
        "settings_before_etag": settings_before.etag,
        "settings_after_etag": settings_before.etag,
        "source_records": len(legacy_records),
        "resolved": 0,
        "unresolved": 0,
        "ambiguous": 0,
        "already_migrated": 0,
        "would_create": 0,
        "would_update": 0,
        "conflicts": 0,
        "orphaned_existing_settings": 0,
    }
    if directory.status != "available":
        report["reason"] = f"directory_{directory.status}"
        _write_report(runtime_root, args.report, report)
        print(json.dumps({"status": "blocked", "reason": report["reason"]}))
        return 2
    if args.expected_directory_etag and directory.etag != args.expected_directory_etag:
        report["reason"] = "directory_changed"
        _write_report(runtime_root, args.report, report)
        print(json.dumps({"status": "blocked", "reason": report["reason"]}))
        return 2
    if args.expected_legacy_sha256 and legacy_sha != args.expected_legacy_sha256:
        report["reason"] = "legacy_source_changed"
        _write_report(runtime_root, args.report, report)
        print(json.dumps({"status": "blocked", "reason": report["reason"]}))
        return 2

    current_payload = (
        settings_before.copy_payload()
        if settings_before.status == "available" and settings_before.payload
        else empty_settings_payload(
            migration_status="not_required" if not legacy_records else "required",
            directory_etag=directory.etag,
        )
    )
    next_employees = dict(current_payload.get("employees") or {})
    resolved_ids = set()
    for record in legacy_records:
        resolution = resolve_historical_va_employee(
            record.get("name"),
            directory,
        )
        if resolution.status == "unresolved":
            report["unresolved"] += 1
            continue
        if resolution.status == "ambiguous":
            report["ambiguous"] += 1
            continue
        employee_id = resolution.employee_id
        resolved_ids.add(employee_id)
        report["resolved"] += 1
        try:
            migrated = normalize_employee_settings(
                record,
                allow_legacy_status=True,
            )
        except EmployeeSettingsValidationError:
            report["conflicts"] += 1
            continue
        existing = next_employees.get(employee_id)
        if existing is None:
            next_employees[employee_id] = migrated
            report["would_create"] += 1
        elif normalize_employee_settings(
            existing,
            allow_legacy_status=True,
        ) == migrated:
            report["already_migrated"] += 1
        else:
            report["conflicts"] += 1

    active_ids = {
        item["employee_id"]
        for item in (directory.payload or {}).get("employees", [])
        if item.get("enabled")
        and ((item.get("memberships") or {}).get("va_schedule_manager") or {}).get("enabled")
    }
    report["orphaned_existing_settings"] = len(set(next_employees) - active_ids)
    blocking = report["unresolved"] + report["ambiguous"] + report["conflicts"]
    report["status"] = "passed" if not blocking else "blocked"
    report["reason"] = "" if not blocking else "migration_resolution_failed"

    if args.write and not blocking:
        migration = current_payload.get("migration") or {}
        already_complete = (
            settings_before.status == "available"
            and migration.get("status") in {"complete", "not_required"}
            and migration.get("legacy_employees_sha256") == legacy_sha
            and dict((settings_before.payload or {}).get("employees") or {}) == next_employees
        )
        if already_complete:
            report["status"] = "already_migrated"
            report["settings_after_etag"] = settings_before.etag
        else:
            current_payload["revision"] = int(current_payload.get("revision") or 0) + 1
            current_payload["updated_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
            current_payload["employees"] = next_employees
            current_payload["migration"] = {
                "status": "not_required" if not legacy_records else "complete",
                "completed_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "completed_against_directory_etag": directory.etag,
                "legacy_employees_sha256": legacy_sha,
                "source_records": len(legacy_records),
                "resolved": report["resolved"],
                "unresolved": 0,
                "ambiguous": 0,
                "conflicts": 0,
            }
            try:
                saved = settings_repository.write_migration_payload(
                    current_payload,
                    expected_etag=settings_before.etag,
                    pre_write_check=lambda: (
                        load_employee_directory_context().etag == directory.etag
                        and _sha256_file(legacy_path) == legacy_sha
                    ),
                )
            except EmployeeSettingsConflictError:
                report["status"] = "blocked"
                report["reason"] = "migration_sources_changed"
            else:
                report["settings_after_etag"] = saved.etag
                report["status"] = "written"

    _write_report(runtime_root, args.report, report)
    print(
        json.dumps(
            {
                "status": report["status"],
                "source_records": report["source_records"],
                "resolved": report["resolved"],
                "unresolved": report["unresolved"],
                "ambiguous": report["ambiguous"],
                "conflicts": report["conflicts"],
            }
        )
    )
    return 0 if report["status"] in {"passed", "written", "already_migrated"} else 2


def _read_legacy_records(path: Path) -> List[Dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (FileNotFoundError, UnicodeError, json.JSONDecodeError):
        return []
    if isinstance(payload, dict) and "payload" in payload:
        payload = payload.get("payload")
    records = payload.get("employees") if isinstance(payload, dict) else None
    return [dict(item) for item in records if isinstance(item, dict)] if isinstance(records, list) else []


def _sha256_file(path: Path) -> str:
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return "missing"
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _write_report(runtime_root: Path, value: str, payload: Dict[str, Any]) -> None:
    path = Path(value)
    if not path.is_absolute():
        path = runtime_root / path
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    os.replace(temporary, path)


if __name__ == "__main__":
    raise SystemExit(main())
