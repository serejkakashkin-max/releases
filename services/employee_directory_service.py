from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from services.employee_directory_repository import (
    DirectorySnapshot,
    normalize_text,
    read_directory_snapshot,
)


CONSUMERS = (
    "release_monitor",
    "release_zni",
    "duty_dashboard",
    "release_notifications",
    "va_schedule_manager",
)


class EmployeeDirectoryUnavailableError(RuntimeError):
    def __init__(
        self,
        status: str,
        consumer: str,
        operation: str,
        revision: Optional[int] = None,
    ) -> None:
        super().__init__(f"Employee directory is unavailable: {status}.")
        self.status = status
        self.consumer = consumer
        self.operation = operation
        self.revision = revision


@dataclass(frozen=True)
class EmployeeDirectoryRuntimeContext:
    status: str
    revision: Optional[int]
    etag: str
    _payload_json: str = ""

    @property
    def payload(self) -> Optional[Dict[str, Any]]:
        return json.loads(self._payload_json) if self._payload_json else None

    @property
    def version_token(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "revision": self.revision,
            "etag": self.etag,
        }


@dataclass(frozen=True)
class IdentityResolution:
    status: str
    employee_id: str = ""
    employee: Optional[Dict[str, Any]] = None
    matched_by: str = ""
    match_count: int = 0

    def as_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "employee_id": self.employee_id,
            "employee": copy.deepcopy(self.employee),
            "matched_by": self.matched_by,
            "match_count": self.match_count,
        }


def load_employee_directory_context(path=None) -> EmployeeDirectoryRuntimeContext:
    snapshot = read_directory_snapshot(path) if path is not None else read_directory_snapshot()
    return context_from_snapshot(snapshot)


def context_from_snapshot(snapshot: DirectorySnapshot) -> EmployeeDirectoryRuntimeContext:
    payload_json = ""
    if snapshot.status == "available" and snapshot.payload is not None:
        payload_json = json.dumps(
            snapshot.payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    return EmployeeDirectoryRuntimeContext(
        status=snapshot.status,
        revision=snapshot.revision,
        etag=snapshot.etag,
        _payload_json=payload_json,
    )


def require_directory_context(
    context: Optional[EmployeeDirectoryRuntimeContext],
    *,
    consumer: str,
    operation: str,
) -> EmployeeDirectoryRuntimeContext:
    resolved = context or load_employee_directory_context()
    if resolved.status != "available" or not resolved.payload:
        raise EmployeeDirectoryUnavailableError(
            resolved.status,
            consumer,
            operation,
            resolved.revision,
        )
    return resolved


def get_consumer_health(
    context: Optional[EmployeeDirectoryRuntimeContext] = None,
    *,
    feature_flags: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    resolved = context or load_employee_directory_context()
    directory = resolved.version_token
    if resolved.status != "available" or not resolved.payload:
        return {
            "directory": directory,
            "consumer_health": {
                name: {"status": "unavailable", "count": 0}
                for name in CONSUMERS
            },
        }

    flags = feature_flags or {}
    employees = _active_employees(resolved)
    release_monitor = _ordered_members(employees, "release_monitor")
    release_zni = [
        employee
        for employee in employees
        if employee["memberships"]["release_zni"]["enabled"]
        and employee["jira_names"]["delta"]
    ]
    dashboard = [
        employee
        for employee in employees
        if employee["memberships"]["duty_dashboard"]["enabled"]
    ]
    notifications = [
        employee
        for employee in employees
        if employee["memberships"]["release_notifications"]["enabled"]
        and employee["emails"]
    ]
    va_members = _ordered_members(employees, "va_schedule_manager")
    notifications_enabled = bool(
        (((flags.get("automation") or {}).get("release_monitor_responsible_email") or {}).get("enabled"))
    )
    va_enabled = bool(
        (((flags.get("modules") or {}).get("va_schedule_manager") or {}).get("enabled"))
    )

    return {
        "directory": directory,
        "consumer_health": {
            "release_monitor": _health(release_monitor, required=True),
            "release_zni": _health(release_zni, required=True),
            "duty_dashboard": _health(dashboard, required=True),
            "release_notifications": _health(
                notifications,
                required=notifications_enabled,
                disabled=not notifications_enabled,
            ),
            "va_schedule_manager": _health(
                va_members,
                required=va_enabled,
                disabled=not va_enabled,
            ),
        },
    }


def get_release_monitor_projection(
    context: Optional[EmployeeDirectoryRuntimeContext] = None,
) -> Dict[str, Any]:
    resolved = require_directory_context(
        context,
        consumer="release_monitor",
        operation="projection",
    )
    members = _ordered_members(_active_employees(resolved), "release_monitor")
    return {
        **resolved.version_token,
        "status": "ready" if members else "empty_membership",
        "count": len(members),
        "names": [item["release_name"] for item in members],
    }


def get_release_monitor_names(
    context: Optional[EmployeeDirectoryRuntimeContext] = None,
) -> List[str]:
    return list(get_release_monitor_projection(context)["names"])


def get_release_zni_users(
    context: Optional[EmployeeDirectoryRuntimeContext] = None,
) -> List[str]:
    resolved = require_directory_context(
        context,
        consumer="release_zni",
        operation="projection",
    )
    employees = _active_employees(resolved)
    indexed = list(enumerate(employees))
    eligible = [
        (record_index, employee)
        for record_index, employee in indexed
        if employee["memberships"]["release_zni"]["enabled"]
        and employee["jira_names"]["delta"]
    ]
    eligible.sort(
        key=lambda row: (
            _dashboard_order(row[1], default=10**9),
            row[0],
        )
    )
    return [employee["jira_names"]["delta"] for _, employee in eligible]


def get_dashboard_projection(
    context: Optional[EmployeeDirectoryRuntimeContext] = None,
) -> Dict[str, Any]:
    resolved = require_directory_context(
        context,
        consumer="duty_dashboard",
        operation="projection",
    )
    employees = _active_employees(resolved)
    primary = _dashboard_members(employees, "primary")
    extra = _dashboard_members(employees, "extra")
    visible = primary + extra
    return {
        **resolved.version_token,
        "status": "ready" if primary else "empty_membership",
        "primary_jira": [item["jira_names"]["delta"] for item in primary],
        "extra_jira": [item["jira_names"]["delta"] for item in extra],
        "visible_jira": [item["jira_names"]["delta"] for item in visible],
        "primary_display": [item["full_name"] for item in primary],
        "extra_display": [item["full_name"] for item in extra],
        "visible_display": [item["full_name"] for item in visible],
    }


def get_release_notification_recipients(
    context: Optional[EmployeeDirectoryRuntimeContext] = None,
) -> Dict[str, List[str]]:
    resolved = require_directory_context(
        context,
        consumer="release_notifications",
        operation="projection",
    )
    recipients: Dict[str, List[str]] = {}
    for employee in _active_employees(resolved):
        if employee["memberships"]["release_notifications"]["enabled"]:
            key = employee["release_name"] or employee["full_name"]
            recipients[key] = list(employee["emails"])
    return recipients


def get_va_members(
    context: Optional[EmployeeDirectoryRuntimeContext] = None,
) -> List[Dict[str, Any]]:
    resolved = require_directory_context(
        context,
        consumer="va_schedule_manager",
        operation="projection",
    )
    return copy.deepcopy(
        _ordered_members(_active_employees(resolved), "va_schedule_manager")
    )


def resolve_employee_identity(
    value: Any,
    *,
    context: EmployeeDirectoryRuntimeContext,
    identity_type: Optional[str] = None,
    jira_domain: Optional[str] = None,
    include_disabled_for_history: bool = False,
) -> IdentityResolution:
    require_directory_context(
        context,
        consumer="identity",
        operation="resolve",
    )
    needle = normalize_text(value).casefold()
    if not needle:
        return IdentityResolution("unresolved")
    normalized_type = normalize_text(identity_type).lower() or None
    normalized_domain = normalize_text(jira_domain).lower() or None
    matches: List[tuple] = []
    for employee in _all_employees(context):
        if not include_disabled_for_history and not employee["enabled"]:
            continue
        matched_by = _match_identity(
            employee,
            needle,
            normalized_type,
            normalized_domain,
        )
        if matched_by:
            matches.append((employee, matched_by))
    if not matches:
        return IdentityResolution("unresolved")
    employee_ids = {item[0]["employee_id"] for item in matches}
    if len(employee_ids) != 1:
        return IdentityResolution("ambiguous", match_count=len(employee_ids))
    employee, matched_by = matches[0]
    return IdentityResolution(
        "resolved",
        employee_id=employee["employee_id"],
        employee=copy.deepcopy(employee),
        matched_by=matched_by,
        match_count=1,
    )


def resolve_employee_historically(
    value: Any,
    identity_type: Optional[str] = None,
    jira_domain: Optional[str] = None,
    *,
    context: Optional[EmployeeDirectoryRuntimeContext] = None,
) -> Optional[Dict[str, Any]]:
    resolved_context = require_directory_context(
        context,
        consumer="identity",
        operation="historical_resolve",
    )
    result = resolve_employee_identity(
        value,
        identity_type=identity_type,
        jira_domain=jira_domain,
        context=resolved_context,
        include_disabled_for_history=True,
    )
    return copy.deepcopy(result.employee) if result.status == "resolved" else None


def resolve_historical_va_employee(
    historical_name: Any,
    context: EmployeeDirectoryRuntimeContext,
) -> IdentityResolution:
    require_directory_context(
        context,
        consumer="va_schedule_manager",
        operation="historical_resolve",
    )
    needle = normalize_text(historical_name).casefold()
    if not needle:
        return IdentityResolution("unresolved")
    matches = []
    for employee in _all_employees(context):
        current_values = {normalize_text(employee.get("full_name")).casefold()}
        alias_values = {
            normalize_text(alias.get("value")).casefold()
            for alias in employee.get("aliases") or []
            if alias.get("type") in {"full", "schedule", "va"}
        }
        source_values = {
            normalize_text(source_ref.partition("va:employees:")[2]).casefold()
            for source_ref in employee.get("source_refs") or []
            if source_ref.startswith("va:employees:")
        }
        matched_by = ""
        if needle in current_values:
            matched_by = "current"
        elif needle in alias_values:
            matched_by = "alias"
        elif needle in source_values:
            matched_by = "source_ref"
        if matched_by:
            matches.append((employee, matched_by))
    employee_ids = {employee["employee_id"] for employee, _ in matches}
    if not employee_ids:
        return IdentityResolution("unresolved")
    if len(employee_ids) != 1:
        return IdentityResolution("ambiguous", match_count=len(employee_ids))
    employee, matched_by = matches[0]
    return IdentityResolution(
        "resolved",
        employee_id=employee["employee_id"],
        employee=copy.deepcopy(employee),
        matched_by=matched_by,
        match_count=1,
    )


def get_va_schedule_display_name(
    employee: Dict[str, Any],
    context: EmployeeDirectoryRuntimeContext,
) -> str:
    employee_id = str(employee.get("employee_id") or "")
    current = next(
        (item for item in _all_employees(context) if item["employee_id"] == employee_id),
        None,
    )
    if not current:
        return ""
    for alias_type in ("schedule", "va"):
        for alias in current["aliases"]:
            if alias["type"] == alias_type and alias["value"]:
                return alias["value"]
    for source_ref in current["source_refs"]:
        if source_ref.startswith("va:employees:"):
            return source_ref.partition("va:employees:")[2]
    return current["full_name"]


def _match_identity(
    employee: Dict[str, Any],
    needle: str,
    identity_type: Optional[str],
    jira_domain: Optional[str],
) -> str:
    current_values: List[str] = []
    if identity_type == "release":
        current_values.append(employee["release_name"])
    elif identity_type == "jira":
        if jira_domain in {"delta", "sberbank"}:
            current_values.append(employee["jira_names"][jira_domain])
    elif identity_type in {"schedule", "va", "full"}:
        current_values.append(employee["full_name"])
    elif identity_type is None:
        current_values.extend([employee["full_name"], employee["release_name"]])
        current_values.extend(employee["jira_names"].values())
    if needle in {normalize_text(item).casefold() for item in current_values if item}:
        return "current"

    aliases = []
    for alias in employee["aliases"]:
        if identity_type is not None and alias["type"] != identity_type:
            continue
        if alias["type"] == "jira" and alias["jira_domain"] != jira_domain:
            continue
        aliases.append(alias["value"])
    if needle in {normalize_text(item).casefold() for item in aliases if item}:
        return "alias"

    if identity_type in {"schedule", "va", None}:
        for source_ref in employee["source_refs"]:
            if source_ref.startswith("va:employees:"):
                if normalize_text(source_ref.partition("va:employees:")[2]).casefold() == needle:
                    return "source_ref"
    return ""


def _all_employees(context: EmployeeDirectoryRuntimeContext) -> List[Dict[str, Any]]:
    payload = context.payload or {}
    return list(payload.get("employees") or [])


def _active_employees(context: EmployeeDirectoryRuntimeContext) -> List[Dict[str, Any]]:
    return [employee for employee in _all_employees(context) if employee["enabled"]]


def _ordered_members(employees: List[Dict[str, Any]], membership: str) -> List[Dict[str, Any]]:
    return sorted(
        [
            employee
            for employee in employees
            if employee["memberships"][membership]["enabled"]
        ],
        key=lambda employee: employee["memberships"][membership]["order"],
    )


def _dashboard_members(employees: List[Dict[str, Any]], role: str) -> List[Dict[str, Any]]:
    return sorted(
        [
            employee
            for employee in employees
            if employee["memberships"]["duty_dashboard"]["enabled"]
            and employee["memberships"]["duty_dashboard"]["role"] == role
        ],
        key=lambda employee: employee["memberships"]["duty_dashboard"]["order"],
    )


def _dashboard_order(employee: Dict[str, Any], *, default: int) -> int:
    membership = employee["memberships"]["duty_dashboard"]
    order = membership.get("order")
    return order if membership.get("enabled") and isinstance(order, int) else default


def _health(items: List[Any], *, required: bool, disabled: bool = False) -> Dict[str, Any]:
    if disabled:
        return {"status": "disabled_not_required", "count": len(items)}
    if items:
        return {"status": "ready", "count": len(items)}
    return {
        "status": "invalid_contract" if required else "empty_membership",
        "count": 0,
    }
