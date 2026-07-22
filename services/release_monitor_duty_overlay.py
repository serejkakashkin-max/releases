from __future__ import annotations

import copy
from datetime import datetime
from typing import Any, Dict, Iterable, List


MANUAL_REVIEWER_SOURCES = {"manual", "manual_text"}


def _assignment_key(item: Dict[str, Any]) -> str:
    row_key = str(item.get("row_key") or "").strip()
    if row_key:
        return row_key
    release_key = str(item.get("release_key") or "").strip()
    rov_key = str(item.get("rov_key") or "").strip()
    return f"{release_key}::{rov_key or 'no-rov'}" if release_key else ""


def _parse_deployment_date(item: Dict[str, Any]):
    value = str(item.get("deployment_start_iso") or item.get("deployment_start") or "").strip()
    if not value:
        return None
    for fmt in (
        "%Y-%m-%dT%H:%M:%S.%f%z",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
        "%d.%m.%Y",
        "%d.%m.%Y %H:%M",
        "%d.%m.%Y %H:%M:%S",
    ):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return None


def is_explicit_manual_reviewer(assignment: Dict[str, Any]) -> bool:
    return bool(
        isinstance(assignment, dict)
        and str(assignment.get("reviewer_source") or "").strip() in MANUAL_REVIEWER_SOURCES
        and str(assignment.get("reviewer_date") or "").strip() == "manual"
    )


def apply_duty_schedule_overlay(
    items: Iterable[Dict[str, Any]],
    projection: Dict[str, Any],
    assignments: Dict[str, Dict[str, Any]],
) -> Dict[str, Any]:
    source_items = list(items or [])
    projection_copy = copy.deepcopy(projection or {})
    assignments_copy = copy.deepcopy(assignments or {})
    authoritative = bool(projection_copy.get("authoritative"))
    provider_status = str(projection_copy.get("status") or "missing_provider")
    date_states = projection_copy.get("date_states") or {}
    changes = {
        "effective_updated": 0,
        "effective_cleared": 0,
        "manual_preserved": 0,
        "stale_suppressed": 0,
        "ambiguous_skipped": 0,
    }
    result_items: List[Dict[str, Any]] = []

    for source_item in source_items:
        item = copy.deepcopy(source_item)
        key = _assignment_key(item)
        assignment = copy.deepcopy(assignments_copy.get(key) or assignments_copy.get(str(item.get("release_key") or "")) or {})
        reviewer = str(assignment.get("reviewer") or item.get("psi_owner") or "").strip()

        if is_explicit_manual_reviewer(assignment):
            item["psi_owner"] = reviewer
            item["psi_owner_source"] = str(assignment.get("reviewer_source") or "").strip()
            item["psi_owner_date"] = "manual"
            item["psi_owner_stale"] = False
            item["psi_owner_schedule_status"] = "manual"
            changes["manual_preserved"] += 1
            result_items.append(item)
            continue

        deployment_date = _parse_deployment_date(item)
        date_key = deployment_date.isoformat() if deployment_date else ""
        state_info = copy.deepcopy(date_states.get(date_key) or {}) if date_key else {}
        if state_info:
            state = str(state_info.get("state") or "provider_unavailable")
        elif authoritative and date_key:
            state = "month_unavailable"
        else:
            state = "provider_unavailable"
        scheduled = str(state_info.get("reviewer") or "").strip()

        if deployment_date and deployment_date.weekday() >= 5:
            state = "no_duty" if authoritative else "provider_unavailable"
            scheduled = ""

        if authoritative and state == "ready" and scheduled:
            item["psi_owner"] = scheduled
            item["psi_owner_source"] = "duty_schedule"
            item["psi_owner_date"] = date_key
            item["psi_owner_stale"] = False
            item["psi_owner_schedule_status"] = "ready"
            changes["effective_updated"] += 1
        elif authoritative and state == "no_duty":
            item["psi_owner"] = ""
            item["psi_owner_source"] = ""
            item["psi_owner_date"] = ""
            item["psi_owner_stale"] = False
            item["psi_owner_schedule_status"] = "no_duty"
            changes["effective_cleared"] += 1
        else:
            item["psi_owner"] = ""
            item["psi_owner_source"] = ""
            item["psi_owner_date"] = ""
            item["psi_owner_stale"] = bool(reviewer)
            item["psi_owner_stored_value_present"] = bool(reviewer)
            item["psi_owner_schedule_status"] = state if authoritative else provider_status
            if state in {"ambiguous", "unmapped", "month_unavailable"}:
                changes["ambiguous_skipped"] += 1
            elif reviewer:
                changes["stale_suppressed"] += 1

        item["psi_checker"] = str(assignment.get("checker") or item.get("psi_checker") or "").strip()
        responsibles = assignment.get("responsibles", item.get("psi_responsibles") or [])
        item["psi_responsibles"] = copy.deepcopy(responsibles if isinstance(responsibles, list) else [responsibles])
        result_items.append(item)

    return {"items": result_items, "changes": changes}


def get_effective_release_reviewer(item: Dict[str, Any]) -> str:
    source = str((item or {}).get("psi_owner_source") or "").strip()
    reviewer_date = str((item or {}).get("psi_owner_date") or "").strip()
    reviewer = str((item or {}).get("psi_owner") or "").strip()
    if source in MANUAL_REVIEWER_SOURCES and reviewer_date == "manual":
        return reviewer
    if (
        source == "duty_schedule"
        and str((item or {}).get("psi_owner_schedule_status") or "") == "ready"
        and not bool((item or {}).get("psi_owner_stale"))
    ):
        return reviewer
    return ""
