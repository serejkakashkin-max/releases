import logging
import json
import os
import re
import threading
import time
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

import requests

from config import DASHBOARD_CACHE_TTL, OPLOT_VALUES
from services.jira_service import get_jira_domain_and_token


FINAL_RELEASE_STATUS = "Установлен на ПРОМ"
CANCELLED_RELEASE_STATUS = "Отменено"
FINAL_RELEASE_STATUSES = (
    FINAL_RELEASE_STATUS,
    CANCELLED_RELEASE_STATUS,
)
PRE_FINAL_RELEASE_STATUSES = (
    "Установка на ПРОМ",
    "Готов к установке на ПРОМ",
)
RELEASE_PREFIXES = ("EMRM", "SMECLM", "SMECSC", "HELPERAI", "AIGAS")
RELEASE_ISSUE_TYPE = "Release 2.0"
ROV_ISSUE_TYPE = "Introduction Order"
QUICK_REFRESH_DAYS = 14
AUTO_FULL_REFRESH_HOUR = 6
AUTO_REFRESH_CHECK_INTERVAL = 300
SNAPSHOT_DIR = Path(__file__).resolve().parent.parent / "cache"
SNAPSHOT_FILE = SNAPSHOT_DIR / "release_monitor_snapshot.json"
REVIEWERS_FILE = SNAPSHOT_DIR / "release_monitor_reviewers.json"

FIELD_FALLBACKS = {
    "planned_prom_start": None,
    "planned_prom_end": "customfield_18606",
    "system_info": "customfield_22400",
    "ke_object": "customfield_18300",
    "release_distributive": "customfield_21710",
    "delta_release_distributive": "customfield_27011",
    "rov_start": None,
    "rov_end": None,
}

FIELD_ALIASES = {
    "planned_prom_start": (
        "начало внедрения",
        "дата начала внедрения",
        "начало внедрения план",
    ),
    "planned_prom_end": (
        "дата завершения установки в пром",
        "дата установки в пром",
    ),
    "system_info": (
        "ит-услуга",
        "кэ",
    ),
    "ke_object": (
        "кэ",
    ),
    "release_distributive": (
        "кэ дистрибутива",
        "кэ дистрибутивов",
    ),
    "rov_start": (
        "дата/время начала работ по внедрению",
        "дата время начала работ по внедрению",
        "начало работ по внедрению",
    ),
    "rov_end": (
        "дата/время окончания работ по внедрению",
        "дата время окончания работ по внедрению",
        "окончание работ по внедрению",
    ),
}

_cache_lock = threading.Lock()
_cached_data = None
_last_cache_update = None
_field_map_cache = {}
_refresh_thread = None
_scheduler_thread = None
_scheduler_started = False
_refresh_status = {
    "state": "idle",
    "message": "",
    "started_at": None,
    "finished_at": None,
    "error": None,
    "mode": None,
    "trigger": None,
}


def _build_empty_release_monitor_payload():
    current_year = datetime.now().year
    previous_year = current_year - 1
    return {
        "items": [],
        "summary": {
            "total": 0,
            "non_final": 0,
            "overdue": 0,
            "today": 0,
            "pre_final": 0,
            "final": 0,
            "cancelled": 0,
            "by_status": {},
            "by_year": {
                str(current_year): {
                    "total": 0,
                    "non_final": 0,
                    "overdue": 0,
                    "today": 0,
                    "pre_final": 0,
                    "final": 0,
                    "cancelled": 0,
                },
                str(previous_year): {
                    "total": 0,
                    "non_final": 0,
                    "overdue": 0,
                    "today": 0,
                    "pre_final": 0,
                    "final": 0,
                    "cancelled": 0,
                },
            },
        },
        "meta": {
            "final_status": FINAL_RELEASE_STATUS,
            "cancelled_status": CANCELLED_RELEASE_STATUS,
            "final_statuses": list(FINAL_RELEASE_STATUSES),
            "pre_final_statuses": list(PRE_FINAL_RELEASE_STATUSES),
            "prefixes": list(RELEASE_PREFIXES),
            "years": [current_year, previous_year],
            "current_year": current_year,
            "last_updated": None,
            "last_full_sync": None,
            "last_quick_sync": None,
            "last_sync_mode": None,
            "quick_refresh_days": QUICK_REFRESH_DAYS,
            "auto_full_refresh_hour": AUTO_FULL_REFRESH_HOUR,
            "is_cached": False,
        },
    }


def _ensure_snapshot_dir():
    os.makedirs(SNAPSHOT_DIR, exist_ok=True)


def _save_snapshot_to_disk(payload):
    try:
        _ensure_snapshot_dir()
        SNAPSHOT_FILE.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as exc:
        logging.warning("Release monitor: failed to save snapshot to disk: %s", exc)


def _load_snapshot_from_disk():
    if not SNAPSHOT_FILE.exists():
        return None

    try:
        payload = json.loads(SNAPSHOT_FILE.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return None
        return payload
    except Exception as exc:
        logging.warning("Release monitor: failed to load snapshot from disk: %s", exc)
        return None


def _load_reviewer_assignments():
    if not REVIEWERS_FILE.exists():
        return {}

    try:
        payload = json.loads(REVIEWERS_FILE.read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            normalized = {}
            for key, value in payload.items():
                release_key = str(key)
                if isinstance(value, dict):
                    raw_responsibles = value.get("responsibles", [])
                    if not isinstance(raw_responsibles, list):
                        raw_responsibles = [raw_responsibles] if raw_responsibles else []
                    normalized[release_key] = {
                        "reviewer": str(value.get("reviewer", "") or "").strip(),
                        "checker": str(value.get("checker", "") or "").strip(),
                        "responsibles": [
                            str(item or "").strip()
                            for item in raw_responsibles
                            if str(item or "").strip()
                        ],
                    }
                elif value:
                    normalized[release_key] = {
                        "reviewer": str(value).strip(),
                        "checker": "",
                        "responsibles": [],
                    }
            return normalized
        return {}
    except Exception as exc:
        logging.warning("Release monitor: failed to load reviewer assignments: %s", exc)
        return {}


def _save_reviewer_assignments(assignments):
    try:
        _ensure_snapshot_dir()
        REVIEWERS_FILE.write_text(
            json.dumps(assignments, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as exc:
        logging.warning("Release monitor: failed to save reviewer assignments: %s", exc)


def _apply_reviewer_assignments(items):
    assignments = _load_reviewer_assignments()
    for item in items:
        assignment_key = item.get("row_key") or item.get("release_key")
        release_assignment = assignments.get(assignment_key) or assignments.get(item.get("release_key"), {})
        item["psi_owner"] = release_assignment.get("reviewer", "")
        item["psi_checker"] = release_assignment.get("checker", "")
        item["psi_responsibles"] = list(release_assignment.get("responsibles", []))
    return items


def _normalize_text(value):
    return (value or "").strip().lower().replace("ё", "е")


def _parse_jira_date(value):
    if not value:
        return None

    for fmt in (
        "%Y-%m-%dT%H:%M:%S.%f%z",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
    ):
        try:
            parsed = datetime.strptime(value, fmt)
            if parsed.tzinfo is not None:
                return parsed.astimezone().replace(tzinfo=None)
            return parsed
        except ValueError:
            continue
    return None


def _format_timestamp(dt=None):
    return (dt or datetime.now()).strftime("%d.%m.%Y %H:%M:%S")


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


def _first_list_item(raw_value):
    if isinstance(raw_value, list) and raw_value:
        return raw_value[0]
    if isinstance(raw_value, dict):
        return raw_value
    return None


def _format_ke_id(raw_ke_id):
    if not raw_ke_id:
        return ""

    digits = re.sub(r"\D", "", str(raw_ke_id))
    if not digits:
        return ""
    return f"CI{digits.zfill(8)}"


def _extract_version(dist_item):
    if not dist_item:
        return ""

    if isinstance(dist_item, dict):
        for key in ("version", "buildVersion"):
            value = dist_item.get(key)
            if value:
                return str(value)

        for key in ("value", "url"):
            value = dist_item.get(key)
            if not value:
                continue
            match = re.search(r"[DP]-\d+\.\d+\.\d+-\d+", str(value))
            if match:
                return match.group(0)
    else:
        match = re.search(r"[DP]-\d+\.\d+\.\d+-\d+", str(dist_item))
        if match:
            return match.group(0)

    return ""


def _extract_ke_object(fields, resolved_fields):
    raw_ke_object = fields.get(resolved_fields["ke_object"])
    item = _first_list_item(raw_ke_object)
    if isinstance(item, dict):
        return {
            "id": item.get("id") or item.get("smId") or "",
            "name": item.get("value") or item.get("name") or "",
        }

    raw_delta_object = fields.get(resolved_fields["delta_release_distributive"])
    item = _first_list_item(raw_delta_object)
    if isinstance(item, dict):
        parent_ci = item.get("PARENT_CI") or item.get("id") or ""
        return {
            "id": parent_ci[2:].lstrip("0") if str(parent_ci).startswith("CI") else str(parent_ci),
            "name": item.get("value") or item.get("name") or "",
        }

    return {"id": "", "name": ""}


def _extract_release_dist(fields, resolved_fields):
    for logical_name in ("release_distributive", "delta_release_distributive"):
        raw_dist = fields.get(resolved_fields[logical_name])
        item = _first_list_item(raw_dist)
        if item:
            return item
    return None


def _get_domain_groups():
    groups = {}
    for prefix in RELEASE_PREFIXES:
        domain, token = get_jira_domain_and_token(f"{prefix}-1")
        groups.setdefault((domain, token), []).append(prefix)
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

    for logical_name, fallback_id in FIELD_FALLBACKS.items():
        resolved[logical_name] = fallback_id
        aliases = FIELD_ALIASES.get(logical_name, ())
        if not aliases:
            continue

        for field_id, field_name in field_name_map.items():
            normalized_name = _normalize_text(field_name)
            if any(_normalize_text(alias) in normalized_name for alias in aliases):
                resolved[logical_name] = field_id
                break

    return resolved


def _execute_search(domain, token, jql, fields_to_load):
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    url = f"{domain}/rest/api/2/search"
    start_at = 0
    issues = []

    while True:
        params = {
            "jql": jql,
            "startAt": start_at,
            "maxResults": 100,
            "fields": ",".join(sorted(field for field in fields_to_load if field)),
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


def _execute_release_search(domain, token, prefix, year_from, fields_to_load):
    current_year = datetime.now().year
    jql = (
        f'project = {prefix} AND '
        f'issuetype = "{RELEASE_ISSUE_TYPE}" AND '
        f'created >= "{year_from}-01-01" AND '
        f'created < "{current_year + 1}-01-01" '
        f'ORDER BY created ASC, key ASC'
    )
    return _execute_search(domain, token, jql, fields_to_load)


def _execute_quick_release_search(domain, token, prefix, updated_since, fields_to_load):
    current_year = datetime.now().year
    updated_since_str = updated_since.strftime("%Y-%m-%d")
    jql = (
        f'project = {prefix} AND '
        f'issuetype = "{RELEASE_ISSUE_TYPE}" AND '
        f'updated >= "{updated_since_str}" AND '
        f'created < "{current_year + 1}-01-01" '
        f'ORDER BY updated DESC, key ASC'
    )
    return _execute_search(domain, token, jql, fields_to_load)


def _execute_issue_keys_search(domain, token, issue_keys, fields_to_load):
    issues = []
    for offset in range(0, len(issue_keys), 50):
        batch_keys = issue_keys[offset: offset + 50]
        quoted = ", ".join(f'"{key}"' for key in batch_keys)
        jql = f"key in ({quoted})"
        issues.extend(_execute_search(domain, token, jql, fields_to_load))
    return issues


def _extract_release_io_keys(issue):
    keys = []
    for link in issue.get("fields", {}).get("issuelinks", []):
        link_type = link.get("type", {})
        inward_issue = link.get("inwardIssue")
        if not inward_issue:
            continue

        if link_type.get("name") == "ReleaseIO" or link_type.get("inward") == "Introduction Order":
            key = inward_issue.get("key")
            if key:
                keys.append(key)

    def _sort_key(issue_key):
        match = re.search(r"-(\d+)$", issue_key or "")
        return int(match.group(1)) if match else -1

    return sorted(list(dict.fromkeys(keys)), key=_sort_key)


def _extract_release_io_key(issue):
    keys = _extract_release_io_keys(issue)
    return keys[-1] if keys else ""


def _clean_release_summary(summary):
    cleaned = re.sub(r"^\s*Релиз#\d+\s*", "", summary or "", flags=re.IGNORECASE)
    return cleaned.strip()


def _detect_system(prefix, summary, ke_name, system_info_text):
    searchable = _normalize_text(f"{summary} {ke_name} {system_info_text}")

    if "аист" in searchable or "aist" in searchable:
        return "АИСТ"
    if "clm" in searchable or prefix in {"SMECLM", "SMECSC"}:
        return "CLM"
    return "Фокус"


def _build_release_name_lines(summary, ke_name, ke_id, version, row_label="(Релиз)"):
    lines = []
    short_name = _clean_release_summary(summary)
    if short_name:
        lines.append(short_name)

    if ke_name:
        if ke_id:
            lines.append(f"{ke_name}({ke_id})")
        else:
            lines.append(ke_name)

    lines.append("(Релиз)")

    if version:
        lines.append(f"Сборка: {version}")

    if len(lines) >= 1:
        lines[-2 if version else -1] = row_label

    return lines


def _build_rov_record(issue, domain, resolved_fields):
    fields = issue.get("fields", {})
    rov_start = _parse_jira_date(_extract_field_value(fields.get(resolved_fields["rov_start"])))
    rov_end = _parse_jira_date(_extract_field_value(fields.get(resolved_fields["rov_end"])))

    return {
        "key": issue.get("key"),
        "summary": fields.get("summary", ""),
        "status": (fields.get("status") or {}).get("name", ""),
        "issue_type": (fields.get("issuetype") or {}).get("name", ""),
        "start_dt": rov_start,
        "end_dt": rov_end,
        "start": rov_start.strftime("%d.%m.%Y") if rov_start else "",
        "end": rov_end.strftime("%d.%m.%Y") if rov_end else "",
        "start_iso": rov_start.isoformat() if rov_start else "",
        "end_iso": rov_end.isoformat() if rov_end else "",
        "url": f"{domain}/browse/{issue.get('key')}",
    }


def _pick_release_year(rov_start, rov_end, planned_prom_start, planned_prom_end, created_dt):
    for dt in (rov_start, rov_end, planned_prom_start, planned_prom_end, created_dt):
        if dt:
            return dt.year
    return datetime.now().year


def _pick_release_sort_dt(rov_start, rov_end, planned_prom_start, planned_prom_end):
    return rov_start or rov_end or planned_prom_start or planned_prom_end


def _build_release_record(issue, domain, prefix, resolved_fields, rov_map, current_year, previous_year):
    fields = issue.get("fields", {})
    status_name = (fields.get("status") or {}).get("name", "")
    summary = fields.get("summary", "")
    created_dt = _parse_jira_date(fields.get("created"))
    planned_prom_start = _parse_jira_date(_extract_field_value(fields.get(resolved_fields["planned_prom_start"])))
    planned_prom_end = _parse_jira_date(_extract_field_value(fields.get(resolved_fields["planned_prom_end"])))
    system_info_text = _extract_field_value(fields.get(resolved_fields["system_info"])) or ""

    ke_object = _extract_ke_object(fields, resolved_fields)
    dist_item = _extract_release_dist(fields, resolved_fields)
    release_version = _extract_version(dist_item)
    ke_distributive = _format_ke_id((dist_item or {}).get("id") if isinstance(dist_item, dict) else "")

    normalized_status = _normalize_text(status_name)
    is_cancelled = normalized_status == _normalize_text(CANCELLED_RELEASE_STATUS)
    is_final = normalized_status == _normalize_text(FINAL_RELEASE_STATUS)
    is_non_final = not is_final and not is_cancelled
    is_pre_final = normalized_status in {_normalize_text(status) for status in PRE_FINAL_RELEASE_STATUSES}
    ke_name = ke_object.get("name") or ""
    ke_id = ke_object.get("id") or ""
    linked_rov_keys = _extract_release_io_keys(issue)
    linked_rov_records = [rov_map.get(key, {}) for key in linked_rov_keys if rov_map.get(key)]
    row_variants = linked_rov_records or [{}]
    records = []
    today = datetime.now().date()

    for index, rov_data in enumerate(row_variants):
        rov_key = rov_data.get("key", "")
        rov_start = rov_data.get("start_dt")
        rov_end = rov_data.get("end_dt")

        release_year = _pick_release_year(rov_start, rov_end, planned_prom_start, planned_prom_end, created_dt)
        if release_year not in {current_year, previous_year}:
            continue

        rov_end_date = rov_end.date() if rov_end else None
        is_overdue = bool(rov_end_date and rov_end_date < today and is_non_final)
        is_today = bool(rov_end_date and rov_end_date == today and is_non_final)

        if is_final:
            row_state = "final"
        elif is_cancelled:
            row_state = "cancelled"
        elif is_overdue:
            row_state = "overdue"
        elif is_today:
            row_state = "today"
        else:
            row_state = "planned"

        days_overdue = (today - rov_end_date).days if is_overdue else 0
        is_reroll = bool(rov_key and len(linked_rov_records) > 1 and index > 0)
        row_label = "(Перераскатка)" if is_reroll else "(Релиз)"
        row_key = f"{issue.get('key')}::{rov_key or 'no-rov'}"

        records.append({
            "row_key": row_key,
            "year": release_year,
            "release_number": "",
            "release_key": issue.get("key"),
            "release_url": f"{domain}/browse/{issue.get('key')}",
            "release_status": status_name,
            "release_status_normalized": normalized_status,
            "release_summary": summary,
            "release_name_lines": _build_release_name_lines(summary, ke_name, ke_id, release_version, row_label=row_label),
            "is_reroll": is_reroll,
            "row_label": row_label,
            "zni_key": "",
            "ke": ke_distributive,
            "ke_name": ke_name,
            "ke_id": ke_id,
            "release_version": release_version,
            "rov_key": rov_key,
            "rov_url": rov_data.get("url", ""),
            "rov_status": rov_data.get("status", ""),
            "has_rov": bool(rov_key),
            "deployment_start": rov_data.get("start", ""),
            "deployment_start_iso": rov_data.get("start_iso", ""),
            "deployment_end": rov_data.get("end", ""),
            "deployment_end_iso": rov_data.get("end_iso", ""),
            "psi_owner": "",
            "psi_responsibles": [],
            "psi_checker": "",
            "row_state": row_state,
            "is_final": is_final,
            "is_cancelled": is_cancelled,
            "is_non_final": is_non_final,
            "is_pre_final": is_pre_final,
            "is_overdue": is_overdue,
            "is_today": is_today,
            "days_overdue": days_overdue,
            "waits_for_rov": not rov_key and not is_cancelled,
            "source_prefix": prefix,
            "system_name": _detect_system(prefix, summary, ke_name, system_info_text),
            "sort_date": _pick_release_sort_dt(rov_start, rov_end, planned_prom_start, planned_prom_end).isoformat() if _pick_release_sort_dt(rov_start, rov_end, planned_prom_start, planned_prom_end) else "",
            "created_sort_date": created_dt.isoformat() if created_dt else "",
            "created": fields.get("created", ""),
        })

    return records


def _sort_datetime_value(item, field_name="sort_date"):
    sort_value = item.get(field_name)
    if isinstance(sort_value, datetime):
        return sort_value
    if isinstance(sort_value, str) and sort_value:
        parsed = _parse_jira_date(sort_value)
        if parsed:
            return parsed
    return datetime.min


def _sort_and_number_records(records):
    records_by_year = defaultdict(list)
    for item in records:
        records_by_year[item["year"]].append(item)

    for year_items in records_by_year.values():
        numbered_items = [item for item in year_items if not item.get("waits_for_rov")]
        waiting_items = [item for item in year_items if item.get("waits_for_rov")]

        numbered_items.sort(
            key=lambda item: (
                _sort_datetime_value(item, "sort_date"),
                _sort_datetime_value(item, "created_sort_date"),
                item.get("release_key", ""),
            )
        )

        waiting_items.sort(
            key=lambda item: (
                _sort_datetime_value(item, "sort_date"),
                _sort_datetime_value(item, "created_sort_date"),
                item.get("release_key", ""),
            ),
            reverse=True,
        )

        for index, item in enumerate(numbered_items, start=1):
            item["release_number"] = index

        for item in waiting_items:
            item["release_number"] = ""

    records.sort(
        key=lambda item: (
            -item.get("year", 0),
            0 if item.get("waits_for_rov") else 1,
            -_sort_datetime_value(item, "sort_date").timestamp() if _sort_datetime_value(item, "sort_date") != datetime.min else float("inf"),
            -(item.get("release_number") or 0),
            item.get("release_key", ""),
        )
    )
    return records


def _build_summary(records, current_year, previous_year):
    summary = {
        "total": len(records),
        "non_final": 0,
        "overdue": 0,
        "today": 0,
        "pre_final": 0,
        "final": 0,
        "cancelled": 0,
        "by_status": defaultdict(int),
        "by_year": {
            str(current_year): {
                "total": 0,
                "non_final": 0,
                "overdue": 0,
                "today": 0,
                "pre_final": 0,
                "final": 0,
                "cancelled": 0,
            },
            str(previous_year): {
                "total": 0,
                "non_final": 0,
                "overdue": 0,
                "today": 0,
                "pre_final": 0,
                "final": 0,
                "cancelled": 0,
            },
        },
    }

    for item in records:
        year_bucket = summary["by_year"].setdefault(
            str(item["year"]),
            {
                "total": 0,
                "non_final": 0,
                "overdue": 0,
                "today": 0,
                "pre_final": 0,
                "final": 0,
                "cancelled": 0,
            },
        )
        summary["by_status"][item["release_status"] or "Не указан"] += 1
        year_bucket["total"] += 1

        if item["is_non_final"]:
            summary["non_final"] += 1
            year_bucket["non_final"] += 1
        if item["is_overdue"]:
            summary["overdue"] += 1
            year_bucket["overdue"] += 1
        if item["is_today"]:
            summary["today"] += 1
            year_bucket["today"] += 1
        if item["is_pre_final"]:
            summary["pre_final"] += 1
            year_bucket["pre_final"] += 1
        if item["is_final"]:
            summary["final"] += 1
            year_bucket["final"] += 1
        if item["is_cancelled"]:
            summary["cancelled"] += 1
            year_bucket["cancelled"] += 1

    summary["by_status"] = dict(
        sorted(summary["by_status"].items(), key=lambda pair: (-pair[1], pair[0]))
    )
    return summary


def _compose_release_payload(all_records, mode):
    current_year = datetime.now().year
    previous_year = current_year - 1
    _apply_reviewer_assignments(all_records)
    _sort_and_number_records(all_records)
    payload = {
        "items": all_records,
        "summary": _build_summary(all_records, current_year, previous_year),
        "meta": {
            "final_status": FINAL_RELEASE_STATUS,
            "cancelled_status": CANCELLED_RELEASE_STATUS,
            "final_statuses": list(FINAL_RELEASE_STATUSES),
            "pre_final_statuses": list(PRE_FINAL_RELEASE_STATUSES),
            "prefixes": list(RELEASE_PREFIXES),
            "years": [current_year, previous_year],
            "current_year": current_year,
            "last_updated": _format_timestamp(),
            "last_sync_mode": mode,
            "quick_refresh_days": QUICK_REFRESH_DAYS,
            "auto_full_refresh_hour": AUTO_FULL_REFRESH_HOUR,
            "is_cached": True,
        },
    }
    return payload


def _fetch_release_monitor_data():
    current_year = datetime.now().year
    previous_year = current_year - 1
    all_records = []

    for (domain, token), prefixes in _get_domain_groups().items():
        resolved_fields = _resolve_field_ids(domain, token)
        release_fields_to_load = {
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
            "issuelinks",
            resolved_fields["planned_prom_start"],
            resolved_fields["planned_prom_end"],
            resolved_fields["system_info"],
            resolved_fields["ke_object"],
            resolved_fields["release_distributive"],
            resolved_fields["delta_release_distributive"],
        }

        domain_release_issues = []
        for prefix in prefixes:
            try:
                issues = _execute_release_search(
                    domain,
                    token,
                    prefix,
                    previous_year,
                    release_fields_to_load,
                )
                logging.info(
                    "Release monitor: loaded %s releases for prefix %s",
                    len(issues),
                    prefix,
                )
                domain_release_issues.extend((prefix, issue) for issue in issues)
            except Exception as exc:
                logging.error(
                    "Release monitor: failed to load releases for prefix %s: %s",
                    prefix,
                    exc,
                )

        rov_keys = sorted(
            {
                rov_key
                for _, issue in domain_release_issues
                for rov_key in _extract_release_io_keys(issue)
                if rov_key
            }
        )
        rov_map = {}
        if rov_keys:
            rov_fields_to_load = {
                "key",
                "summary",
                "status",
                "issuetype",
                resolved_fields["rov_start"],
                resolved_fields["rov_end"],
            }
            try:
                rov_issues = _execute_issue_keys_search(domain, token, rov_keys, rov_fields_to_load)
                rov_map = {
                    issue.get("key"): _build_rov_record(issue, domain, resolved_fields)
                    for issue in rov_issues
                }
            except Exception as exc:
                logging.error("Release monitor: failed to load linked ROV issues from %s: %s", domain, exc)

        for prefix, issue in domain_release_issues:
            records = _build_release_record(
                issue,
                domain,
                prefix,
                resolved_fields,
                rov_map,
                current_year,
                previous_year,
            )
            if records:
                all_records.extend(records)

    return _compose_release_payload(all_records, "full")


def _merge_release_records(existing_items, updated_items):
    current_year = datetime.now().year
    previous_year = current_year - 1
    updated_release_keys = {
        item.get("release_key")
        for item in (updated_items or [])
        if item.get("release_key")
    }
    records_by_key = {
        item.get("row_key") or item.get("release_key"): dict(item)
        for item in (existing_items or [])
        if (item.get("row_key") or item.get("release_key"))
        and item.get("year") in {current_year, previous_year}
        and item.get("release_key") not in updated_release_keys
    }

    for item in updated_items:
        item_key = item.get("row_key") or item.get("release_key")
        if item and item_key:
            records_by_key[item_key] = item

    merged_items = list(records_by_key.values())
    return _compose_release_payload(merged_items, "quick")


def _fetch_quick_release_monitor_data(base_items=None):
    current_year = datetime.now().year
    previous_year = current_year - 1
    updated_since = datetime.now() - timedelta(days=QUICK_REFRESH_DAYS)
    updated_records = []

    for (domain, token), prefixes in _get_domain_groups().items():
        resolved_fields = _resolve_field_ids(domain, token)
        release_fields_to_load = {
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
            "issuelinks",
            resolved_fields["planned_prom_start"],
            resolved_fields["planned_prom_end"],
            resolved_fields["system_info"],
            resolved_fields["ke_object"],
            resolved_fields["release_distributive"],
            resolved_fields["delta_release_distributive"],
        }

        domain_release_issues = []
        for prefix in prefixes:
            try:
                issues = _execute_quick_release_search(
                    domain,
                    token,
                    prefix,
                    updated_since,
                    release_fields_to_load,
                )
                logging.info(
                    "Release monitor: quick refresh loaded %s releases for prefix %s",
                    len(issues),
                    prefix,
                )
                domain_release_issues.extend((prefix, issue) for issue in issues)
            except Exception as exc:
                logging.error(
                    "Release monitor: quick refresh failed for prefix %s: %s",
                    prefix,
                    exc,
                )

        rov_keys = sorted(
            {
                rov_key
                for _, issue in domain_release_issues
                for rov_key in _extract_release_io_keys(issue)
                if rov_key
            }
        )
        rov_map = {}
        if rov_keys:
            rov_fields_to_load = {
                "key",
                "summary",
                "status",
                "issuetype",
                resolved_fields["rov_start"],
                resolved_fields["rov_end"],
            }
            try:
                rov_issues = _execute_issue_keys_search(domain, token, rov_keys, rov_fields_to_load)
                rov_map = {
                    issue.get("key"): _build_rov_record(issue, domain, resolved_fields)
                    for issue in rov_issues
                }
            except Exception as exc:
                logging.error("Release monitor: quick refresh failed to load linked ROV issues from %s: %s", domain, exc)

        for prefix, issue in domain_release_issues:
            records = _build_release_record(
                issue,
                domain,
                prefix,
                resolved_fields,
                rov_map,
                current_year,
                previous_year,
            )
            if records:
                updated_records.extend(records)

    return _merge_release_records(base_items or [], updated_records)


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


def _run_release_monitor_refresh():
    global _cached_data, _last_cache_update

    try:
        logging.info("Release monitor: background refresh started")
        data = _fetch_release_monitor_data()
        with _cache_lock:
            _cached_data = data
            _last_cache_update = time.time()
            _refresh_status.update(
                {
                    "state": "completed",
                    "message": "Данные по релизам обновлены",
                    "started_at": _refresh_status.get("started_at"),
                    "finished_at": datetime.now().strftime("%d.%m.%Y %H:%M:%S"),
                    "error": None,
                }
            )
        logging.info("Release monitor: background refresh completed, items=%s", len(data.get("items", [])))
    except Exception as exc:
        logging.exception("Release monitor: background refresh failed")
        with _cache_lock:
            _refresh_status.update(
                {
                    "state": "failed",
                    "message": "Ошибка обновления релизов",
                    "finished_at": datetime.now().strftime("%d.%m.%Y %H:%M:%S"),
                    "error": str(exc),
                }
            )


def start_release_monitor_refresh():
    global _refresh_thread

    with _cache_lock:
        if _refresh_thread and _refresh_thread.is_alive():
            return {
                "started": False,
                "status": dict(_refresh_status),
            }

        _refresh_status.update(
            {
                "state": "refreshing",
                "message": "Идет обновление релизов из Jira",
                "started_at": datetime.now().strftime("%d.%m.%Y %H:%M:%S"),
                "finished_at": None,
                "error": None,
            }
        )
        _refresh_thread = threading.Thread(target=_run_release_monitor_refresh, daemon=True)
        _refresh_thread.start()

        return {
            "started": True,
            "status": dict(_refresh_status),
        }


def get_release_monitor_refresh_status():
    with _cache_lock:
        payload = {
            "status": dict(_refresh_status),
        }
        if _cached_data is not None:
            payload["data"] = {
                "items": list(_cached_data.get("items", [])),
                "summary": dict(_cached_data.get("summary", {})),
                "meta": dict(_cached_data.get("meta", {})),
            }
        else:
            payload["data"] = _build_empty_release_monitor_payload()
        return payload


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


def _get_cached_payload_copy():
    if _cached_data is None:
        return None
    return {
        "items": list(_cached_data.get("items", [])),
        "summary": dict(_cached_data.get("summary", {})),
        "meta": dict(_cached_data.get("meta", {})),
    }


def _ensure_scheduler_started():
    global _scheduler_thread, _scheduler_started

    if _scheduler_started:
        return

    def _scheduler_loop():
        while True:
            try:
                with _cache_lock:
                    snapshot = _cached_data or _load_snapshot_from_disk() or _build_empty_release_monitor_payload()
                    last_full_sync = (snapshot.get("meta") or {}).get("last_full_sync")
                    running = _refresh_thread is not None and _refresh_thread.is_alive()

                now = datetime.now()
                last_full_date = None
                if last_full_sync:
                    try:
                        last_full_date = datetime.strptime(last_full_sync, "%d.%m.%Y %H:%M:%S").date()
                    except ValueError:
                        last_full_date = None

                should_run = (
                    now.hour >= AUTO_FULL_REFRESH_HOUR
                    and last_full_date != now.date()
                    and not running
                )

                if should_run:
                    start_release_monitor_refresh(mode="full", trigger="auto")
            except Exception:
                logging.exception("Release monitor: auto full refresh scheduler failed")

            time.sleep(AUTO_REFRESH_CHECK_INTERVAL)

    _scheduler_thread = threading.Thread(target=_scheduler_loop, daemon=True)
    _scheduler_thread.start()
    _scheduler_started = True


def get_release_monitor_data(force_refresh=False):
    global _cached_data, _last_cache_update

    with _cache_lock:
        _ensure_scheduler_started()
        if _cached_data is None:
            disk_payload = _load_snapshot_from_disk()
            if disk_payload:
                _cached_data = disk_payload
                _last_cache_update = time.time()

        now = time.time()
        if (
            not force_refresh
            and _cached_data is not None
            and _last_cache_update is not None
            and (now - _last_cache_update) < DASHBOARD_CACHE_TTL
        ):
            return _cached_data

    data = _fetch_release_monitor_data()
    data["meta"]["last_full_sync"] = data["meta"].get("last_updated")
    data["meta"]["last_quick_sync"] = (_cached_data or {}).get("meta", {}).get("last_quick_sync")

    with _cache_lock:
        _cached_data = data
        _last_cache_update = time.time()
        _save_snapshot_to_disk(_cached_data)
        return _cached_data


def _run_release_monitor_refresh(mode="full", trigger="manual"):
    global _cached_data, _last_cache_update

    try:
        logging.info("Release monitor: background %s refresh started", mode)
        with _cache_lock:
            base_items = list((_cached_data or {}).get("items", []))
            current_meta = dict((_cached_data or {}).get("meta", {}))
            if not base_items:
                disk_payload = _load_snapshot_from_disk()
                if disk_payload:
                    base_items = list(disk_payload.get("items", []))
                    current_meta = dict(disk_payload.get("meta", {}))

        if mode == "quick" and base_items:
            data = _fetch_quick_release_monitor_data(base_items)
        else:
            data = _fetch_release_monitor_data()
            if mode == "quick":
                mode = "full"

        now_str = _format_timestamp()
        meta = dict(data.get("meta", {}))
        meta["last_updated"] = now_str
        meta["last_sync_mode"] = mode
        meta["quick_refresh_days"] = QUICK_REFRESH_DAYS
        meta["auto_full_refresh_hour"] = AUTO_FULL_REFRESH_HOUR
        meta["last_full_sync"] = now_str if mode == "full" else current_meta.get("last_full_sync")
        meta["last_quick_sync"] = now_str if mode == "quick" else current_meta.get("last_quick_sync")
        data["meta"] = meta

        with _cache_lock:
            _cached_data = data
            _last_cache_update = time.time()
            _save_snapshot_to_disk(_cached_data)
            _refresh_status.update(
                {
                    "state": "completed",
                    "message": "Данные по релизам обновлены",
                    "started_at": _refresh_status.get("started_at"),
                    "finished_at": now_str,
                    "error": None,
                    "mode": mode,
                    "trigger": trigger,
                }
            )
        logging.info("Release monitor: background %s refresh completed, items=%s", mode, len(data.get("items", [])))
    except Exception as exc:
        logging.exception("Release monitor: background %s refresh failed", mode)
        with _cache_lock:
            _refresh_status.update(
                {
                    "state": "failed",
                    "message": "Ошибка обновления релизов",
                    "finished_at": _format_timestamp(),
                    "error": str(exc),
                    "mode": mode,
                    "trigger": trigger,
                }
            )


def start_release_monitor_refresh(mode="full", trigger="manual"):
    global _refresh_thread

    with _cache_lock:
        _ensure_scheduler_started()
        if _refresh_thread and _refresh_thread.is_alive():
            return {
                "started": False,
                "status": dict(_refresh_status),
            }

        _refresh_status.update(
            {
                "state": "refreshing",
                "message": "Идет полное обновление релизов из Jira" if mode == "full" else f"Идет быстрое обновление релизов за последние {QUICK_REFRESH_DAYS} дней",
                "started_at": _format_timestamp(),
                "finished_at": None,
                "error": None,
                "mode": mode,
                "trigger": trigger,
            }
        )
        _refresh_thread = threading.Thread(
            target=_run_release_monitor_refresh,
            kwargs={"mode": mode, "trigger": trigger},
            daemon=True,
        )
        _refresh_thread.start()

        return {
            "started": True,
            "status": dict(_refresh_status),
        }


def get_release_monitor_refresh_status():
    global _cached_data

    with _cache_lock:
        _ensure_scheduler_started()
        if _cached_data is None:
            disk_payload = _load_snapshot_from_disk()
            if disk_payload:
                _cached_data = disk_payload

        payload = {
            "status": dict(_refresh_status),
        }
        payload["data"] = _get_cached_payload_copy() or _build_empty_release_monitor_payload()
        return payload


def get_release_monitor_snapshot():
    global _cached_data, _last_cache_update

    with _cache_lock:
        _ensure_scheduler_started()
        if _cached_data is None:
            disk_payload = _load_snapshot_from_disk()
            if disk_payload:
                _cached_data = disk_payload
                _last_cache_update = time.time()
            else:
                return _build_empty_release_monitor_payload()

        payload = _get_cached_payload_copy()
        _apply_reviewer_assignments(payload.get("items", []))
        payload["meta"] = {
            **payload.get("meta", {}),
            "is_cached": True,
        }
        return payload


def get_release_monitor_reviewer_options():
    return list(OPLOT_VALUES)


def set_release_monitor_reviewer(release_key, reviewer):
    global _cached_data

    release_key = (release_key or "").strip()
    reviewer = (reviewer or "").strip()
    if not release_key:
        raise ValueError("Не указан ключ релиза")

    if reviewer and reviewer not in OPLOT_VALUES:
        raise ValueError("Выбранный проверяющий отсутствует в списке ОПЛОТ")

    assignments = _load_reviewer_assignments()
    current_assignment = dict(assignments.get(release_key, {}))
    current_assignment["reviewer"] = reviewer

    if current_assignment.get("reviewer") or current_assignment.get("checker") or current_assignment.get("responsibles"):
        assignments[release_key] = current_assignment
    else:
        assignments.pop(release_key, None)
    _save_reviewer_assignments(assignments)

    with _cache_lock:
        if _cached_data is not None:
            for item in _cached_data.get("items", []):
                item_key = item.get("row_key") or item.get("release_key")
                if item_key == release_key:
                    item["psi_owner"] = reviewer
                    item["psi_checker"] = current_assignment.get("checker", "")
                    item["psi_responsibles"] = list(current_assignment.get("responsibles", []))
                    break
            _save_snapshot_to_disk(_cached_data)

    return reviewer


def set_release_monitor_assignment(release_key, reviewer, checker, responsibles=None):
    global _cached_data

    release_key = (release_key or "").strip()
    reviewer = (reviewer or "").strip()
    checker = (checker or "").strip()
    if not release_key:
        raise ValueError("Не указан ключ релиза")

    if reviewer and reviewer not in OPLOT_VALUES:
        raise ValueError("Выбранный дежурный отсутствует в списке ОПЛОТ")

    normalized_responsibles = []
    for responsible in (responsibles or []):
        responsible_name = str(responsible or "").strip()
        if not responsible_name:
            continue
        if responsible_name not in OPLOT_VALUES:
            raise ValueError("Выбранный ответственный отсутствует в списке ОПЛОТ")
        if responsible_name not in normalized_responsibles:
            normalized_responsibles.append(responsible_name)

    assignments = _load_reviewer_assignments()
    if reviewer or checker or normalized_responsibles:
        assignments[release_key] = {
            "reviewer": reviewer,
            "checker": checker,
            "responsibles": normalized_responsibles,
        }
    else:
        assignments.pop(release_key, None)
    _save_reviewer_assignments(assignments)

    with _cache_lock:
        if _cached_data is not None:
            for item in _cached_data.get("items", []):
                item_key = item.get("row_key") or item.get("release_key")
                if item_key == release_key:
                    item["psi_owner"] = reviewer
                    item["psi_checker"] = checker
                    item["psi_responsibles"] = list(normalized_responsibles)
                    break
            _save_snapshot_to_disk(_cached_data)

    return {
        "reviewer": reviewer,
        "checker": checker,
        "responsibles": normalized_responsibles,
    }
