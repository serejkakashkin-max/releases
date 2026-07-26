from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List


SCRIPT_PATH = Path(__file__).resolve()
DEFAULT_ROOT = SCRIPT_PATH.parent.parent


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate directory-only employee contracts.")
    parser.add_argument("--project-root", default=".")
    parser.add_argument("--runtime-root", default="")
    parser.add_argument("--report", default="cache/employee_directory_contracts/report.json")
    parser.add_argument("--pre-start", action="store_true")
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
        context_from_snapshot,
        get_consumer_health,
        get_dashboard_projection,
        get_release_monitor_projection,
        get_release_notification_recipients,
        get_release_zni_users,
        get_va_members,
        get_va_schedule_display_name,
    )
    from services.employee_directory_repository import read_directory_snapshot
    from VA.schedule_manager.repositories.employee_settings_repository import (
        EmployeeSettingsRepository,
    )

    directory_snapshot = read_directory_snapshot()
    context = context_from_snapshot(directory_snapshot)
    feature_flags = _read_json(runtime_root / "feature_flags.json")
    health = get_consumer_health(context, feature_flags=feature_flags)
    report: Dict[str, Any] = {
        "contract_type": "directory_only",
        "status": "blocked",
        "runtime_location": "configured" if args.runtime_root else "local_default",
        "directory_status": context.status,
        "directory_revision": context.revision,
        "directory_etag": context.etag,
        "duplicates": sum(
            1
            for error in directory_snapshot.validation_errors
            if str(error.get("code") or "").startswith("duplicate")
        ),
        "potential_identity_duplicates": sum(
            1
            for error in directory_snapshot.validation_errors
            if error.get("code") == "historical_identity_conflict"
        ),
        "consumer_health": health["consumer_health"],
        "projection_counts": {},
        "va_settings": {
            "active_members": 0,
            "explicit_settings": 0,
            "using_defaults": 0,
            "orphaned_settings": 0,
            "invalid": 0,
            "migration_status": "required",
        },
        "warnings": [],
        "errors": [],
    }
    if context.status != "available" or not context.payload:
        report["errors"].append(f"directory_{context.status}")
        return _finish(runtime_root, args.report, report, 2)
    if args.expected_directory_etag and context.etag != args.expected_directory_etag:
        report["errors"].append("deployment_blocked_directory_changed")

    try:
        report["projection_counts"] = {
            "release_monitor": get_release_monitor_projection(context)["count"],
            "release_zni": len(get_release_zni_users(context)),
            "duty_dashboard": len(get_dashboard_projection(context)["visible_jira"]),
            "release_notifications": len(get_release_notification_recipients(context)),
            "va_schedule_manager": len(get_va_members(context)),
        }
    except Exception as exc:
        report["errors"].append(f"projection_error:{type(exc).__name__}")

    settings = EmployeeSettingsRepository().read()
    va_members = get_va_members(context)
    active_ids = {item["employee_id"] for item in va_members}
    canonical_names = []
    for employee in va_members:
        canonical = get_va_schedule_display_name(employee, context)
        if not canonical:
            report["errors"].append("active_va_member_without_schedule_identity")
        canonical_names.append(canonical.casefold())
    if len(canonical_names) != len(set(canonical_names)):
        report["errors"].append("duplicate_canonical_va_schedule_names")

    if settings.status != "available" or not settings.payload:
        report["va_settings"]["invalid"] = 1
        report["errors"].append(
            "va_settings_migration_required"
            if settings.status in {"missing", "empty"}
            else "va_settings_invalid"
        )
    else:
        migration = settings.payload.get("migration") or {}
        explicit_ids = set((settings.payload.get("employees") or {}).keys())
        report["va_settings"] = {
            "active_members": len(active_ids),
            "explicit_settings": len(active_ids & explicit_ids),
            "using_defaults": len(active_ids - explicit_ids),
            "orphaned_settings": len(explicit_ids - active_ids),
            "invalid": 0,
            "migration_status": migration.get("status"),
        }
        if migration.get("status") in {"required", "failed"}:
            report["errors"].append(f"migration_{migration.get('status')}")
        for key in ("unresolved", "ambiguous", "conflicts"):
            if int(migration.get(key) or 0):
                report["errors"].append(f"migration_{key}")
        if explicit_ids - active_ids:
            report["warnings"].append("orphaned_settings")

    if args.expected_legacy_sha256:
        legacy_path = runtime_root / "cache" / "va_schedule_manager" / "data" / "employees.json"
        if _sha256_file(legacy_path) != args.expected_legacy_sha256:
            report["errors"].append("deployment_blocked_legacy_source_changed")

    blocking_health = [
        name
        for name, value in report["consumer_health"].items()
        if value["status"] in {"invalid_contract", "unavailable"}
    ]
    if args.pre_start and blocking_health:
        report["errors"].append("consumer_health_failed")
    report["status"] = "passed" if not report["errors"] else "blocked"
    return _finish(runtime_root, args.report, report, 0 if report["status"] == "passed" else 2)


def _finish(runtime_root: Path, report_value: str, report: Dict[str, Any], code: int) -> int:
    path = Path(report_value)
    if not path.is_absolute():
        path = runtime_root / path
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    os.replace(temporary, path)
    print(
        json.dumps(
            {
                "status": report["status"],
                "contract_type": report["contract_type"],
                "directory_revision": report["directory_revision"],
                "directory_etag": report["directory_etag"],
                "errors": report["errors"],
            }
        )
    )
    return code


def _read_json(path: Path) -> Dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (FileNotFoundError, UnicodeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _sha256_file(path: Path) -> str:
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return "missing"
    return "sha256:" + hashlib.sha256(data).hexdigest()


if __name__ == "__main__":
    raise SystemExit(main())
