from __future__ import annotations

from typing import Any, Dict, List, Optional

from services.employee_directory_repository import normalize_text, read_directory_snapshot


def get_release_monitor_names() -> List[str]:
    return [employee["release_name"] for employee in _ordered_members("release_monitor")]


def get_release_zni_users() -> List[str]:
    return [
        employee["jira_names"]["delta"]
        for employee in sorted(
            _active_employees(),
            key=lambda item: _dashboard_order(item, default=10**9),
        )
        if employee["memberships"]["release_zni"]["enabled"]
        and employee["jira_names"]["delta"]
    ]


def get_dashboard_primary_jira_names() -> List[str]:
    return [employee["jira_names"]["delta"] for employee in _dashboard_members("primary")]


def get_dashboard_extra_jira_names() -> List[str]:
    return [employee["jira_names"]["delta"] for employee in _dashboard_members("extra")]


def get_dashboard_visible_jira_names() -> List[str]:
    return get_dashboard_primary_jira_names() + get_dashboard_extra_jira_names()


def get_dashboard_display_names() -> List[str]:
    return [employee["full_name"] for employee in _dashboard_members("primary")]


def get_dashboard_visible_display_names() -> List[str]:
    return [employee["full_name"] for employee in _dashboard_members("primary") + _dashboard_members("extra")]


def get_release_notification_recipients() -> Dict[str, List[str]]:
    recipients = {}
    for employee in _active_employees():
        if employee["memberships"]["release_notifications"]["enabled"]:
            recipients[employee["release_name"] or employee["full_name"]] = list(employee["emails"])
    return recipients


def resolve_employee_historically(
    value: Any,
    identity_type: Optional[str] = None,
    jira_domain: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    needle = normalize_text(value).casefold()
    if not needle:
        return None
    identity_type = normalize_text(identity_type).lower() or None
    jira_domain = normalize_text(jira_domain).lower() or None
    matches = []
    for employee in _all_employees():
        values = _identity_values(employee, identity_type, jira_domain)
        if needle in {item.casefold() for item in values if item}:
            matches.append(employee)
    return matches[0] if len(matches) == 1 else None


def _identity_values(employee: Dict[str, Any], identity_type: Optional[str], jira_domain: Optional[str]) -> List[str]:
    values: List[str] = []
    if identity_type == "release":
        values.append(employee["release_name"])
    elif identity_type == "jira":
        if jira_domain in {"delta", "sberbank"}:
            values.append(employee["jira_names"][jira_domain])
    elif identity_type in {"schedule", "va", "full"}:
        values.append(employee["full_name"])
    elif identity_type is None:
        values.extend([employee["full_name"], employee["release_name"]])
        values.extend(employee["jira_names"].values())

    for alias in employee["aliases"]:
        alias_type = alias["type"]
        if identity_type is not None and alias_type != identity_type:
            continue
        if alias_type == "jira" and alias["jira_domain"] != jira_domain:
            continue
        values.append(alias["value"])
    return values


def _all_employees() -> List[Dict[str, Any]]:
    snapshot = read_directory_snapshot()
    if snapshot.status != "available" or not snapshot.payload:
        return []
    return list(snapshot.payload["employees"])


def _active_employees() -> List[Dict[str, Any]]:
    return [employee for employee in _all_employees() if employee["enabled"]]


def _ordered_members(membership: str) -> List[Dict[str, Any]]:
    return sorted(
        [employee for employee in _active_employees() if employee["memberships"][membership]["enabled"]],
        key=lambda employee: employee["memberships"][membership]["order"],
    )


def _dashboard_members(role: str) -> List[Dict[str, Any]]:
    return sorted(
        [
            employee
            for employee in _active_employees()
            if employee["memberships"]["duty_dashboard"]["enabled"]
            and employee["memberships"]["duty_dashboard"]["role"] == role
        ],
        key=lambda employee: employee["memberships"]["duty_dashboard"]["order"],
    )


def _dashboard_order(employee: Dict[str, Any], *, default: int) -> int:
    membership = employee["memberships"]["duty_dashboard"]
    order = membership.get("order")
    return order if membership.get("enabled") and isinstance(order, int) else default
