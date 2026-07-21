from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Sequence


SCRIPT_PATH = Path(__file__).resolve()
DEFAULT_ROOT = SCRIPT_PATH.parent.parent
if str(DEFAULT_ROOT) not in sys.path:
    sys.path.insert(0, str(DEFAULT_ROOT))

from services.employee_directory_repository import normalize_text, read_directory_snapshot  # noqa: E402
from services.feature_flags_service import (  # noqa: E402
    EMPLOYEE_DIRECTORY_CONSUMERS,
    get_employee_directory_consumer_mode,
)
from tools.migrate_employee_directory import (  # noqa: E402
    read_literal_lists,
    read_raw_employee_recipients,
    read_release_zni,
    read_va_employees,
    surname_initials_key,
    strip_jira_suffix,
)


def main(argv: List[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    project_root = Path(args.project_root).resolve()
    report_path = resolve_under_root(project_root, args.report)
    snapshot = read_directory_snapshot(project_root / "employee_directory.json")
    if snapshot.status != "available" or not snapshot.payload:
        report = {
            "status": "blocked",
            "reason": f"directory_{snapshot.status}",
            "directory_revision": snapshot.revision,
            "consumers": {},
        }
        atomic_write_json(report_path, report)
        print(json.dumps({"status": "blocked", "reason": report["reason"]}))
        return 2

    config_lists = read_literal_lists(
        project_root / "config.py",
        ("OPLOT_VALUES", "DASHBOARD_ASSIGNEES", "DASHBOARD_EXTRA_ASSIGNEES"),
    )
    legacy_zni, _ = read_release_zni(
        project_root / "services" / "jira_oplot_issue_service.py",
        config_lists["DASHBOARD_ASSIGNEES"],
    )
    recipient_rows, _ = read_raw_employee_recipients(project_root / "feature_flags.json")
    va_rows = read_va_employees(project_root / "cache" / "va_schedule_manager" / "data" / "employees.json")

    employees = [item for item in snapshot.payload["employees"] if item["enabled"]]
    release_names = [item["release_name"] for item in ordered_members(employees, "release_monitor")]
    primary = dashboard_members(employees, "primary")
    extra = dashboard_members(employees, "extra")
    central_primary = [item["jira_names"]["delta"] for item in primary]
    central_extra = [item["jira_names"]["delta"] for item in extra]
    central_visible = central_primary + central_extra
    central_primary_display = [item["full_name"] for item in primary]
    central_visible_display = [item["full_name"] for item in primary + extra]
    central_zni = [
        item["jira_names"]["delta"]
        for item in sorted(employees, key=dashboard_order)
        if item["memberships"]["release_zni"]["enabled"] and item["jira_names"]["delta"]
    ]

    legacy_primary = config_lists["DASHBOARD_ASSIGNEES"]
    legacy_extra = config_lists["DASHBOARD_EXTRA_ASSIGNEES"]
    legacy_visible = deduplicate(legacy_primary + legacy_extra)
    legacy_primary_display = [strip_jira_suffix(value) for value in legacy_primary]
    legacy_visible_display = [strip_jira_suffix(value) for value in legacy_visible]

    legacy_notifications = {
        normalize_text(row["name"]): normalized_emails(row["emails"])
        for row in recipient_rows
        if row["enabled"]
    }
    central_notifications = {
        normalize_text(item["release_name"] or item["full_name"]): normalized_emails(item["emails"])
        for item in employees
        if item["memberships"]["release_notifications"]["enabled"]
    }
    disabled_notification_names = {
        normalize_text(row["name"]).casefold()
        for row in recipient_rows
        if not row["enabled"]
    }
    active_central_notification_names = {value.casefold() for value in central_notifications}

    legacy_va_names = [normalize_text(row["name"]) for row in va_rows]
    central_va_names = central_va_source_names(employees)

    consumers = {
        "release_monitor": consumer_report(
            "release_monitor",
            [sequence_check("release_names", config_lists["OPLOT_VALUES"], release_names)],
        ),
        "release_zni": consumer_report(
            "release_zni",
            [sequence_check("eligible_jira_users", legacy_zni, central_zni)],
        ),
        "duty_dashboard": consumer_report(
            "duty_dashboard",
            [
                sequence_check("primary_jira_names", legacy_primary, central_primary),
                sequence_check("extra_jira_names", legacy_extra, central_extra),
                sequence_check("visible_jira_names", legacy_visible, central_visible),
                sequence_check("primary_display_names", legacy_primary_display, central_primary_display),
                sequence_check("visible_display_names", legacy_visible_display, central_visible_display),
            ],
        ),
        "release_notifications": consumer_report(
            "release_notifications",
            [
                mapping_check("active_recipients", legacy_notifications, central_notifications),
                boolean_check(
                    "disabled_recipients_excluded",
                    not bool(disabled_notification_names & active_central_notification_names),
                ),
            ],
        ),
        "va_schedule_manager": consumer_report(
            "va_schedule_manager",
            [
                unordered_check("employee_identities", legacy_va_names, central_va_names),
                sequence_check("employee_order", legacy_va_names, central_va_names),
            ],
        ),
    }
    checks = [check for consumer in consumers.values() for check in consumer["checks"]]
    exact_duplicate_count = duplicate_record_count(snapshot.payload["employees"])
    potential_identity_duplicates = potential_duplicate_identity_groups(snapshot.payload["employees"])
    contracts_passed = all(check["passed"] for check in checks)
    report = {
        "status": "passed" if contracts_passed and not exact_duplicate_count and not potential_identity_duplicates else "mismatch",
        "directory_revision": snapshot.revision,
        "directory_etag": snapshot.etag,
        "directory_records": len(snapshot.payload["employees"]),
        "active_records": len(employees),
        "duplicate_records_detected": exact_duplicate_count,
        "potential_duplicate_identity_groups": potential_identity_duplicates,
        "consumers": consumers,
        "summary": {
            "checks_total": len(checks),
            "checks_passed": sum(1 for check in checks if check["passed"]),
            "checks_failed": sum(1 for check in checks if not check["passed"]),
            "all_modes_legacy": all(
                get_employee_directory_consumer_mode(name) == "legacy"
                for name in EMPLOYEE_DIRECTORY_CONSUMERS
            ),
        },
    }
    atomic_write_json(report_path, report)
    print(
        json.dumps(
            {
                "status": report["status"],
                "checks": report["summary"]["checks_total"],
                "passed": report["summary"]["checks_passed"],
                "failed": report["summary"]["checks_failed"],
                "duplicates": report["duplicate_records_detected"],
                "potential_identity_duplicates": report["potential_duplicate_identity_groups"],
                "all_modes_legacy": report["summary"]["all_modes_legacy"],
            }
        )
    )
    return 0 if report["status"] == "passed" else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Compare legacy employee lists with central directory projections.")
    parser.add_argument("--project-root", default=".")
    parser.add_argument("--report", default="cache/employee_directory_contracts/report.json")
    return parser


def ordered_members(employees: Sequence[Dict[str, Any]], membership: str) -> List[Dict[str, Any]]:
    return sorted(
        [item for item in employees if item["memberships"][membership]["enabled"]],
        key=lambda item: item["memberships"][membership]["order"],
    )


def dashboard_members(employees: Sequence[Dict[str, Any]], role: str) -> List[Dict[str, Any]]:
    return sorted(
        [
            item
            for item in employees
            if item["memberships"]["duty_dashboard"]["enabled"]
            and item["memberships"]["duty_dashboard"]["role"] == role
        ],
        key=lambda item: item["memberships"]["duty_dashboard"]["order"],
    )


def dashboard_order(employee: Dict[str, Any]) -> int:
    membership = employee["memberships"]["duty_dashboard"]
    order = membership.get("order")
    return order if membership.get("enabled") and isinstance(order, int) else 10**9


def central_va_source_names(employees: Sequence[Dict[str, Any]]) -> List[str]:
    result = []
    va_employees = sorted(
        [item for item in employees if item["memberships"]["va_schedule_manager"]["enabled"]],
        key=lambda item: item["memberships"]["va_schedule_manager"]["order"],
    )
    for employee in va_employees:
        refs = [
            normalize_text(ref).split(":", 2)[2]
            for ref in employee["source_refs"]
            if normalize_text(ref).startswith("va:employees:") and len(normalize_text(ref).split(":", 2)) == 3
        ]
        result.extend(refs)
    return result


def consumer_report(name: str, checks: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        "mode": get_employee_directory_consumer_mode(name),
        "contract_passed": all(check["passed"] for check in checks),
        "ready": False,
        "readiness_reason": "consumer_adapter_not_implemented",
        "checks": checks,
    }


def sequence_check(name: str, legacy: Sequence[str], central: Sequence[str]) -> Dict[str, Any]:
    legacy_values = [normalize_text(value) for value in legacy]
    central_values = [normalize_text(value) for value in central]
    first_mismatch = next(
        (index for index, pair in enumerate(zip(legacy_values, central_values)) if pair[0] != pair[1]),
        None,
    )
    if first_mismatch is None and len(legacy_values) != len(central_values):
        first_mismatch = min(len(legacy_values), len(central_values))
    counts = count_difference(legacy_values, central_values)
    return {
        "name": name,
        "passed": legacy_values == central_values,
        "legacy_count": len(legacy_values),
        "directory_count": len(central_values),
        "first_mismatch_index": first_mismatch,
        **counts,
    }


def unordered_check(name: str, legacy: Sequence[str], central: Sequence[str]) -> Dict[str, Any]:
    legacy_values = [normalize_text(value) for value in legacy]
    central_values = [normalize_text(value) for value in central]
    counts = count_difference(legacy_values, central_values)
    return {
        "name": name,
        "passed": not counts["missing_count"] and not counts["extra_count"],
        "legacy_count": len(legacy_values),
        "directory_count": len(central_values),
        **counts,
    }


def mapping_check(name: str, legacy: Mapping[str, Sequence[str]], central: Mapping[str, Sequence[str]]) -> Dict[str, Any]:
    legacy_normalized = {normalize_text(key).casefold(): list(value) for key, value in legacy.items()}
    central_normalized = {normalize_text(key).casefold(): list(value) for key, value in central.items()}
    shared = set(legacy_normalized) & set(central_normalized)
    value_mismatches = sum(
        1 for key in shared if normalized_emails(legacy_normalized[key]) != normalized_emails(central_normalized[key])
    )
    return {
        "name": name,
        "passed": legacy_normalized == central_normalized,
        "legacy_count": len(legacy_normalized),
        "directory_count": len(central_normalized),
        "missing_count": len(set(legacy_normalized) - set(central_normalized)),
        "extra_count": len(set(central_normalized) - set(legacy_normalized)),
        "value_mismatch_count": value_mismatches,
    }


def boolean_check(name: str, passed: bool) -> Dict[str, Any]:
    return {"name": name, "passed": bool(passed)}


def count_difference(legacy: Iterable[str], central: Iterable[str]) -> Dict[str, int]:
    legacy_counter = Counter(legacy)
    central_counter = Counter(central)
    return {
        "missing_count": sum((legacy_counter - central_counter).values()),
        "extra_count": sum((central_counter - legacy_counter).values()),
    }


def duplicate_record_count(employees: Sequence[Dict[str, Any]]) -> int:
    source_refs = [ref for employee in employees for ref in employee["source_refs"]]
    return sum(count - 1 for count in Counter(source_refs).values() if count > 1)


def potential_duplicate_identity_groups(employees: Sequence[Dict[str, Any]]) -> int:
    employee_ids_by_key: Dict[str, set[str]] = {}
    for employee in employees:
        if not employee.get("enabled"):
            continue
        values = [employee.get("full_name"), employee.get("release_name")]
        values.extend((employee.get("jira_names") or {}).values())
        values.extend(alias.get("value") for alias in employee.get("aliases") or [])
        for value in values:
            key = surname_initials_key(value or "")
            if key:
                employee_ids_by_key.setdefault(key, set()).add(str(employee.get("employee_id") or ""))
    return sum(1 for employee_ids in employee_ids_by_key.values() if len(employee_ids) > 1)


def normalized_emails(values: Iterable[Any]) -> List[str]:
    return sorted({normalize_text(value).lower() for value in values if normalize_text(value)})


def deduplicate(values: Iterable[str]) -> List[str]:
    return list(dict.fromkeys(values))


def resolve_under_root(root: Path, value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else root / path


def atomic_write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
        temporary.replace(path)
    finally:
        if temporary.exists():
            temporary.unlink()


if __name__ == "__main__":
    raise SystemExit(main())
