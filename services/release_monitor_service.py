import logging
import threading
import time
from collections import defaultdict
from datetime import datetime

import requests

from config import DASHBOARD_CACHE_TTL
from services.jira_service import get_jira_domain_and_token


FINAL_RELEASE_STATUSES = (
    "Установлен на ПРОМ",
    "Отменено",
)
PRE_FINAL_RELEASE_STATUS = "Установка на ПРОМ"
RELEASE_PREFIXES = ("EMRM", "SMECLM", "SMECSC")
RELEASE_ISSUE_TYPE = "Release 2.0"

FIELD_FALLBACKS = {
    "planned_psi_start": "customfield_24200",
    "planned_psi_end": "customfield_24201",
    "planned_prom_end": "customfield_18606",
    "system_info": "customfield_22400",
    "sm_id": "customfield_18300",
}

FIELD_ALIASES = {
    "planned_psi_start": (
        "начало пси",
        "дата начала пси",
        "начало пси план",
    ),
    "planned_psi_end": (
        "окончание пси",
        "дата окончания пси",
        "окончание пси план",
    ),
    "planned_prom_end": (
        "дата завершения установки в пром",
        "завершение установки в пром",
        "дата установки в пром",
        "дата завершения установки на пром",
    ),
    "system_info": (
        "ит-услуга",
        "ит услуга",
        "ке",
    ),
    "sm_id": (
        "smid",
        "sm id",
    ),
}

_cache_lock = threading.Lock()
_cached_data = None
_last_cache_update = None
_field_map_cache = {}


def _build_empty_release_monitor_payload():
    return {
        "items": [],
        "summary": {
            "total": 0,
            "overdue": 0,
            "today": 0,
            "planned": 0,
            "no_date": 0,
            "pre_final": 0,
            "by_system": {},
            "by_status": {},
        },
        "meta": {
            "final_status": FINAL_RELEASE_STATUSES[0],
            "final_statuses": list(FINAL_RELEASE_STATUSES),
            "pre_final_status": PRE_FINAL_RELEASE_STATUS,
            "prefixes": list(RELEASE_PREFIXES),
            "last_updated": None,
            "is_cached": False,
        },
    }


def _normalize_text(value):
    return (value or "").strip().lower().replace("ё", "е")


def _parse_jira_date(value):
    if not value:
        return None

    for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


def _extract_field_value(raw_value):
    if raw_value is None:
        return None
    if isinstance(raw_value, list):
        values = []
        for item in raw_value:
            if isinstance(item, dict):
                values.append(item.get("value") or item.get("name") or item.get("id"))
            else:
                values.append(str(item))
        return ", ".join(str(v) for v in values if v)
    if isinstance(raw_value, dict):
        return raw_value.get("value") or raw_value.get("name") or raw_value.get("id")
    return raw_value


def _get_domain_groups():
    groups = {}
    for prefix in RELEASE_PREFIXES:
        domain, token = get_jira_domain_and_token(f"{prefix}-1")
        key = (domain, token)
        groups.setdefault(key, []).append(prefix)
    return groups


def _fetch_field_name_map(domain, token):
    cache_key = domain
    cached = _field_map_cache.get(cache_key)
    if cached:
        return cached

    url = f"{domain}/rest/api/2/field"
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}

    try:
        response = requests.get(url, headers=headers, verify=False, timeout=30)
        response.raise_for_status()
        field_data = response.json()
        field_map = {item.get("id"): item.get("name", "") for item in field_data}
        _field_map_cache[cache_key] = field_map
        return field_map
    except Exception as exc:
        logging.warning("Release monitor: failed to fetch field map from %s: %s", domain, exc)
        field_map = {}
        _field_map_cache[cache_key] = field_map
        return field_map


def _resolve_field_ids(domain, token):
    field_name_map = _fetch_field_name_map(domain, token)
    resolved = {}

    for logical_name, aliases in FIELD_ALIASES.items():
        fallback_id = FIELD_FALLBACKS[logical_name]
        resolved[logical_name] = fallback_id

        for field_id, field_name in field_name_map.items():
            normalized_name = _normalize_text(field_name)
            if all(alias in normalized_name for alias in aliases[0].split()):
                resolved[logical_name] = field_id
                break

        if resolved[logical_name] == fallback_id:
            for field_id, field_name in field_name_map.items():
                normalized_name = _normalize_text(field_name)
                if any(alias in normalized_name for alias in aliases):
                    resolved[logical_name] = field_id
                    break

    return resolved


def _execute_release_search(domain, token, prefix, fields_to_load):
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    url = f"{domain}/rest/api/2/search"
    start_at = 0
    issues = []

    final_statuses_jql = ", ".join(f'"{status}"' for status in FINAL_RELEASE_STATUSES)
    jql = (
        f'project = {prefix} AND '
        f'issuetype = "{RELEASE_ISSUE_TYPE}" AND '
        f'status NOT IN ({final_statuses_jql}) '
        f'ORDER BY updated DESC'
    )

    while True:
        params = {
            "jql": jql,
            "startAt": start_at,
            "maxResults": 100,
            "fields": ",".join(fields_to_load),
        }
        response = requests.get(url, headers=headers, params=params, verify=False, timeout=60)
        response.raise_for_status()
        data = response.json()
        batch = data.get("issues", [])
        issues.extend(batch)

        if start_at + len(batch) >= data.get("total", 0) or not batch:
            break
        start_at += len(batch)

    return issues


def _detect_system(prefix, summary, system_info_text):
    searchable = _normalize_text(f"{summary} {system_info_text}")

    if "аист" in searchable or "aist" in searchable:
        return "АИСТ"
    if "clm" in searchable or prefix in {"SMECLM", "SMECSC"}:
        return "CLM"
    return "Фокус"


def _build_release_record(issue, domain, prefix, resolved_fields):
    fields = issue.get("fields", {})
    status_name = fields.get("status", {}).get("name", "")
    assignee = fields.get("assignee")
    reporter = fields.get("reporter")
    summary = fields.get("summary", "")

    planned_prom_end_raw = fields.get(resolved_fields["planned_prom_end"])
    planned_psi_start_raw = fields.get(resolved_fields["planned_psi_start"])
    planned_psi_end_raw = fields.get(resolved_fields["planned_psi_end"])
    system_info_raw = fields.get(resolved_fields["system_info"])
    sm_info_raw = fields.get(resolved_fields["sm_id"])

    planned_prom_end = _parse_jira_date(_extract_field_value(planned_prom_end_raw))
    planned_psi_start = _parse_jira_date(_extract_field_value(planned_psi_start_raw))
    planned_psi_end = _parse_jira_date(_extract_field_value(planned_psi_end_raw))
    system_info_text = _extract_field_value(system_info_raw) or ""

    now = datetime.now()
    today = now.date()
    planned_prom_end_date = planned_prom_end.date() if planned_prom_end else None

    is_final = _normalize_text(status_name) in {
        _normalize_text(status) for status in FINAL_RELEASE_STATUSES
    }
    is_pre_final = _normalize_text(status_name) == _normalize_text(PRE_FINAL_RELEASE_STATUS)
    is_overdue = bool(planned_prom_end_date and planned_prom_end_date < today and not is_final)
    is_today = bool(planned_prom_end_date and planned_prom_end_date == today and not is_final)

    if is_overdue:
        timeline_state = "overdue"
    elif is_today:
        timeline_state = "today"
    elif planned_prom_end_date:
        timeline_state = "planned"
    else:
        timeline_state = "no_date"

    days_overdue = (today - planned_prom_end_date).days if is_overdue else 0
    sm_id = None
    if isinstance(sm_info_raw, list) and sm_info_raw:
        first = sm_info_raw[0]
        if isinstance(first, dict):
            sm_id = first.get("id") or first.get("smId")

    return {
        "key": issue.get("key"),
        "summary": summary,
        "status": status_name,
        "resolution": (fields.get("resolution") or {}).get("name", ""),
        "issue_type": (fields.get("issuetype") or {}).get("name", ""),
        "priority": (fields.get("priority") or {}).get("name", ""),
        "assignee_name": assignee.get("displayName", "Не назначен") if assignee else "Не назначен",
        "reporter_name": reporter.get("displayName", "Не указан") if reporter else "Не указан",
        "created": fields.get("created", ""),
        "updated": fields.get("updated", ""),
        "planned_prom_end": planned_prom_end.strftime("%d.%m.%Y") if planned_prom_end else "",
        "planned_prom_end_iso": planned_prom_end_date.isoformat() if planned_prom_end_date else "",
        "planned_psi_start": planned_psi_start.strftime("%d.%m.%Y %H:%M") if planned_psi_start else "",
        "planned_psi_end": planned_psi_end.strftime("%d.%m.%Y %H:%M") if planned_psi_end else "",
        "timeline_state": timeline_state,
        "days_overdue": days_overdue,
        "is_final": is_final,
        "is_pre_final": is_pre_final,
        "system_name": _detect_system(prefix, summary, system_info_text),
        "source_prefix": prefix,
        "system_info": system_info_text,
        "sm_id": sm_id,
        "url": f"{domain}/browse/{issue.get('key')}",
    }


def _sort_release_records(records):
    state_order = {
        "overdue": 0,
        "today": 1,
        "no_date": 2,
        "planned": 3,
    }

    def sort_key(item):
        planned_date = item.get("planned_prom_end_iso") or "9999-12-31"
        return (
            state_order.get(item.get("timeline_state"), 9),
            -item.get("days_overdue", 0),
            planned_date,
            item.get("status", ""),
            item.get("key", ""),
        )

    records.sort(key=sort_key)
    return records


def _build_summary(records):
    summary = {
        "total": len(records),
        "overdue": 0,
        "today": 0,
        "planned": 0,
        "no_date": 0,
        "pre_final": 0,
        "by_system": defaultdict(int),
        "by_status": defaultdict(int),
    }

    for item in records:
        timeline_state = item.get("timeline_state", "planned")
        summary[timeline_state] += 1
        if item.get("is_pre_final"):
            summary["pre_final"] += 1
        summary["by_system"][item.get("system_name") or "Не определено"] += 1
        summary["by_status"][item.get("status") or "Не указан"] += 1

    summary["by_system"] = dict(sorted(summary["by_system"].items()))
    summary["by_status"] = dict(
        sorted(summary["by_status"].items(), key=lambda pair: (-pair[1], pair[0]))
    )
    return summary


def _fetch_release_monitor_data():
    all_records = []

    for (domain, token), prefixes in _get_domain_groups().items():
        resolved_fields = _resolve_field_ids(domain, token)
        fields_to_load = {
            "key",
            "summary",
            "status",
            "resolution",
            "assignee",
            "reporter",
            "created",
            "updated",
            "issuetype",
            "priority",
            resolved_fields["planned_psi_start"],
            resolved_fields["planned_psi_end"],
            resolved_fields["planned_prom_end"],
            resolved_fields["system_info"],
            resolved_fields["sm_id"],
        }

        for prefix in prefixes:
            try:
                issues = _execute_release_search(domain, token, prefix, sorted(fields_to_load))
                logging.info(
                    "Release monitor: loaded %s non-final releases for prefix %s",
                    len(issues),
                    prefix,
                )
                for issue in issues:
                    all_records.append(_build_release_record(issue, domain, prefix, resolved_fields))
            except Exception as exc:
                logging.error(
                    "Release monitor: failed to load releases for prefix %s: %s",
                    prefix,
                    exc,
                )

    _sort_release_records(all_records)
    return {
        "items": all_records,
        "summary": _build_summary(all_records),
        "meta": {
            "final_status": FINAL_RELEASE_STATUSES[0],
            "final_statuses": list(FINAL_RELEASE_STATUSES),
            "pre_final_status": PRE_FINAL_RELEASE_STATUS,
            "prefixes": list(RELEASE_PREFIXES),
            "last_updated": datetime.now().strftime("%d.%m.%Y %H:%M:%S"),
            "is_cached": True,
        },
    }


def get_release_monitor_data(force_refresh=False):
    global _cached_data, _last_cache_update

    with _cache_lock:
        now = time.time()
        if (
            not force_refresh
            and _cached_data is not None
            and _last_cache_update is not None
            and (now - _last_cache_update) < DASHBOARD_CACHE_TTL
        ):
            return _cached_data

        _cached_data = _fetch_release_monitor_data()
        _last_cache_update = now
        return _cached_data


def get_release_monitor_snapshot():
    with _cache_lock:
        if _cached_data is None:
            return _build_empty_release_monitor_payload()

        return {
            "items": list(_cached_data.get("items", [])),
            "summary": dict(_cached_data.get("summary", {})),
            "meta": {
                **dict(_cached_data.get("meta", {})),
                "is_cached": True,
            },
        }


def clear_release_monitor_cache():
    global _cached_data, _last_cache_update
    with _cache_lock:
        _cached_data = None
        _last_cache_update = None
