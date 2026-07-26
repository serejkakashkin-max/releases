from __future__ import annotations

from typing import Any, Dict, List


def validate_employee_directory_operations(
    payload: Dict[str, Any],
    feature_flags_snapshot: Dict[str, Any],
    *,
    va_settings_snapshot=None,
) -> List[Dict[str, str]]:
    errors: List[Dict[str, str]] = []
    employees = [
        item
        for item in payload.get("employees", [])
        if item.get("enabled")
    ]
    release = _members(employees, "release_monitor")
    zni = [
        item
        for item in _members(employees, "release_zni")
        if (item.get("jira_names") or {}).get("delta")
    ]
    dashboard_primary = [
        item
        for item in _members(employees, "duty_dashboard")
        if item["memberships"]["duty_dashboard"].get("role") == "primary"
    ]
    notifications = _members(employees, "release_notifications")
    va_members = _members(employees, "va_schedule_manager")

    if not release:
        errors.append(_error("release_monitor", "active_member_required"))
    if not zni:
        errors.append(_error("release_zni", "delta_member_required"))
    if not dashboard_primary:
        errors.append(_error("duty_dashboard", "primary_member_required"))

    notification_enabled = bool(
        (
            ((feature_flags_snapshot.get("automation") or {}).get("release_monitor_responsible_email") or {})
            .get("enabled")
        )
    )
    if notification_enabled and not notifications:
        errors.append(_error("release_notifications", "recipient_required_when_enabled"))
    if notification_enabled and any(not item.get("emails") for item in notifications):
        errors.append(_error("release_notifications", "email_required"))

    va_enabled = bool(
        (((feature_flags_snapshot.get("modules") or {}).get("va_schedule_manager") or {}).get("enabled"))
    )
    if va_enabled and not va_members:
        errors.append(_error("va_schedule_manager", "active_member_required_when_enabled"))
    if va_enabled and va_settings_snapshot is not None:
        if va_settings_snapshot.status != "available" or not va_settings_snapshot.payload:
            errors.append(_error("va_schedule_manager", "settings_migration_required"))
        else:
            migration = va_settings_snapshot.payload.get("migration") or {}
            if migration.get("status") not in {"complete", "not_required"}:
                errors.append(_error("va_schedule_manager", "settings_migration_required"))
            if any(int(migration.get(key) or 0) for key in ("unresolved", "ambiguous", "conflicts")):
                errors.append(_error("va_schedule_manager", "settings_migration_incomplete"))
    return errors


def _members(employees: List[Dict[str, Any]], membership: str) -> List[Dict[str, Any]]:
    return [
        item
        for item in employees
        if ((item.get("memberships") or {}).get(membership) or {}).get("enabled")
    ]


def _error(path: str, code: str) -> Dict[str, str]:
    return {"path": path, "code": code}
