from __future__ import annotations

import argparse
import ast
import copy
import hashlib
import json
import sys
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from uuid import UUID, uuid5


SCRIPT_PATH = Path(__file__).resolve()
DEFAULT_ROOT = SCRIPT_PATH.parent.parent
if str(DEFAULT_ROOT) not in sys.path:
    sys.path.insert(0, str(DEFAULT_ROOT))

from services.employee_directory_repository import (  # noqa: E402
    EmployeeDirectoryConflictError,
    EmployeeDirectoryStateError,
    EmployeeDirectoryValidationError,
    canonical_source_ref,
    normalize_employee,
    normalize_text,
    read_directory_snapshot,
    save_employee_directory,
    validate_directory,
)


PROVISIONAL_NAMESPACE = UUID("8f0c8bf3-dbb9-52b0-9d78-86ae7ce18c68")
KNOWN_RECIPIENT_FIELDS = {"name", "full_name", "release_name", "enabled", "emails", "email"}
LEGACY_PATHS = {
    "feature_flags": Path("feature_flags.json"),
    "va_employees": Path("cache/va_schedule_manager/data/employees.json"),
    "va_schedule_data": Path("cache/va_schedule_manager/data/schedule_data.json"),
    "duty_schedule": Path("cache/release_monitor_duty_schedule.json"),
}


@dataclass
class Fragment:
    source_ref: str
    source_kind: str
    full_name: str = ""
    release_name: str = ""
    jira_delta: str = ""
    emails: List[str] = field(default_factory=list)
    phone: str = ""
    location: str = ""
    personnel_number: str = ""
    aliases: List[Dict[str, str]] = field(default_factory=list)
    memberships: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    diagnostics: Dict[str, Any] = field(default_factory=dict)


class UnionFind:
    def __init__(self, size: int):
        self.parent = list(range(size))

    def find(self, index: int) -> int:
        while self.parent[index] != index:
            self.parent[index] = self.parent[self.parent[index]]
            index = self.parent[index]
        return index

    def union(self, left: int, right: int) -> None:
        left_root = self.find(left)
        right_root = self.find(right)
        if left_root != right_root:
            self.parent[right_root] = left_root


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    project_root = Path(args.project_root).resolve()
    report_dir = _resolve_under_root(project_root, args.report_dir)
    resolutions_path = _resolve_under_root(project_root, args.resolutions)
    directory_path = project_root / "employee_directory.json"
    report_dir.mkdir(parents=True, exist_ok=True)

    initial_hashes = hash_legacy_files(project_root)
    snapshot = read_directory_snapshot(directory_path)
    mode = args.mode or ("reconcile" if snapshot.status == "available" and snapshot.payload and snapshot.payload["employees"] else "bootstrap")
    mode_error = validate_mode(snapshot.status, snapshot.payload, mode, args.overwrite, args.write)
    if mode_error:
        print(json.dumps({"status": "blocked", "reason": mode_error}, ensure_ascii=True))
        return 2

    resolutions = read_resolutions(resolutions_path)
    source_result = collect_sources(project_root, args)
    proposal = build_proposal(source_result, resolutions)
    for source_error in source_result["blocking_errors"]:
        conflict = {
            "conflict_id": stable_id(
                "source_error",
                [str(source_error.get("source") or "unknown"), str(source_error.get("error_type") or "Error")],
            ),
            "type": "source_error",
            "blocking": True,
            "source": str(source_error.get("source") or "unknown"),
            "error_type": str(source_error.get("error_type") or "Error"),
        }
        proposal["conflicts"].append(conflict)
        proposal["unresolved"].append(conflict)

    if mode == "reconcile" and snapshot.payload:
        preview, diffs, reconcile_conflicts = reconcile_directory(
            snapshot.payload,
            proposal["employees"],
            resolutions,
        )
        proposal["conflicts"].extend(reconcile_conflicts)
        proposal["unresolved"].extend(reconcile_conflicts)
    else:
        preview = proposal["directory"]
        diffs = []

    validation_errors = validate_directory(preview)
    if validation_errors:
        proposal["conflicts"].append(
            {
                "conflict_id": stable_id("directory_validation", [item["path"] + ":" + item["code"] for item in validation_errors]),
                "type": "directory_validation",
                "blocking": True,
                "error_count": len(validation_errors),
            }
        )

    before_write_hashes = hash_legacy_files(project_root)
    integrity_before_write = integrity_rows(initial_hashes, before_write_hashes)
    legacy_changed_before_write = any(not row["unchanged"] for row in integrity_before_write)

    write_status = "dry_run"
    write_error = ""
    if args.write:
        if legacy_changed_before_write:
            write_status = "blocked"
            write_error = "legacy_source_changed_before_write"
        elif source_result["blocking_errors"] or any(item.get("blocking", True) for item in proposal["unresolved"]):
            write_status = "blocked"
            write_error = "blocking_conflicts"
        else:
            try:
                saved = save_employee_directory(
                    preview["employees"],
                    expected_revision=snapshot.revision,
                    expected_etag=snapshot.etag,
                    writer="migrate_employee_directory",
                    path=directory_path,
                    allow_invalid_overwrite=(snapshot.status == "invalid" and mode == "bootstrap" and args.overwrite),
                    pre_write_check=lambda: hash_legacy_files(project_root) == initial_hashes,
                )
                write_status = "written"
                preview = saved.get("directory") or preview
            except (
                EmployeeDirectoryConflictError,
                EmployeeDirectoryStateError,
                EmployeeDirectoryValidationError,
            ) as exc:
                write_status = "blocked"
                write_error = type(exc).__name__

    final_hashes = hash_legacy_files(project_root)
    integrity_final = integrity_rows(initial_hashes, final_hashes)
    authoritative = not any(not row["unchanged"] for row in integrity_final)
    if write_status == "written" and not authoritative:
        write_status = "written_non_authoritative"

    reports = {
        "preview.json": preview,
        "conflicts.json": {"conflicts": proposal["conflicts"]},
        "unresolved.json": {"unresolved": proposal["unresolved"]},
        "diffs.json": {"diffs": diffs},
        "sources.json": source_result["sources_report"],
        "report.json": {
            "mode": mode,
            "write_status": write_status,
            "write_error": write_error,
            "authoritative": authoritative,
            "employees_total": len(preview.get("employees") or []),
            "employees_active": sum(1 for item in preview.get("employees") or [] if item.get("enabled")),
            "employees_archived": sum(1 for item in preview.get("employees") or [] if not item.get("enabled")),
            "sources_found": sum(1 for item in source_result["sources_report"]["sources"].values() if item.get("found")),
            "source_records": len(source_result["fragments"]),
            "automatic_matches": proposal["automatic_matches"],
            "conflicts": len(proposal["conflicts"]),
            "unresolved": len(proposal["unresolved"]),
            "blocking_source_errors": len(source_result["blocking_errors"]),
            "integrity": integrity_final,
        },
    }
    if not resolutions_path.exists():
        resolutions = {"identity_decisions": {}, "field_decisions": {}}
    reports["resolutions.json"] = resolutions
    for filename, payload in reports.items():
        atomic_write_json(report_dir / filename, payload)

    print(
        json.dumps(
            {
                "status": write_status,
                "mode": mode,
                "source_records": len(source_result["fragments"]),
                "employees": len(preview.get("employees") or []),
                "conflicts": len(proposal["conflicts"]),
                "unresolved": len(proposal["unresolved"]),
                "legacy_unchanged": authoritative,
            },
            ensure_ascii=True,
        )
    )
    return 0 if write_status in {"dry_run", "written"} else 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build employee_directory.json from legacy sources.")
    parser.add_argument("--project-root", default=".")
    parser.add_argument("--mode", choices=("bootstrap", "reconcile"))
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--dry-run", action="store_true", default=True)
    action.add_argument("--write", action="store_true")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--skip-feature-flags", action="store_true")
    parser.add_argument("--skip-va", action="store_true")
    parser.add_argument("--skip-duty-schedule", action="store_true")
    parser.add_argument("--report-dir", default="cache/employee_directory_migration")
    parser.add_argument("--resolutions", default="cache/employee_directory_migration/resolutions.json")
    return parser


def validate_mode(
    status: str,
    payload: Optional[Dict[str, Any]],
    mode: str,
    overwrite: bool,
    write: bool,
) -> str:
    if status == "unsupported_schema":
        return "unsupported_schema_requires_separate_migration"
    if status == "invalid":
        if not (mode == "bootstrap" and overwrite):
            return "invalid_directory_requires_bootstrap_write_overwrite"
        return ""
    has_employees = bool(payload and payload.get("employees"))
    if mode == "bootstrap" and status not in {"missing", "empty"} and not (status == "available" and not has_employees):
        return "bootstrap_requires_missing_empty_or_available_empty_directory"
    if mode == "reconcile" and not (status == "available" and has_employees):
        return "reconcile_requires_available_non_empty_directory"
    if write and mode == "reconcile" and not overwrite:
        return "reconcile_write_requires_overwrite"
    return ""


def collect_sources(project_root: Path, args: argparse.Namespace) -> Dict[str, Any]:
    fragments: List[Fragment] = []
    blocking_errors: List[Dict[str, Any]] = []
    sources_report: Dict[str, Any] = {"sources": {}, "raw_unknown_fields": []}

    config_path = project_root / "config.py"
    try:
        config_lists = read_literal_lists(
            config_path,
            ("OPLOT_VALUES", "DASHBOARD_ASSIGNEES", "DASHBOARD_EXTRA_ASSIGNEES"),
        )
        sources_report["sources"]["config"] = {"found": True, "record_count": sum(len(value) for value in config_lists.values())}
        fragments.extend(config_fragments(config_lists, blocking_errors))
    except Exception as exc:
        sources_report["sources"]["config"] = {"found": config_path.exists(), "status": "invalid", "error_type": type(exc).__name__}
        blocking_errors.append({"source": "config", "error_type": type(exc).__name__})
        config_lists = {"DASHBOARD_ASSIGNEES": []}

    zni_path = project_root / "services" / "jira_oplot_issue_service.py"
    try:
        eligible, excluded = read_release_zni(zni_path, config_lists.get("DASHBOARD_ASSIGNEES", []))
        sources_report["sources"]["release_zni"] = {
            "found": True,
            "eligible_count": len(eligible),
            "excluded_count": len(excluded),
        }
        fragments.extend(zni_fragments(eligible))
    except Exception as exc:
        sources_report["sources"]["release_zni"] = {"found": zni_path.exists(), "status": "invalid", "error_type": type(exc).__name__}
        blocking_errors.append({"source": "release_zni", "error_type": type(exc).__name__})

    feature_path = project_root / LEGACY_PATHS["feature_flags"]
    if args.skip_feature_flags:
        sources_report["sources"]["feature_flags"] = {"found": feature_path.exists(), "status": "skipped"}
    else:
        try:
            recipients, unknown_fields = read_raw_employee_recipients(feature_path)
            sources_report["sources"]["feature_flags"] = {"found": True, "record_count": len(recipients)}
            sources_report["raw_unknown_fields"].extend(unknown_fields)
            fragments.extend(recipient_fragments(recipients))
        except FileNotFoundError:
            sources_report["sources"]["feature_flags"] = {"found": False}
        except Exception as exc:
            sources_report["sources"]["feature_flags"] = {"found": True, "status": "invalid", "error_type": type(exc).__name__}
            blocking_errors.append({"source": "feature_flags", "error_type": type(exc).__name__})

    va_path = project_root / LEGACY_PATHS["va_employees"]
    if args.skip_va:
        sources_report["sources"]["va_employees"] = {"found": va_path.exists(), "status": "skipped"}
    else:
        try:
            va_rows = read_va_employees(va_path)
            sources_report["sources"]["va_employees"] = {"found": True, "record_count": len(va_rows)}
            fragments.extend(va_fragments(va_rows))
        except FileNotFoundError:
            sources_report["sources"]["va_employees"] = {"found": False, "record_count": 0}
        except Exception as exc:
            sources_report["sources"]["va_employees"] = {"found": True, "status": "invalid", "error_type": type(exc).__name__}
            blocking_errors.append({"source": "va_employees", "error_type": type(exc).__name__})

    duty_path = project_root / LEGACY_PATHS["duty_schedule"]
    if args.skip_duty_schedule:
        sources_report["sources"]["duty_schedule"] = {"found": duty_path.exists(), "status": "skipped"}
    else:
        try:
            duty_names = read_duty_schedule_names(duty_path)
            sources_report["sources"]["duty_schedule"] = {"found": True, "record_count": len(duty_names)}
            fragments.extend(duty_fragments(duty_names))
        except FileNotFoundError:
            sources_report["sources"]["duty_schedule"] = {"found": False, "record_count": 0}
        except Exception as exc:
            sources_report["sources"]["duty_schedule"] = {"found": True, "status": "invalid", "error_type": type(exc).__name__}
            blocking_errors.append({"source": "duty_schedule", "error_type": type(exc).__name__})

    return {
        "fragments": fragments,
        "blocking_errors": blocking_errors,
        "sources_report": sources_report,
    }


def build_proposal(source_result: Dict[str, Any], resolutions: Dict[str, Any]) -> Dict[str, Any]:
    fragments: List[Fragment] = source_result["fragments"]
    conflicts: List[Dict[str, Any]] = []
    unresolved: List[Dict[str, Any]] = []
    automatic_matches = 0
    union = UnionFind(len(fragments))
    source_ref_index: Dict[str, int] = {}
    excluded_indexes = set()

    for index, fragment in enumerate(fragments):
        if fragment.source_ref in source_ref_index:
            conflict = make_conflict("duplicate_source_reference", [fragment.source_ref], blocking=True)
            conflicts.append(conflict)
            unresolved.append(conflict)
        else:
            source_ref_index[fragment.source_ref] = index

    identity_decisions = resolutions.get("identity_decisions") if isinstance(resolutions.get("identity_decisions"), dict) else {}
    field_decisions = resolutions.get("field_decisions") if isinstance(resolutions.get("field_decisions"), dict) else {}
    for conflict_id, decision in identity_decisions.items():
        if not isinstance(decision, dict):
            continue
        refs = [normalize_text(value) for value in decision.get("source_refs") or []]
        indexes = [source_ref_index[ref] for ref in refs if ref in source_ref_index]
        action = str(decision.get("action") or "").strip().lower()
        if action in {"merge", "manual"} and len(indexes) >= 2:
            for index in indexes[1:]:
                union.union(indexes[0], index)
        elif action == "exclude":
            excluded_indexes.update(indexes)

    exact_keys: Dict[Tuple[str, str], int] = {}
    for index, fragment in enumerate(fragments):
        if index in excluded_indexes:
            continue
        keys = []
        if fragment.full_name:
            keys.append(("full", fragment.full_name.casefold()))
            if fragment.source_kind == "duty_schedule":
                keys.append(("release", fragment.full_name.casefold()))
        if fragment.release_name:
            keys.append(("release", fragment.release_name.casefold()))
        for email in fragment.emails:
            keys.append(("email", email.lower()))
        for key in keys:
            previous = exact_keys.get(key)
            if previous is None:
                exact_keys[key] = index
            elif fragments[previous].source_kind != fragment.source_kind or key[0] == "email":
                union.union(previous, index)
                automatic_matches += 1

    role_by_name: Dict[str, Dict[str, Fragment]] = {}
    for fragment in fragments:
        dashboard = fragment.memberships.get("duty_dashboard") or {}
        if dashboard.get("enabled") and fragment.full_name:
            role_by_name.setdefault(fragment.full_name.casefold(), {})[dashboard.get("role")] = fragment
    for roles in role_by_name.values():
        if "primary" in roles and "extra" in roles:
            refs = [roles["primary"].source_ref, roles["extra"].source_ref]
            conflict = make_conflict("dashboard_role_collision", refs, blocking=True)
            conflicts.append(conflict)
            if not decision_resolves(conflict, identity_decisions):
                unresolved.append(conflict)

    release_indexes = [index for index, item in enumerate(fragments) if item.release_name]
    full_indexes = [index for index, item in enumerate(fragments) if item.full_name and not item.release_name]
    for release_index in release_indexes:
        release_fragment = fragments[release_index]
        candidate_indexes = [
            full_index
            for full_index in full_indexes
            if surname_initials_key(release_fragment.release_name)
            and surname_initials_key(release_fragment.release_name) == surname_initials_key(fragments[full_index].full_name)
            and union.find(release_index) != union.find(full_index)
        ]
        if not candidate_indexes:
            continue
        refs = [release_fragment.source_ref] + [fragments[index].source_ref for index in candidate_indexes]
        conflict = make_conflict("identity_match_suggested", refs, blocking=True)
        conflicts.append(conflict)
        decision = identity_decisions.get(conflict["conflict_id"])
        if isinstance(decision, dict) and decision.get("action") in {"merge", "manual"}:
            selected_refs = decision.get("source_refs") or refs
            selected = [source_ref_index[ref] for ref in selected_refs if ref in source_ref_index]
            if len(selected) >= 2:
                for index in selected[1:]:
                    union.union(selected[0], index)
                continue
        if isinstance(decision, dict) and decision.get("action") in {"keep_separate", "exclude"}:
            continue
        unresolved.append(conflict)

    groups: Dict[int, List[Fragment]] = {}
    for index, fragment in enumerate(fragments):
        if index not in excluded_indexes:
            groups.setdefault(union.find(index), []).append(fragment)
    employees = []
    for group in groups.values():
        employee, group_conflicts = aggregate_fragments(group)
        employees.append(employee)
        conflicts.extend(group_conflicts)
        unresolved.extend(
            conflict
            for conflict in group_conflicts
            if not decision_resolves(conflict, identity_decisions)
            and not decision_resolves(conflict, field_decisions)
        )
    employees.sort(key=employee_sort_key)
    directory = build_directory_payload(employees)
    return {
        "employees": employees,
        "directory": directory,
        "conflicts": unique_items(conflicts, "conflict_id"),
        "unresolved": unique_items(unresolved, "conflict_id"),
        "automatic_matches": automatic_matches,
    }


def aggregate_fragments(fragments: List[Fragment]) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    refs = sorted({item.source_ref for item in fragments})
    conflicts: List[Dict[str, Any]] = []
    dashboard_fragments = [item for item in fragments if item.source_kind in {"dashboard_primary", "dashboard_extra"}]
    release_fragments = [item for item in fragments if item.source_kind == "release_monitor"]
    notification_fragments = [item for item in fragments if item.source_kind == "release_notifications"]
    va_fragments_list = [item for item in fragments if item.source_kind == "va"]

    full_names = unique_non_empty(item.full_name for item in dashboard_fragments + va_fragments_list + fragments)
    release_names = unique_non_empty(item.release_name for item in release_fragments + notification_fragments + fragments)
    jira_names = unique_non_empty(item.jira_delta for item in dashboard_fragments + fragments)
    if len(full_names) > 1:
        conflicts.append(make_conflict("full_name_difference", refs, blocking=True))
    if len(release_names) > 1:
        conflicts.append(make_conflict("release_name_difference", refs, blocking=True))
    if len(jira_names) > 1:
        conflicts.append(make_conflict("jira_name_difference", refs, blocking=True))

    recipient_emails = unique_non_empty(email for item in notification_fragments for email in item.emails)
    va_emails = unique_non_empty(email for item in va_fragments_list for email in item.emails)
    emails = recipient_emails or va_emails
    if recipient_emails and va_emails and set(recipient_emails) != set(va_emails):
        conflicts.append(make_conflict("email_source_difference", refs, blocking=True))

    memberships = default_memberships()
    aliases: List[Dict[str, str]] = []
    phone = ""
    location = ""
    personnel_number = ""
    for fragment in fragments:
        for name, value in fragment.memberships.items():
            if not value.get("enabled"):
                continue
            current = memberships[name]
            if name == "duty_dashboard" and current.get("enabled") and current.get("role") != value.get("role"):
                conflicts.append(make_conflict("dashboard_role_collision", refs, blocking=True))
                continue
            memberships[name] = copy.deepcopy(value)
        aliases.extend(copy.deepcopy(fragment.aliases))
        phone = phone or fragment.phone
        location = location or fragment.location
        personnel_number = personnel_number or fragment.personnel_number

    release_name = release_names[0] if release_names else ""
    full_name = full_names[0] if full_names else release_name
    jira_delta = jira_names[0] if jira_names else ""
    aliases = unique_aliases(aliases)
    employee_id = str(uuid5(PROVISIONAL_NAMESPACE, "\n".join(refs)))
    employee = normalize_employee(
        {
            "employee_id": employee_id,
            "enabled": True,
            "full_name": full_name,
            "release_name": release_name,
            "jira_names": {"delta": jira_delta, "sberbank": ""},
            "aliases": aliases,
            "emails": emails,
            "phone": phone,
            "location": location,
            "personnel_number": personnel_number,
            "memberships": memberships,
            "source_refs": refs,
        }
    )
    return employee, conflicts


def reconcile_directory(
    existing: Dict[str, Any],
    proposed_employees: List[Dict[str, Any]],
    resolutions: Dict[str, Any],
) -> Tuple[Dict[str, Any], List[Dict[str, Any]], List[Dict[str, Any]]]:
    preview = copy.deepcopy(existing)
    existing_rows = preview.get("employees") or []
    by_source_ref = {
        source_ref: row
        for row in existing_rows
        for source_ref in row.get("source_refs") or []
    }
    diffs = []
    conflicts = []
    for proposed in proposed_employees:
        matches = {
            id(by_source_ref[source_ref]): by_source_ref[source_ref]
            for source_ref in proposed.get("source_refs") or []
            if source_ref in by_source_ref
        }
        if len(matches) > 1:
            conflict = make_conflict("reconcile_multiple_central_matches", proposed.get("source_refs") or [], blocking=True)
            conflicts.append(conflict)
            continue
        if not matches:
            existing_rows.append(proposed)
            continue
        central = next(iter(matches.values()))
        for field_name in (
            "full_name",
            "release_name",
            "jira_names",
            "aliases",
            "emails",
            "phone",
            "location",
            "personnel_number",
            "memberships",
            "enabled",
        ):
            if central.get(field_name) != proposed.get(field_name):
                diff_id = stable_id("field_diff", [central.get("employee_id", ""), field_name])
                diff = {"diff_id": diff_id, "field": field_name, "status": "requires_resolution"}
                decision = (resolutions.get("field_decisions") or {}).get(diff_id)
                if isinstance(decision, dict) and decision.get("action") == "accept_source":
                    central[field_name] = copy.deepcopy(proposed.get(field_name))
                    diff["status"] = "accepted"
                elif isinstance(decision, dict) and decision.get("action") in {"keep_central", "manual"}:
                    diff["status"] = "kept_central"
                else:
                    conflicts.append(make_conflict("reconcile_field_difference", proposed.get("source_refs") or [], blocking=True, seed=diff_id))
                diffs.append(diff)
        central["source_refs"] = sorted(set(central.get("source_refs") or []) | set(proposed.get("source_refs") or []))
    preview["employees"] = existing_rows
    return preview, diffs, conflicts


def read_literal_lists(path: Path, names: Iterable[str]) -> Dict[str, List[str]]:
    tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
    wanted = set(names)
    result: Dict[str, List[str]] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        targets = [target.id for target in node.targets if isinstance(target, ast.Name)]
        matching = wanted.intersection(targets)
        if not matching:
            continue
        value = ast.literal_eval(node.value)
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise ValueError("Expected a literal string list.")
        for name in matching:
            result[name] = value
    missing = wanted - set(result)
    if missing:
        raise ValueError("Required config list was not found.")
    return result


def read_release_zni(path: Path, dashboard_names: List[str]) -> Tuple[List[str], List[str]]:
    tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
    target_value = None
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "RELEASE_MONITOR_JIRA_USERS" for target in node.targets):
            target_value = node.value
            break
    if not isinstance(target_value, ast.ListComp) or len(target_value.generators) != 1:
        raise ValueError("Unsupported RELEASE_MONITOR_JIRA_USERS structure.")
    generator = target_value.generators[0]
    if not isinstance(generator.iter, ast.Name) or generator.iter.id != "DASHBOARD_ASSIGNEES" or len(generator.ifs) != 1:
        raise ValueError("Unsupported RELEASE_MONITOR_JIRA_USERS source.")
    condition = generator.ifs[0]
    if not isinstance(condition, ast.UnaryOp) or not isinstance(condition.op, ast.Not) or not isinstance(condition.operand, ast.Call):
        raise ValueError("Unsupported RELEASE_MONITOR_JIRA_USERS filter.")
    call = condition.operand
    if not isinstance(call.func, ast.Attribute) or call.func.attr != "startswith" or len(call.args) != 1:
        raise ValueError("Unsupported RELEASE_MONITOR_JIRA_USERS predicate.")
    prefixes = ast.literal_eval(call.args[0])
    if not isinstance(prefixes, tuple) or not prefixes or not all(isinstance(item, str) for item in prefixes):
        raise ValueError("Unsupported RELEASE_MONITOR_JIRA_USERS prefixes.")
    eligible = [name for name in dashboard_names if not name.startswith(prefixes)]
    excluded = [name for name in dashboard_names if name.startswith(prefixes)]
    return eligible, excluded


def read_raw_employee_recipients(path: Path) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    raw = (((payload.get("automation") or {}).get("release_monitor_responsible_email") or {}).get("employee_recipients"))
    rows = []
    if isinstance(raw, dict):
        for name, value in raw.items():
            if isinstance(value, dict):
                row = {"name": name, **value}
            else:
                row = {"name": name, "emails": value}
            rows.append(row)
    elif isinstance(raw, list):
        rows = list(raw)
    elif raw is None:
        rows = []
    else:
        raise ValueError("Unsupported employee_recipients format.")

    normalized = []
    unknown_fields = []
    for raw_row in rows:
        if not isinstance(raw_row, dict):
            raise ValueError("Employee recipient row must be an object.")
        name = normalize_text(raw_row.get("name") or raw_row.get("release_name") or raw_row.get("full_name"))
        if not name:
            raise ValueError("Employee recipient name is required.")
        raw_emails = raw_row.get("emails", raw_row.get("email", []))
        if isinstance(raw_emails, str):
            raw_emails = [raw_emails]
        if not isinstance(raw_emails, list):
            raise ValueError("Employee recipient emails must be a list or string.")
        emails = unique_non_empty(normalize_text(value).lower() for value in raw_emails)
        unknown = {key: copy.deepcopy(value) for key, value in raw_row.items() if key not in KNOWN_RECIPIENT_FIELDS}
        if unknown:
            unknown_fields.append(
                {
                    "source_ref": canonical_source_ref("feature_flags", "employee_recipients", name),
                    "fields": unknown,
                }
            )
        normalized.append(
            {
                "name": name,
                "enabled": raw_row.get("enabled") if isinstance(raw_row.get("enabled"), bool) else True,
                "emails": emails,
            }
        )
    return normalized, unknown_fields


def read_va_employees(path: Path) -> List[Dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise ValueError("VA root must be an object.")
    if "payload" in payload:
        container = payload.get("payload")
        if not isinstance(container, dict) or not isinstance(container.get("employees"), list):
            raise ValueError("Invalid versioned VA employees payload.")
        rows = container["employees"]
    else:
        if not isinstance(payload.get("employees"), list):
            raise ValueError("Invalid legacy VA employees payload.")
        rows = payload["employees"]
    result = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("VA employee row must be an object.")
        name = normalize_text(row.get("name"))
        if not name:
            raise ValueError("VA employee name is required.")
        result.append(
            {
                "name": name,
                "email": normalize_text(row.get("email")).lower(),
                "phone": normalize_text(row.get("phone")),
                "location": normalize_text(row.get("location")),
                "personnel_number": normalize_text(row.get("personnel_number")),
                "status": normalize_text(row.get("status")),
            }
        )
    return result


def read_duty_schedule_names(path: Path) -> List[str]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise ValueError("Duty schedule root must be an object.")
    names = []
    dates = payload.get("dates")
    if isinstance(dates, dict):
        names.extend(value for value in dates.values() if isinstance(value, str))
    availability = payload.get("availability")
    if isinstance(availability, dict):
        for day_values in availability.values():
            if isinstance(day_values, dict):
                names.extend(key for key in day_values if isinstance(key, str))
    return unique_non_empty(normalize_text(value) for value in names)


def config_fragments(config: Dict[str, List[str]], blocking_errors: List[Dict[str, Any]]) -> List[Fragment]:
    fragments = []
    for order, name in enumerate(config["OPLOT_VALUES"]):
        clean = normalize_text(name)
        fragments.append(
            Fragment(
                source_ref=canonical_source_ref("config", "OPLOT_VALUES", clean),
                source_kind="release_monitor",
                release_name=clean,
                aliases=[{"value": clean, "type": "release", "jira_domain": ""}],
                memberships={"release_monitor": {"enabled": True, "order": order}},
            )
        )
    for source_name, role in (("DASHBOARD_ASSIGNEES", "primary"), ("DASHBOARD_EXTRA_ASSIGNEES", "extra")):
        seen = set()
        for order, jira_name in enumerate(config[source_name]):
            clean_jira = normalize_text(jira_name)
            if clean_jira.casefold() in seen:
                blocking_errors.append({"source": source_name, "error_type": "DuplicateValue"})
            seen.add(clean_jira.casefold())
            full_name = strip_jira_suffix(clean_jira)
            fragments.append(
                Fragment(
                    source_ref=canonical_source_ref("config", source_name, clean_jira),
                    source_kind=f"dashboard_{role}",
                    full_name=full_name,
                    jira_delta=clean_jira,
                    aliases=[{"value": clean_jira, "type": "jira", "jira_domain": "delta"}] if clean_jira != full_name else [],
                    memberships={"duty_dashboard": {"enabled": True, "role": role, "order": order}},
                )
            )
    return fragments


def zni_fragments(names: List[str]) -> List[Fragment]:
    return [
        Fragment(
            source_ref=canonical_source_ref("release_zni", "eligible", name),
            source_kind="release_zni",
            full_name=strip_jira_suffix(name),
            jira_delta=normalize_text(name),
            memberships={"release_zni": {"enabled": True}},
        )
        for name in names
    ]


def recipient_fragments(rows: List[Dict[str, Any]]) -> List[Fragment]:
    return [
        Fragment(
            source_ref=canonical_source_ref("feature_flags", "employee_recipients", row["name"]),
            source_kind="release_notifications",
            release_name=row["name"],
            emails=row["emails"],
            memberships={"release_notifications": {"enabled": row["enabled"]}},
        )
        for row in rows
    ]


def va_fragments(rows: List[Dict[str, Any]]) -> List[Fragment]:
    return [
        Fragment(
            source_ref=canonical_source_ref("va", "employees", row["name"]),
            source_kind="va",
            full_name=row["name"],
            emails=[row["email"]] if row["email"] else [],
            phone=row["phone"],
            location=row["location"],
            personnel_number=row["personnel_number"],
            aliases=[{"value": row["name"], "type": "va", "jira_domain": ""}],
            memberships={"va_schedule_manager": {"enabled": True}},
            diagnostics={"status": row["status"]},
        )
        for row in rows
    ]


def duty_fragments(names: List[str]) -> List[Fragment]:
    return [
        Fragment(
            source_ref=canonical_source_ref("duty_schedule", "employee", name),
            source_kind="duty_schedule",
            full_name=name,
            aliases=[{"value": name, "type": "schedule", "jira_domain": ""}],
        )
        for name in names
    ]


def build_directory_payload(employees: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        "schema_version": 1,
        "revision": 1,
        "created_at": "",
        "created_by": "migrate_employee_directory",
        "updated_at": "",
        "updated_by": "migrate_employee_directory",
        "employees": employees,
    }


def default_memberships() -> Dict[str, Dict[str, Any]]:
    return {
        "release_monitor": {"enabled": False, "order": None},
        "release_zni": {"enabled": False},
        "duty_dashboard": {"enabled": False, "role": "none", "order": None},
        "release_notifications": {"enabled": False},
        "va_schedule_manager": {"enabled": False},
    }


def read_resolutions(path: Path) -> Dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return {"identity_decisions": {}, "field_decisions": {}}
    if not isinstance(payload, dict):
        raise ValueError("resolutions.json root must be an object.")
    return payload


def hash_legacy_files(project_root: Path) -> Dict[str, Optional[str]]:
    result = {}
    for path_id, relative_path in LEGACY_PATHS.items():
        path = project_root / relative_path
        try:
            result[path_id] = f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}"
        except FileNotFoundError:
            result[path_id] = None
    return result


def integrity_rows(before: Dict[str, Optional[str]], after: Dict[str, Optional[str]]) -> List[Dict[str, Any]]:
    return [
        {
            "path_id": path_id,
            "before_hash": before.get(path_id),
            "after_hash": after.get(path_id),
            "unchanged": before.get(path_id) == after.get(path_id),
        }
        for path_id in LEGACY_PATHS
    ]


def atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.tmp")
    try:
        with temp_path.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
        temp_path.replace(path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def make_conflict(conflict_type: str, source_refs: Iterable[str], *, blocking: bool, seed: str = "") -> Dict[str, Any]:
    refs = sorted(set(source_refs))
    return {
        "conflict_id": seed or stable_id(conflict_type, refs),
        "type": conflict_type,
        "blocking": blocking,
        "source_refs": refs,
    }


def stable_id(kind: str, values: Iterable[str]) -> str:
    material = "\n".join([normalize_text(kind)] + sorted(normalize_text(value) for value in values))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]


def decision_resolves(conflict: Dict[str, Any], decisions: Dict[str, Any]) -> bool:
    decision = decisions.get(conflict["conflict_id"])
    return isinstance(decision, dict) and decision.get("action") in {
        "merge",
        "keep_separate",
        "exclude",
        "manual",
    }


def surname_initials_key(value: str) -> str:
    clean = normalize_text(value).replace(" - СРБ", "")
    parts = clean.split()
    if not parts:
        return ""
    surname = parts[0].casefold()
    initials = []
    for part in parts[1:]:
        letters = [char for char in part if char.isalpha()]
        if letters:
            if "." in part and len(letters) > 1:
                initials.extend(char.casefold() for char in letters)
            else:
                initials.append(letters[0].casefold())
        if len(initials) == 2:
            break
    return surname + ":" + "".join(initials[:2])


def strip_jira_suffix(value: str) -> str:
    clean = normalize_text(value)
    return clean[:-len(" - СРБ")] if clean.endswith(" - СРБ") else clean


def unique_non_empty(values: Iterable[Any]) -> List[str]:
    result = []
    seen = set()
    for value in values:
        clean = normalize_text(value)
        key = clean.casefold()
        if clean and key not in seen:
            result.append(clean)
            seen.add(key)
    return result


def unique_aliases(aliases: Iterable[Dict[str, str]]) -> List[Dict[str, str]]:
    result = []
    seen = set()
    for alias in aliases:
        value = normalize_text(alias.get("value"))
        alias_type = normalize_text(alias.get("type")).lower()
        domain = normalize_text(alias.get("jira_domain")).lower()
        key = (alias_type, domain, value.casefold())
        if value and key not in seen:
            result.append({"value": value, "type": alias_type, "jira_domain": domain})
            seen.add(key)
    return result


def unique_items(items: Iterable[Dict[str, Any]], key: str) -> List[Dict[str, Any]]:
    result = []
    seen = set()
    for item in items:
        value = item.get(key)
        if value not in seen:
            result.append(item)
            seen.add(value)
    return result


def employee_sort_key(employee: Dict[str, Any]) -> Tuple[int, int, str]:
    release = employee["memberships"]["release_monitor"]
    dashboard = employee["memberships"]["duty_dashboard"]
    if release["enabled"]:
        return 0, int(release["order"]), employee["full_name"].casefold()
    if dashboard["enabled"]:
        role_order = 0 if dashboard["role"] == "primary" else 1
        return 1 + role_order, int(dashboard["order"]), employee["full_name"].casefold()
    return 3, 0, employee["full_name"].casefold()


def _resolve_under_root(project_root: Path, value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else project_root / path


if __name__ == "__main__":
    raise SystemExit(main())
