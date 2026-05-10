import logging
import json
import html
import os
import re
import threading
import time
from collections import defaultdict
from datetime import datetime, timedelta
from html.parser import HTMLParser
from io import BytesIO
from pathlib import Path

import requests
from openpyxl import load_workbook

from config import DASHBOARD_CACHE_TTL, OPLOT_VALUES, TOKENS
from services.jira_service import get_jira_domain_and_token
from services.jira_oplot_issue_service import create_oplot_release_issue


FINAL_RELEASE_STATUS = "\u0423\u0441\u0442\u0430\u043d\u043e\u0432\u043b\u0435\u043d \u043d\u0430 \u041f\u0420\u041e\u041c"
CANCELLED_RELEASE_STATUS = "\u041e\u0442\u043c\u0435\u043d\u0435\u043d\u043e"
READY_FOR_PROM_STATUS = "\u0413\u043e\u0442\u043e\u0432 \u043a \u0443\u0441\u0442\u0430\u043d\u043e\u0432\u043a\u0435 \u043d\u0430 \u041f\u0420\u041e\u041c"
FINAL_RELEASE_STATUSES = (
    FINAL_RELEASE_STATUS,
    CANCELLED_RELEASE_STATUS,
)
PRE_FINAL_RELEASE_STATUSES = (
    "\u0423\u0441\u0442\u0430\u043d\u043e\u0432\u043a\u0430 \u043d\u0430 \u041f\u0420\u041e\u041c",
    READY_FOR_PROM_STATUS,
)
RELEASE_PREFIXES = ("EMRM", "SMECLM", "SMECSC", "HELPERAI", "AIGAS")
RELEASE_ISSUE_TYPE = "Release 2.0"
ROV_ISSUE_TYPE = "Introduction Order"
QUICK_REFRESH_DAYS = 9
AUTO_FULL_REFRESH_HOUR = 6
AUTO_REFRESH_CHECK_INTERVAL = 300
SNAPSHOT_DIR = Path(__file__).resolve().parent.parent / "cache"
SNAPSHOT_FILE = SNAPSHOT_DIR / "release_monitor_snapshot.json"
MANUAL_OVERRIDES_FILE = SNAPSHOT_DIR / "release_monitor_manual_overrides.json"
REVIEWERS_FILE = SNAPSHOT_DIR / "release_monitor_reviewers.json"
ORDER_FILE = SNAPSHOT_DIR / "release_monitor_order.json"
DUTY_SCHEDULE_FILE = SNAPSHOT_DIR / "release_monitor_duty_schedule.json"
DATE_OVERRIDES_FILE = SNAPSHOT_DIR / "release_monitor_date_overrides.json"
ZNI_FILE = SNAPSHOT_DIR / "release_monitor_zni.json"
ATTEMPTS_FILE = SNAPSHOT_DIR / "release_monitor_attempts.json"
REVISION_FILE = SNAPSHOT_DIR / "release_monitor_revision.txt"
CONFLUENCE_DELTA_BASE = "https://confluence.delta.sbrf.ru"
JIRA_DELTA_BASE = "https://jira.delta.sbrf.ru"
RELEASE_VERSION_PATTERN = re.compile(r"[DP]-\d+(?:\.\d+){2}(?:[.-][A-Za-z0-9_]+)+")
ARTIFACT_URL_PATTERN = re.compile(r"(?:https?://)?[A-Za-z0-9.-]+\.[A-Za-z]{2,}/[^\s\"'<>)]+")
DUTY_HARD_EXCLUDED_STATUSES = {"ДД", "ВД", "ВР", "ХД", "ХР"}
DUTY_RESERVE_STATUSES = {"ДР"}
DUTY_ABSENCE_KEYWORDS = ("отпуск", "отгул", "больн")

MONTH_NAME_MAP = {
    "\u044f\u043d\u0432\u0430\u0440": 1,
    "\u0444\u0435\u0432\u0440\u0430\u043b": 2,
    "\u043c\u0430\u0440\u0442": 3,
    "\u0430\u043f\u0440\u0435\u043b": 4,
    "\u043c\u0430\u0439": 5,
    "\u0438\u044e\u043d": 6,
    "\u0438\u044e\u043b": 7,
    "\u0430\u0432\u0433\u0443\u0441\u0442": 8,
    "\u0441\u0435\u043d\u0442\u044f\u0431": 9,
    "\u043e\u043a\u0442\u044f\u0431": 10,
    "\u043d\u043e\u044f\u0431": 11,
    "\u0434\u0435\u043a\u0430\u0431": 12,
}

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
        "\u043d\u0430\u0447\u0430\u043b\u043e \u0432\u043d\u0435\u0434\u0440\u0435\u043d\u0438\u044f",
        "\u0434\u0430\u0442\u0430 \u043d\u0430\u0447\u0430\u043b\u0430 \u0432\u043d\u0435\u0434\u0440\u0435\u043d\u0438\u044f",
        "\u043d\u0430\u0447\u0430\u043b\u043e \u0432\u043d\u0435\u0434\u0440\u0435\u043d\u0438\u044f \u043f\u043b\u0430\u043d",
    ),
    "planned_prom_end": (
        "\u0434\u0430\u0442\u0430 \u0437\u0430\u0432\u0435\u0440\u0448\u0435\u043d\u0438\u044f \u0443\u0441\u0442\u0430\u043d\u043e\u0432\u043a\u0438 \u0432 \u043f\u0440\u043e\u043c",
        "\u0434\u0430\u0442\u0430 \u0443\u0441\u0442\u0430\u043d\u043e\u0432\u043a\u0438 \u0432 \u043f\u0440\u043e\u043c",
    ),
    "system_info": (
        "\u0438\u0442-\u0443\u0441\u043b\u0443\u0433\u0430",
    ),
    "ke_object": (
        "\u043a\u044d",
    ),
    "release_distributive": (
        "\u043a\u044d \u0434\u0438\u0441\u0442\u0440\u0438\u0431\u0443\u0442\u0438\u0432\u0430",
        "\u043a\u044d \u0434\u0438\u0441\u0442\u0440\u0438\u0431\u0443\u0442\u0438\u0432\u043e\u0432",
    ),
    "rov_start": (
        "\u0434\u0430\u0442\u0430/\u0432\u0440\u0435\u043c\u044f \u043d\u0430\u0447\u0430\u043b\u0430 \u0440\u0430\u0431\u043e\u0442 \u043f\u043e \u0432\u043d\u0435\u0434\u0440\u0435\u043d\u0438\u044e",
        "\u0434\u0430\u0442\u0430 \u0432\u0440\u0435\u043c\u044f \u043d\u0430\u0447\u0430\u043b\u0430 \u0440\u0430\u0431\u043e\u0442 \u043f\u043e \u0432\u043d\u0435\u0434\u0440\u0435\u043d\u0438\u044e",
        "\u043d\u0430\u0447\u0430\u043b\u043e \u0440\u0430\u0431\u043e\u0442 \u043f\u043e \u0432\u043d\u0435\u0434\u0440\u0435\u043d\u0438\u044e",
    ),
    "rov_end": (
        "\u0434\u0430\u0442\u0430/\u0432\u0440\u0435\u043c\u044f \u043e\u043a\u043e\u043d\u0447\u0430\u043d\u0438\u044f \u0440\u0430\u0431\u043e\u0442 \u043f\u043e \u0432\u043d\u0435\u0434\u0440\u0435\u043d\u0438\u044e",
        "\u0434\u0430\u0442\u0430 \u0432\u0440\u0435\u043c\u044f \u043e\u043a\u043e\u043d\u0447\u0430\u043d\u0438\u044f \u0440\u0430\u0431\u043e\u0442 \u043f\u043e \u0432\u043d\u0435\u0434\u0440\u0435\u043d\u0438\u044e",
        "\u043e\u043a\u043e\u043d\u0447\u0430\u043d\u0438\u0435 \u0440\u0430\u0431\u043e\u0442 \u043f\u043e \u0432\u043d\u0435\u0434\u0440\u0435\u043d\u0438\u044e",
    ),
}

_cache_lock = threading.RLock()
_cached_data = None
_last_cache_update = None
_manual_order_cache = None
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
        "manual_overrides": {},
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
            "last_confluence_sync": None,
            "last_duty_schedule_upload": None,
            "last_sync_mode": None,
            "quick_refresh_days": QUICK_REFRESH_DAYS,
            "auto_full_refresh_hour": AUTO_FULL_REFRESH_HOUR,
            "data_revision": _read_data_revision(),
            "is_cached": False,
        },
    }


def _ensure_snapshot_dir():
    os.makedirs(SNAPSHOT_DIR, exist_ok=True)


def _count_payload_items(payload):
    if not isinstance(payload, dict):
        return 0
    items = payload.get("items", [])
    return len(items) if isinstance(items, list) else 0


def _new_data_revision():
    return str(int(time.time() * 1000))


def _read_data_revision():
    try:
        if REVISION_FILE.exists():
            revision = REVISION_FILE.read_text(encoding="utf-8").strip()
            if revision:
                return revision
    except Exception as exc:
        logging.warning("Release monitor: failed to read revision: %s", exc)
    return ""


def _touch_release_monitor_revision():
    revision = _new_data_revision()
    try:
        _ensure_snapshot_dir()
        REVISION_FILE.write_text(revision, encoding="utf-8")
    except Exception as exc:
        logging.warning("Release monitor: failed to save revision: %s", exc)
    return revision


def _append_revision_meta(meta):
    meta = dict(meta or {})
    revision = str(meta.get("data_revision") or "").strip() or _read_data_revision()
    if not revision:
        revision = str(_get_snapshot_mtime() or "")
    meta["data_revision"] = revision
    return meta


def is_release_monitor_refreshing():
    with _cache_lock:
        return bool(_refresh_thread and _refresh_thread.is_alive()) or _refresh_status.get("state") == "refreshing"


def ensure_release_monitor_not_refreshing():
    if is_release_monitor_refreshing():
        raise RuntimeError("Идет обновление релизов. Дождитесь завершения и обновите страницу.")


def _strip_manual_release_overrides_for_snapshot(payload):
    payload_to_save = dict(payload or {})
    payload_to_save.pop("manual_overrides", None)

    items_to_save = []
    for item in payload_to_save.get("items") or []:
        if not isinstance(item, dict):
            continue
        clean_item = dict(item)

        for target_field, base_field in (
            ("release_summary", "base_release_summary"),
            ("release_version", "base_release_version"),
            ("release_dist_url", "base_release_dist_url"),
            ("ke", "base_ke"),
            ("zni_key", "base_zni_key"),
            ("zni_url", "base_zni_url"),
        ):
            if base_field in clean_item:
                clean_item[target_field] = clean_item.get(base_field) or ""

        if isinstance(clean_item.get("base_release_name_lines"), list):
            clean_item["release_name_lines"] = list(clean_item.get("base_release_name_lines") or [])

        for manual_field in (
            "has_manual_release_override",
            "manual_release_summary",
            "manual_release_version",
            "manual_release_dist_url",
            "manual_ke",
            "manual_zni_key",
            "manual_zni_url",
            "manual_clear_zni",
        ):
            clean_item.pop(manual_field, None)

        items_to_save.append(clean_item)

    payload_to_save["items"] = items_to_save
    return payload_to_save


def _save_snapshot_to_disk(payload, bump_revision=True):
    try:
        _ensure_snapshot_dir()
        current_disk_payload = _load_snapshot_from_disk()
        incoming_items = _count_payload_items(payload)
        existing_items = _count_payload_items(current_disk_payload)

        if incoming_items == 0 and existing_items > 0:
            logging.warning(
                "Release monitor: skipped overwriting non-empty snapshot with empty payload (existing_items=%s)",
                existing_items,
            )
            return

        if isinstance(payload, dict):
            payload.setdefault("meta", {})
            if bump_revision:
                payload["meta"]["data_revision"] = _touch_release_monitor_revision()
            else:
                payload["meta"] = _append_revision_meta(payload.get("meta", {}))

        payload_to_save = _strip_manual_release_overrides_for_snapshot(payload)

        SNAPSHOT_FILE.write_text(
            json.dumps(payload_to_save, ensure_ascii=False, indent=2),
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


def _get_snapshot_mtime():
    try:
        if SNAPSHOT_FILE.exists():
            return SNAPSHOT_FILE.stat().st_mtime
    except Exception:
        logging.exception("Release monitor: failed to read snapshot mtime")
    return None


def _reload_snapshot_from_disk_if_newer():
    global _cached_data, _last_cache_update

    disk_mtime = _get_snapshot_mtime()
    if disk_mtime is None:
        return

    if _cached_data is not None and _last_cache_update is not None and disk_mtime <= _last_cache_update:
        return

    disk_payload = _load_snapshot_from_disk()
    if disk_payload is not None:
        _cached_data = _hydrate_release_monitor_payload(disk_payload)
        _last_cache_update = disk_mtime


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
                        "reviewer_source": str(value.get("reviewer_source") or "").strip(),
                        "reviewer_date": str(value.get("reviewer_date", "") or "").strip(),
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
                        "reviewer_source": "manual",
                        "reviewer_date": "",
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


def _normalize_zni_assignments(payload):
    normalized = {}
    if not isinstance(payload, dict):
        return normalized

    for key, value in payload.items():
        row_key = str(key or "").strip()
        if not row_key:
            continue
        if isinstance(value, dict):
            zni_key = str(value.get("key") or value.get("zni_key") or "").strip()
            if zni_key:
                normalized[row_key] = {
                    "key": zni_key,
                    "url": str(value.get("url") or "").strip(),
                    "summary": str(value.get("summary") or "").strip(),
                    "created_at": str(value.get("created_at") or "").strip(),
                    "assignee": str(value.get("assignee") or "").strip(),
                    "reporter": str(value.get("reporter") or "").strip(),
                }
        elif value:
            normalized[row_key] = {
                "key": str(value).strip(),
                "url": "",
                "summary": "",
                "created_at": "",
                "assignee": "",
                "reporter": "",
            }
    return normalized


def _normalize_rollout_note_flags(payload):
    normalized = {}
    if not isinstance(payload, dict):
        return normalized

    for key, value in payload.items():
        row_key = str(key or "").strip()
        if not row_key or not isinstance(value, dict):
            continue
        raw_level = str(value.get("rollout_notes_level") or value.get("level") or "").strip().lower()
        if not raw_level and bool(value.get("has_rollout_notes")):
            raw_level = "warning"
        if raw_level not in {"success", "warning", "danger"}:
            continue
        normalized[row_key] = {
            "has_rollout_notes": True,
            "rollout_notes_level": raw_level,
            "updated_at": str(value.get("updated_at") or "").strip(),
        }
    return normalized


def _load_zni_payload():
    if not ZNI_FILE.exists():
        return {"issues": {}, "flags": {}}

    try:
        payload = json.loads(ZNI_FILE.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return {"issues": {}, "flags": {}}

        if "issues" in payload or "flags" in payload:
            return {
                "issues": _normalize_zni_assignments(payload.get("issues") or {}),
                "flags": _normalize_rollout_note_flags(payload.get("flags") or {}),
            }

        return {
            "issues": _normalize_zni_assignments(payload),
            "flags": {},
        }
    except Exception as exc:
        logging.warning("Release monitor: failed to load ZNI payload: %s", exc)
        return {"issues": {}, "flags": {}}


def _save_zni_payload(payload):
    try:
        _ensure_snapshot_dir()
        ZNI_FILE.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as exc:
        logging.warning("Release monitor: failed to save ZNI payload: %s", exc)


def _load_manual_overrides_payload():
    if MANUAL_OVERRIDES_FILE.exists():
        try:
            payload = json.loads(MANUAL_OVERRIDES_FILE.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                return _normalize_manual_release_overrides(payload)
        except Exception as exc:
            logging.warning("Release monitor: failed to load manual overrides payload: %s", exc)

    legacy_payload = _load_snapshot_from_disk() or {}
    legacy_overrides = _normalize_manual_release_overrides(legacy_payload.get("manual_overrides") or {})
    if legacy_overrides:
        _save_manual_overrides_payload(legacy_overrides)
    return legacy_overrides


def _save_manual_overrides_payload(overrides):
    try:
        _ensure_snapshot_dir()
        MANUAL_OVERRIDES_FILE.write_text(
            json.dumps(_normalize_manual_release_overrides(overrides), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as exc:
        logging.warning("Release monitor: failed to save manual overrides payload: %s", exc)


def _load_zni_assignments():
    return _load_zni_payload().get("issues", {})


def _save_zni_assignments(assignments):
    payload = _load_zni_payload()
    payload["issues"] = _normalize_zni_assignments(assignments)
    _save_zni_payload(payload)


def _load_rollout_note_flags():
    return _load_zni_payload().get("flags", {})


def _save_rollout_note_flags(flags):
    payload = _load_zni_payload()
    payload["flags"] = _normalize_rollout_note_flags(flags)
    _save_zni_payload(payload)


def _get_release_auto_color_level(item):
    row_state = str(item.get("row_state") or "").strip().lower()
    if row_state == "final":
        return "success"
    if row_state in {"overdue", "cancelled"}:
        return "danger"
    return ""


def _normalize_manual_release_override(value):
    if not isinstance(value, dict):
        return {}

    normalized = {}
    for key in (
        "release_summary",
        "release_version",
        "release_dist_url",
        "ke",
        "base_release_summary",
        "base_release_version",
        "base_release_dist_url",
        "base_ke",
        "base_zni_key",
        "base_zni_url",
    ):
        raw_value = str(value.get(key) or "").strip()
        if raw_value:
            normalized[key] = raw_value

    zni_key = str(value.get("zni_key") or "").strip()
    zni_url = str(value.get("zni_url") or "").strip()
    clear_zni = bool(value.get("clear_zni"))
    if zni_key:
        normalized["zni_key"] = zni_key
    if zni_url:
        normalized["zni_url"] = zni_url
    if clear_zni:
        normalized["clear_zni"] = True

    updated_at = str(value.get("updated_at") or "").strip()
    if updated_at:
        normalized["updated_at"] = updated_at

    return normalized


def _normalize_manual_release_overrides(payload):
    normalized = {}
    if not isinstance(payload, dict):
        return normalized

    for key, value in payload.items():
        row_key = str(key or "").strip()
        normalized_value = _normalize_manual_release_override(value)
        if row_key and normalized_value:
            normalized[row_key] = normalized_value
    return normalized


def _load_manual_release_overrides():
    return _load_manual_overrides_payload()


def _save_manual_release_overrides(overrides):
    normalized_overrides = _normalize_manual_release_overrides(overrides)
    _save_manual_overrides_payload(normalized_overrides)
    return normalized_overrides


def _load_manual_order():
    global _manual_order_cache

    def _normalize_group_order(value):
        if isinstance(value, dict):
            normalized_buckets = {}
            for bucket, row_keys in value.items():
                bucket_key = str(bucket or "").strip()
                if not bucket_key or not isinstance(row_keys, list):
                    continue
                normalized_keys = []
                for row_key in row_keys:
                    row_key = str(row_key or "").strip()
                    if row_key and row_key not in normalized_keys:
                        normalized_keys.append(row_key)
                if normalized_keys:
                    normalized_buckets[bucket_key] = normalized_keys
            return normalized_buckets

        return [
            str(item or "").strip()
            for item in (value or [])
            if str(item or "").strip()
        ]

    if isinstance(_manual_order_cache, dict):
        return _manual_order_cache

    if not ORDER_FILE.exists():
        _manual_order_cache = {}
        return {}

    try:
        payload = json.loads(ORDER_FILE.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            _manual_order_cache = {}
            return {}

        normalized = {}
        for year, value in payload.items():
            year_key = str(year or "").strip()
            if not year_key or not isinstance(value, dict):
                continue

            normalized[year_key] = {
                "waiting": _normalize_group_order(value.get("waiting")),
                "numbered": _normalize_group_order(value.get("numbered")),
                "force_unnumbered": [
                    str(item or "").strip()
                    for item in (value.get("force_unnumbered") or [])
                    if str(item or "").strip()
                ],
            }
        _manual_order_cache = normalized
        return normalized
    except Exception as exc:
        logging.warning("Release monitor: failed to load manual order: %s", exc)
        _manual_order_cache = {}
        return {}


def _save_manual_order(order_payload):
    global _manual_order_cache

    try:
        _ensure_snapshot_dir()
        _manual_order_cache = dict(order_payload or {})
        ORDER_FILE.write_text(
            json.dumps(order_payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as exc:
        logging.warning("Release monitor: failed to save manual order: %s", exc)


def _remove_row_from_manual_order(row_key):
    row_key = str(row_key or "").strip()
    if not row_key:
        return

    manual_order = _load_manual_order()
    changed = False
    for year_payload in manual_order.values():
        if not isinstance(year_payload, dict):
            continue
        for group_name in ("waiting", "numbered", "force_unnumbered"):
            values = year_payload.get(group_name)
            if isinstance(values, dict):
                next_values = {}
                for bucket, bucket_values in values.items():
                    if not isinstance(bucket_values, list):
                        continue
                    filtered_values = [
                        value
                        for value in bucket_values
                        if not _manual_order_row_matches_release(str(value or "").strip(), row_key)
                    ]
                    if len(filtered_values) != len(bucket_values):
                        changed = True
                    if filtered_values:
                        next_values[bucket] = filtered_values
                if next_values != values:
                    year_payload[group_name] = next_values
                continue

            if isinstance(values, list):
                filtered_values = [
                    value
                    for value in values
                    if not _manual_order_row_matches_release(str(value or "").strip(), row_key)
                ]
                if len(filtered_values) != len(values):
                    year_payload[group_name] = filtered_values
                    changed = True

    if changed:
        _save_manual_order(manual_order)


def _manual_order_row_matches_release(stored_row_key, release_key):
    stored_row_key = str(stored_row_key or "").strip()
    release_key = str(release_key or "").strip()
    if not stored_row_key or not release_key:
        return False
    return stored_row_key == release_key or stored_row_key.startswith(f"{release_key}::")


def _get_release_order_bucket(item):
    raw_value = str(
        item.get("deployment_start_iso")
        or item.get("deployment_start")
        or item.get("sort_date")
        or "no-date"
    ).strip()
    parsed = _parse_release_monitor_date(raw_value)
    if parsed:
        return parsed.strftime("%Y-%m-%d")
    return raw_value[:10] if raw_value else "no-date"


def _build_empty_duty_schedule_payload():
    return {
        "dates": {},
        "availability": {},
        "months": [],
        "files": [],
        "last_upload": None,
    }


def _normalize_duty_availability_payload(value):
    normalized = {}
    if not isinstance(value, dict):
        return normalized

    for date_key, people in value.items():
        normalized_date = str(date_key or "").strip()
        if not normalized_date or not isinstance(people, dict):
            continue

        normalized_people = {}
        for person, info in people.items():
            person_name = str(person or "").strip()
            if not person_name:
                continue

            if isinstance(info, dict):
                status = str(info.get("status") or "").strip()
                availability = str(info.get("availability") or "").strip()
                reason = str(info.get("reason") or "").strip()
            else:
                status = str(info or "").strip()
                availability, reason = _classify_duty_status(status)

            if not status:
                continue
            if not availability:
                availability, reason = _classify_duty_status(status)
            normalized_people[person_name] = {
                "status": status,
                "availability": availability,
                "reason": reason,
            }

        if normalized_people:
            normalized[normalized_date] = normalized_people

    return normalized


def _load_duty_schedule_payload():
    if not DUTY_SCHEDULE_FILE.exists():
        return _build_empty_duty_schedule_payload()

    try:
        payload = json.loads(DUTY_SCHEDULE_FILE.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return _build_empty_duty_schedule_payload()

        dates = {
            str(date_key).strip(): str(reviewer).strip()
            for date_key, reviewer in (payload.get("dates") or {}).items()
            if str(date_key).strip() and str(reviewer).strip()
        }
        availability = _normalize_duty_availability_payload(payload.get("availability") or {})
        months = [
            str(month_label).strip()
            for month_label in (payload.get("months") or [])
            if str(month_label).strip()
        ]
        files = [
            dict(file_info)
            for file_info in (payload.get("files") or [])
            if isinstance(file_info, dict)
        ]
        return {
            "dates": dates,
            "availability": availability,
            "months": list(dict.fromkeys(months)),
            "files": files,
            "last_upload": str(payload.get("last_upload") or "").strip() or None,
        }
    except Exception as exc:
        logging.warning("Release monitor: failed to load duty schedules: %s", exc)
        return _build_empty_duty_schedule_payload()


def _save_duty_schedule_payload(payload):
    try:
        _ensure_snapshot_dir()
        DUTY_SCHEDULE_FILE.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as exc:
        logging.warning("Release monitor: failed to save duty schedules: %s", exc)


def _load_date_overrides():
    if not DATE_OVERRIDES_FILE.exists():
        return {}

    try:
        payload = json.loads(DATE_OVERRIDES_FILE.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return {}

        normalized = {}
        for row_key, value in payload.items():
            key = str(row_key or "").strip()
            if not key or not isinstance(value, dict):
                continue

            start_value = str(value.get("start", "") or "").strip()
            end_value = str(value.get("end", "") or "").strip()
            if not start_value and not end_value:
                continue

            normalized[key] = {
                "start": start_value,
                "end": end_value,
                "updated_at": str(value.get("updated_at", "") or "").strip(),
            }
        return normalized
    except Exception as exc:
        logging.warning("Release monitor: failed to load date overrides: %s", exc)
        return {}


def _save_date_overrides(payload):
    try:
        _ensure_snapshot_dir()
        DATE_OVERRIDES_FILE.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as exc:
        logging.warning("Release monitor: failed to save date overrides: %s", exc)


def _normalize_release_attempt_outcomes(payload):
    normalized = {}
    if not isinstance(payload, dict):
        return normalized

    for row_key, value in payload.items():
        key = str(row_key or "").strip()
        if not key or not isinstance(value, dict):
            continue
        state = str(value.get("state") or "").strip().lower()
        if state != "deferred":
            continue
        normalized[key] = {
            "state": "deferred",
            "release_key": str(value.get("release_key") or "").strip(),
            "rov_key": str(value.get("rov_key") or "").strip(),
            "detected_at": str(value.get("detected_at") or "").strip(),
            "updated_at": str(value.get("updated_at") or "").strip(),
        }
    return normalized


def _load_release_attempt_outcomes():
    if not ATTEMPTS_FILE.exists():
        return {}

    try:
        payload = json.loads(ATTEMPTS_FILE.read_text(encoding="utf-8"))
        return _normalize_release_attempt_outcomes(payload)
    except Exception as exc:
        logging.warning("Release monitor: failed to load release attempt outcomes: %s", exc)
        return {}


def _save_release_attempt_outcomes(payload):
    try:
        _ensure_snapshot_dir()
        ATTEMPTS_FILE.write_text(
            json.dumps(_normalize_release_attempt_outcomes(payload), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as exc:
        logging.warning("Release monitor: failed to save release attempt outcomes: %s", exc)


def _parse_release_monitor_date(value):
    if not value:
        return None

    parsed = _parse_jira_date(str(value))
    if parsed:
        return parsed

    for fmt in ("%d.%m.%Y", "%d.%m.%Y %H:%M", "%d.%m.%Y %H:%M:%S"):
        try:
            return datetime.strptime(str(value), fmt)
        except ValueError:
            continue
    return None


def _format_release_monitor_date(dt_value):
    return dt_value.strftime("%d.%m.%Y") if dt_value else ""


def _is_release_window_expired(end_dt, now_dt=None):
    if not end_dt:
        return False
    now_dt = now_dt or datetime.now()
    if end_dt.time() == datetime.min.time():
        return end_dt.date() < now_dt.date()
    return end_dt < now_dt


def _release_days_overdue(end_dt, now_dt=None):
    if not end_dt or not _is_release_window_expired(end_dt, now_dt):
        return 0
    now_dt = now_dt or datetime.now()
    return max((now_dt.date() - end_dt.date()).days, 0)


def _resolve_effective_release_date(base_dt, manual_dt):
    return manual_dt or base_dt


def _release_dates_match(left_dt, right_dt):
    if not left_dt or not right_dt:
        return False
    return _format_release_monitor_date(left_dt) == _format_release_monitor_date(right_dt)


def _release_base_covers_manual_date(base_dt, manual_dt):
    return _release_dates_match(base_dt, manual_dt)


def _extract_schedule_month_year(sheet_title):
    normalized_title = _normalize_text(sheet_title)
    year_match = re.search(r"(20\d{2})", normalized_title)
    if not year_match:
        return None, None

    month = None
    for month_token, month_number in MONTH_NAME_MAP.items():
        if month_token in normalized_title:
            month = month_number
            break

    if month is None:
        return None, None

    return month, int(year_match.group(1))


def _coerce_schedule_day(value):
    if isinstance(value, int):
        return value if 1 <= value <= 31 else None
    if isinstance(value, float) and value.is_integer():
        day_value = int(value)
        return day_value if 1 <= day_value <= 31 else None

    text_value = str(value or "").strip()
    if text_value.isdigit():
        day_value = int(text_value)
        return day_value if 1 <= day_value <= 31 else None
    return None


def _detect_schedule_day_columns(worksheet):
    max_scan_row = min(12, worksheet.max_row)
    max_scan_col = min(50, worksheet.max_column)

    for row_index in range(1, max_scan_row + 1):
        detected = []
        for col_index in range(2, max_scan_col + 1):
            day_value = _coerce_schedule_day(worksheet.cell(row_index, col_index).value)
            if day_value is not None:
                detected.append((col_index, day_value))

        if len(detected) >= 20:
            return row_index, detected

    return None, []


def _classify_duty_status(status):
    raw_status = str(status or "").strip()
    if not raw_status:
        return "", ""

    normalized_status = _normalize_text(raw_status)
    upper_status = raw_status.upper()
    if upper_status in DUTY_HARD_EXCLUDED_STATUSES:
        return "excluded", {
            "ДД": "Дневной дежурный",
            "ВД": "Вечерний дежурный",
            "ВР": "Вечерний резервный дежурный",
            "ХД": "Хабаровская смена",
            "ХР": "Хабаровский резерв",
        }.get(upper_status, "Дежурство")
    if upper_status in DUTY_RESERVE_STATUSES:
        return "reserve", "Дневной резервный ответственный"
    if any(keyword in normalized_status for keyword in DUTY_ABSENCE_KEYWORDS):
        return "excluded", raw_status
    if "празд" in normalized_status:
        return "excluded", raw_status
    return "available", ""


def _parse_duty_schedule_sheet(worksheet, filename):
    month, year = _extract_schedule_month_year(worksheet.title)
    if not month or not year:
        return {"dates": {}, "months": [], "warnings": []}

    day_row_index, day_columns = _detect_schedule_day_columns(worksheet)
    if not day_row_index or not day_columns:
        return {"dates": {}, "months": [], "warnings": []}

    daily_candidates = defaultdict(list)
    availability_dates = defaultdict(dict)
    max_scan_row = min(worksheet.max_row, day_row_index + 40)

    for row_index in range(day_row_index + 1, max_scan_row + 1):
        raw_name = str(worksheet.cell(row_index, 1).value or "").strip()
        if not raw_name:
            continue

        matched_name = _match_oplot_name(raw_name)
        for col_index, day_number in day_columns:
            raw_marker = str(worksheet.cell(row_index, col_index).value or "").strip()
            marker = _normalize_text(raw_marker)
            if marker != "\u0432\u0434":
                if matched_name and raw_marker:
                    try:
                        day_date = datetime(year, month, day_number).date()
                    except ValueError:
                        continue
                    availability, reason = _classify_duty_status(raw_marker)
                    if availability in {"excluded", "reserve"}:
                        availability_dates[day_date.isoformat()][matched_name] = {
                            "status": raw_marker,
                            "availability": availability,
                            "reason": reason,
                        }
                continue

            if not matched_name:
                daily_candidates[day_number].append({"name": raw_name, "matched": ""})
            else:
                daily_candidates[day_number].append({"name": raw_name, "matched": matched_name})
                try:
                    day_date = datetime(year, month, day_number).date()
                except ValueError:
                    continue
                availability, reason = _classify_duty_status(raw_marker)
                availability_dates[day_date.isoformat()][matched_name] = {
                    "status": raw_marker,
                    "availability": availability,
                    "reason": reason,
                }

    parsed_dates = {}
    warnings = []
    month_label = f"{year:04d}-{month:02d}"

    for day_number, entries in daily_candidates.items():
        try:
            day_date = datetime(year, month, day_number).date()
        except ValueError:
            continue

        if day_date.weekday() >= 5:
            continue

        matched_names = [entry["matched"] for entry in entries if entry.get("matched")]
        unique_names = list(dict.fromkeys(matched_names))

        if len(unique_names) == 1:
            parsed_dates[day_date.isoformat()] = unique_names[0]
            continue

        if not unique_names:
            source_names = ", ".join(entry.get("name", "") for entry in entries if entry.get("name"))
            warnings.append(
                f"{worksheet.title}: не удалось сопоставить ВД за {day_date.strftime('%d.%m.%Y')} ({source_names})"
            )
        else:
            warnings.append(
                f"{worksheet.title}: найдено несколько ВД за {day_date.strftime('%d.%m.%Y')} ({', '.join(unique_names)})"
            )

    if not parsed_dates and not availability_dates:
        return {"dates": {}, "availability": {}, "months": [], "warnings": warnings}

    return {
        "dates": parsed_dates,
        "availability": dict(availability_dates),
        "months": [month_label],
        "warnings": warnings,
    }


def _parse_duty_schedule_workbook(file_bytes, filename):
    workbook = load_workbook(BytesIO(file_bytes), data_only=True)
    merged_dates = {}
    merged_availability = {}
    parsed_months = []
    warnings = []

    for worksheet in workbook.worksheets:
        parsed_sheet = _parse_duty_schedule_sheet(worksheet, filename)
        merged_dates.update(parsed_sheet.get("dates", {}))
        for date_key, people in (parsed_sheet.get("availability") or {}).items():
            merged_availability.setdefault(date_key, {}).update(people or {})
        parsed_months.extend(parsed_sheet.get("months", []))
        warnings.extend(parsed_sheet.get("warnings", []))

    if not merged_dates and not merged_availability:
        raise ValueError(f"\u0412 \u0444\u0430\u0439\u043b\u0435 {filename} \u043d\u0435 \u0443\u0434\u0430\u043b\u043e\u0441\u044c \u043d\u0430\u0439\u0442\u0438 \u043b\u0438\u0441\u0442\u044b \u0441 \u0433\u0440\u0430\u0444\u0438\u043a\u043e\u043c \u0434\u0435\u0436\u0443\u0440\u0441\u0442\u0432")

    return {
        "dates": merged_dates,
        "availability": merged_availability,
        "months": list(dict.fromkeys(parsed_months)),
        "warnings": warnings,
        "files": [{
            "name": filename,
            "uploaded_at": _format_timestamp(),
            "months": list(dict.fromkeys(parsed_months)),
        }],
    }


def _merge_duty_schedule_payload(base_payload, incoming_payload):
    merged = _build_empty_duty_schedule_payload()
    merged["dates"] = dict((base_payload or {}).get("dates") or {})
    merged["dates"].update((incoming_payload or {}).get("dates") or {})
    merged["availability"] = _normalize_duty_availability_payload((base_payload or {}).get("availability") or {})
    for date_key, people in _normalize_duty_availability_payload((incoming_payload or {}).get("availability") or {}).items():
        merged["availability"].setdefault(date_key, {}).update(people)
    merged["months"] = list(
        dict.fromkeys(
            list((base_payload or {}).get("months") or [])
            + list((incoming_payload or {}).get("months") or [])
        )
    )
    merged["files"] = list((base_payload or {}).get("files") or []) + list((incoming_payload or {}).get("files") or [])
    merged["last_upload"] = (
        (incoming_payload or {}).get("last_upload")
        or (base_payload or {}).get("last_upload")
    )
    return merged


def _is_explicit_manual_reviewer_assignment(assignment):
    if not isinstance(assignment, dict):
        return False
    return (
        str(assignment.get("reviewer_source") or "").strip() in {"manual", "manual_text"}
        and str(assignment.get("reviewer_date") or "").strip() == "manual"
    )


def _apply_duty_schedule_assignments(items, persist=False, force=False, debug_limit=0):
    assignments = _load_reviewer_assignments()
    duty_payload = _load_duty_schedule_payload()
    duty_dates = duty_payload.get("dates") or {}
    changed = False
    applied_count = 0
    debug_rows = []

    for item in items:
        assignment_key = _get_assignment_key_for_item(item)
        current_assignment = dict(assignments.get(assignment_key) or {})
        current_reviewer = str(current_assignment.get("reviewer") or "").strip()
        reviewer_source = str(current_assignment.get("reviewer_source") or "").strip()
        if not force and current_reviewer and _is_explicit_manual_reviewer_assignment(current_assignment):
            if debug_limit and len(debug_rows) < debug_limit:
                debug_rows.append({
                    "row_key": assignment_key,
                    "release_key": item.get("release_key", ""),
                    "date": "",
                    "previous": current_reviewer,
                    "scheduled": "",
                    "result": current_reviewer,
                    "reason": "manual",
                })
            continue

        deployment_dt = _parse_release_monitor_date(item.get("deployment_start_iso") or item.get("deployment_start"))
        if not deployment_dt or deployment_dt.date().weekday() >= 5:
            if force or current_reviewer or reviewer_source == "duty_schedule":
                current_assignment["reviewer"] = ""
                current_assignment["reviewer_source"] = ""
                current_assignment["reviewer_date"] = ""
                current_assignment["checker"] = str(current_assignment.get("checker", "") or "").strip()
                raw_responsibles = current_assignment.get("responsibles") or []
                if not isinstance(raw_responsibles, list):
                    raw_responsibles = [raw_responsibles] if raw_responsibles else []
                current_assignment["responsibles"] = [
                    str(value or "").strip()
                    for value in raw_responsibles
                    if str(value or "").strip()
                ]
                if current_assignment["checker"] or current_assignment["responsibles"]:
                    assignments[assignment_key] = current_assignment
                else:
                    assignments.pop(assignment_key, None)
                item["psi_owner"] = ""
                item["psi_owner_source"] = ""
                item["psi_owner_date"] = ""
                item["psi_checker"] = current_assignment["checker"]
                item["psi_responsibles"] = list(current_assignment["responsibles"])
                changed = True
            if debug_limit and len(debug_rows) < debug_limit:
                debug_rows.append({
                    "row_key": assignment_key,
                    "release_key": item.get("release_key", ""),
                    "date": deployment_dt.date().isoformat() if deployment_dt else "",
                    "previous": current_reviewer,
                    "scheduled": "",
                    "result": "",
                    "reason": "no-date-or-weekend",
                })
            continue

        deployment_date = deployment_dt.date()
        reviewer_name = str(duty_dates.get(deployment_date.isoformat()) or "").strip()
        if reviewer_name and reviewer_name not in OPLOT_VALUES:
            reviewer_name = _match_oplot_name(reviewer_name)
        reviewer_name = reviewer_name or ""

        if (
            current_reviewer == reviewer_name
            and reviewer_source == "duty_schedule"
            and str(current_assignment.get("reviewer_date") or "") == deployment_date.isoformat()
            and not force
        ):
            item["psi_owner"] = reviewer_name
            item["psi_owner_source"] = "duty_schedule"
            item["psi_owner_date"] = deployment_date.isoformat()
            continue

        current_assignment["reviewer"] = reviewer_name
        current_assignment["reviewer_source"] = "duty_schedule" if reviewer_name else ""
        current_assignment["reviewer_date"] = deployment_date.isoformat() if reviewer_name else ""
        current_assignment["checker"] = str(current_assignment.get("checker", "") or "").strip()
        raw_responsibles = current_assignment.get("responsibles") or []
        if not isinstance(raw_responsibles, list):
            raw_responsibles = [raw_responsibles] if raw_responsibles else []
        current_assignment["responsibles"] = [
            str(value or "").strip()
            for value in raw_responsibles
            if str(value or "").strip()
        ]
        if current_assignment["reviewer"] or current_assignment["checker"] or current_assignment["responsibles"]:
            assignments[assignment_key] = current_assignment
        else:
            assignments.pop(assignment_key, None)

        item["psi_owner"] = reviewer_name
        item["psi_owner_source"] = current_assignment["reviewer_source"]
        item["psi_owner_date"] = current_assignment["reviewer_date"]
        item["psi_checker"] = current_assignment["checker"]
        item["psi_responsibles"] = list(current_assignment["responsibles"])
        changed = True
        if reviewer_name:
            applied_count += 1
        if debug_limit and len(debug_rows) < debug_limit:
            debug_rows.append({
                "row_key": assignment_key,
                "release_key": item.get("release_key", ""),
                "date": deployment_date.isoformat(),
                "previous": current_reviewer,
                "scheduled": reviewer_name,
                "result": reviewer_name,
                "reason": "updated" if reviewer_name else "no-duty-for-date",
            })

    if changed and persist:
        _save_reviewer_assignments(assignments)

    if debug_limit:
        return {
            "applied_count": applied_count,
            "debug_rows": debug_rows,
        }
    return applied_count


def _append_duty_schedule_meta(meta):
    duty_payload = _load_duty_schedule_payload()
    meta["last_duty_schedule_upload"] = duty_payload.get("last_upload")
    meta["duty_schedule_months"] = list(duty_payload.get("months") or [])
    meta["duty_schedule_files"] = list(duty_payload.get("files") or [])
    return meta


def _apply_date_overrides(items):
    overrides = _load_date_overrides()
    overrides_changed = False
    for item in items:
        row_key = _get_assignment_key_for_item(item)
        override = overrides.get(row_key)
        if not override:
            source_start_dt = _parse_release_monitor_date(
                item.get("source_deployment_start_iso") or item.get("source_deployment_start")
            )
            source_end_dt = _parse_release_monitor_date(
                item.get("source_deployment_end_iso") or item.get("source_deployment_end")
            )
            source_sort_dt = source_start_dt or source_end_dt
            if source_start_dt:
                item["deployment_start"] = _format_release_monitor_date(source_start_dt)
                item["deployment_start_iso"] = source_start_dt.isoformat()
            else:
                item["deployment_start"] = ""
                item["deployment_start_iso"] = ""
            if source_end_dt:
                item["deployment_end"] = _format_release_monitor_date(source_end_dt)
                item["deployment_end_iso"] = source_end_dt.isoformat()
            else:
                item["deployment_end"] = ""
                item["deployment_end_iso"] = ""
            item["sort_date"] = source_sort_dt.isoformat() if source_sort_dt else ""
            if item.get("is_non_final"):
                now_dt = datetime.now()
                today = now_dt.date()
                source_start_date = source_start_dt.date() if source_start_dt else None
                source_end_date = source_end_dt.date() if source_end_dt else None
                item["is_overdue"] = _is_release_window_expired(source_end_dt, now_dt)
                item["is_today"] = bool(
                    (source_start_date and source_start_date == today)
                    or (source_end_date and source_end_date == today)
                )
                item["days_overdue"] = _release_days_overdue(source_end_dt, now_dt)
                if item.get("is_overdue"):
                    item["row_state"] = "overdue"
                elif item.get("is_today"):
                    item["row_state"] = "today"
                else:
                    item["row_state"] = "planned"
            item["has_manual_date_override"] = False
            item["manual_deployment_start"] = ""
            item["manual_deployment_end"] = ""
            continue

        manual_start_dt = _parse_release_monitor_date(override.get("start"))
        manual_end_dt = _parse_release_monitor_date(override.get("end"))
        base_start_dt = _parse_release_monitor_date(
            item.get("source_deployment_start_iso")
            or item.get("source_deployment_start")
        )
        base_end_dt = _parse_release_monitor_date(
            item.get("source_deployment_end_iso")
            or item.get("source_deployment_end")
        )

        manual_start_matches_base = _release_base_covers_manual_date(base_start_dt, manual_start_dt)
        manual_end_matches_base = _release_base_covers_manual_date(base_end_dt, manual_end_dt)
        active_manual_start = manual_start_dt if not manual_start_matches_base else None
        active_manual_end = manual_end_dt if not manual_end_matches_base else None

        if manual_start_matches_base or manual_end_matches_base:
            cleaned_override = dict(override)
            if manual_start_matches_base:
                cleaned_override["start"] = ""
            if manual_end_matches_base:
                cleaned_override["end"] = ""

            if cleaned_override.get("start") or cleaned_override.get("end"):
                overrides[row_key] = cleaned_override
                override = cleaned_override
            else:
                overrides.pop(row_key, None)
                override = {}
            overrides_changed = True

        effective_start_dt = _resolve_effective_release_date(base_start_dt, active_manual_start)
        effective_end_dt = _resolve_effective_release_date(base_end_dt, active_manual_end)

        if effective_start_dt:
            item["deployment_start"] = _format_release_monitor_date(effective_start_dt)
            item["deployment_start_iso"] = effective_start_dt.isoformat()
            item["sort_date"] = effective_start_dt.isoformat()
        if effective_end_dt:
            item["deployment_end"] = _format_release_monitor_date(effective_end_dt)
            item["deployment_end_iso"] = effective_end_dt.isoformat()

        row_is_non_final = bool(item.get("is_non_final"))
        if row_is_non_final:
            now_dt = datetime.now()
            today = now_dt.date()
            effective_start_date = effective_start_dt.date() if effective_start_dt else None
            effective_end_date = effective_end_dt.date() if effective_end_dt else None
            item["is_overdue"] = _is_release_window_expired(effective_end_dt, now_dt)
            item["is_today"] = bool(
                (effective_start_date and effective_start_date == today)
                or (effective_end_date and effective_end_date == today)
            )
            item["days_overdue"] = _release_days_overdue(effective_end_dt, now_dt)

            if item.get("is_overdue"):
                item["row_state"] = "overdue"
            elif item.get("is_today"):
                item["row_state"] = "today"
            else:
                item["row_state"] = "planned"

        item["has_manual_date_override"] = bool(active_manual_start or active_manual_end)
        item["manual_deployment_start"] = override.get("start", "") if active_manual_start else ""
        item["manual_deployment_end"] = override.get("end", "") if active_manual_end else ""

    if overrides_changed:
        _save_date_overrides(overrides)
    return items


def _apply_reviewer_assignments(items):
    assignments = _load_reviewer_assignments()
    for item in items:
        assignment_key = _get_assignment_key_for_item(item)
        release_assignment = assignments.get(assignment_key) or assignments.get(item.get("release_key"), {})
        item["psi_owner"] = release_assignment.get("reviewer", "")
        item["psi_owner_source"] = release_assignment.get("reviewer_source", "")
        item["psi_owner_date"] = release_assignment.get("reviewer_date", "")
        item["psi_checker"] = release_assignment.get("checker", "")
        item["psi_responsibles"] = list(release_assignment.get("responsibles", []))
    return items


def _get_current_week_bounds(reference_dt=None):
    reference_dt = reference_dt or datetime.now()
    week_start = reference_dt.date() - timedelta(days=reference_dt.weekday())
    week_end = week_start + timedelta(days=6)
    return week_start, week_end


def _get_release_start_date(item):
    deployment_dt = _parse_release_monitor_date(
        item.get("deployment_start_iso")
        or item.get("deployment_start")
        or item.get("sort_date")
    )
    return deployment_dt.date() if deployment_dt else None


def _has_release_responsible(item):
    responsibles = item.get("psi_responsibles") or []
    if not isinstance(responsibles, list):
        responsibles = [responsibles] if responsibles else []
    return any(str(value or "").strip() for value in responsibles)


def _is_release_assignment_relevant_for_week(item, week_start=None, week_end=None):
    week_start, week_end = (week_start, week_end) if week_start and week_end else _get_current_week_bounds()
    start_date = _get_release_start_date(item)
    if not start_date or start_date < week_start or start_date > week_end:
        return False
    if item.get("is_cancelled") or item.get("is_final"):
        return False
    return True


def _apply_week_control_flags(items):
    week_start, week_end = _get_current_week_bounds()
    for item in items:
        is_week_release = _is_release_assignment_relevant_for_week(item, week_start, week_end)
        missing_responsible = bool(is_week_release and not _has_release_responsible(item))
        item["is_current_week_assignment_scope"] = is_week_release
        item["is_missing_week_responsible"] = missing_responsible
    return items


def _collect_week_candidate_availability(week_start=None, week_end=None):
    week_start, week_end = (week_start, week_end) if week_start and week_end else _get_current_week_bounds()
    duty_payload = _load_duty_schedule_payload()
    availability_by_date = duty_payload.get("availability") or {}

    candidates = {
        name: {
            "name": name,
            "availability": "available",
            "reasons": [],
            "statuses": [],
        }
        for name in OPLOT_VALUES
    }

    current_date = week_start
    while current_date <= week_end:
        day_people = availability_by_date.get(current_date.isoformat()) or {}
        for name, info in day_people.items():
            matched_name = name if name in candidates else _match_oplot_name(name)
            if not matched_name or matched_name not in candidates:
                continue
            status = str((info or {}).get("status") or "").strip()
            availability = str((info or {}).get("availability") or "").strip()
            reason = str((info or {}).get("reason") or "").strip() or status
            if not availability:
                availability, reason = _classify_duty_status(status)
            entry = candidates[matched_name]
            if status:
                entry["statuses"].append({
                    "date": current_date.isoformat(),
                    "status": status,
                    "reason": reason,
                })
            if availability == "excluded":
                entry["availability"] = "excluded"
                if reason and reason not in entry["reasons"]:
                    entry["reasons"].append(reason)
            elif availability == "reserve" and entry["availability"] != "excluded":
                entry["availability"] = "reserve"
                if reason and reason not in entry["reasons"]:
                    entry["reasons"].append(reason)
        current_date += timedelta(days=1)

    grouped = {"available": [], "reserve": [], "excluded": []}
    for entry in candidates.values():
        grouped[entry["availability"]].append(entry)
    for values in grouped.values():
        values.sort(key=lambda item: item["name"])
    return grouped


def _apply_release_status_consistency(items):
    final_status = _normalize_text(FINAL_RELEASE_STATUS)
    cancelled_status = _normalize_text(CANCELLED_RELEASE_STATUS)
    pre_final_statuses = {_normalize_text(status) for status in PRE_FINAL_RELEASE_STATUSES}

    for item in items:
        normalized_status = _normalize_text(item.get("release_status"))
        is_final_status = normalized_status == final_status
        is_cancelled_status = normalized_status == cancelled_status

        if is_final_status:
            item["is_final"] = True
            item["is_cancelled"] = False
            item["is_non_final"] = False
            item["is_pre_final"] = False
            item["is_ready_for_prom"] = False
            item["is_overdue"] = False
            item["is_today"] = False
            item["days_overdue"] = 0
            item["row_state"] = "final"
            continue

        if is_cancelled_status:
            item["is_final"] = False
            item["is_cancelled"] = True
            item["is_non_final"] = False
            item["is_pre_final"] = False
            item["is_ready_for_prom"] = False
            item["is_overdue"] = False
            item["is_today"] = False
            item["days_overdue"] = 0
            item["row_state"] = "cancelled"
            continue

        item["is_final"] = False
        item["is_cancelled"] = False
        item["is_non_final"] = True
        item["is_pre_final"] = normalized_status in pre_final_statuses

    return items


def _apply_release_attempt_outcomes(items):
    outcomes = _load_release_attempt_outcomes()
    changed = False
    now_text = _format_timestamp()

    for item in items:
        row_key = _get_assignment_key_for_item(item)
        if not row_key:
            continue

        if (
            item.get("has_rov")
            and not item.get("is_final")
            and not item.get("is_cancelled")
            and item.get("is_overdue")
        ):
            current = dict(outcomes.get(row_key) or {})
            outcomes[row_key] = {
                "state": "deferred",
                "release_key": str(item.get("release_key") or "").strip(),
                "rov_key": str(item.get("rov_key") or "").strip(),
                "detected_at": current.get("detected_at") or now_text,
                "updated_at": now_text,
            }
            changed = True

    if changed:
        _save_release_attempt_outcomes(outcomes)

    deferred_keys = set(outcomes)
    for item in items:
        row_key = _get_assignment_key_for_item(item)
        if row_key not in deferred_keys:
            item["is_deferred_attempt"] = False
            continue

        item["is_deferred_attempt"] = True
        item["is_final"] = False
        item["is_non_final"] = True
        item["is_pre_final"] = False
        item["is_ready_for_prom"] = False
        item["is_overdue"] = True
        item["is_today"] = False
        item["days_overdue"] = _release_days_overdue(
            _parse_release_monitor_date(item.get("deployment_end_iso") or item.get("deployment_end"))
        )
        item["row_state"] = "overdue"

    return items


def _apply_zni_assignments(items):
    assignments = _load_zni_assignments()
    flags = _load_rollout_note_flags()
    flags_changed = False
    for item in items:
        assignment_key = _get_assignment_key_for_item(item)
        zni_assignment = assignments.get(assignment_key) or assignments.get(item.get("release_key"), {})
        zni_key = str(zni_assignment.get("key") or "").strip() if isinstance(zni_assignment, dict) else ""
        zni_url = str(zni_assignment.get("url") or "").strip() if isinstance(zni_assignment, dict) else ""
        item["zni_key"] = zni_key
        item["zni_url"] = zni_url
        if not str(item.get("base_zni_key") or "").strip():
            item["base_zni_key"] = zni_key
        if not str(item.get("base_zni_url") or "").strip():
            item["base_zni_url"] = zni_url
        rollout_flag_key = assignment_key if assignment_key in flags else item.get("release_key")
        rollout_note = flags.get(rollout_flag_key) or {}
        rollout_level = ""
        if isinstance(rollout_note, dict) and rollout_note.get("has_rollout_notes"):
            rollout_level = str(rollout_note.get("rollout_notes_level") or "warning").strip().lower()
            if rollout_level not in {"success", "warning", "danger"}:
                rollout_level = "warning"
            if rollout_level == _get_release_auto_color_level(item):
                if rollout_flag_key in flags:
                    flags.pop(rollout_flag_key, None)
                    flags_changed = True
                rollout_level = ""
        item["rollout_notes_level"] = rollout_level
        item["has_rollout_notes"] = bool(rollout_level)
    if flags_changed:
        _save_rollout_note_flags(flags)
    return items


def _get_release_name_ke_line(item):
    lines = item.get("base_release_name_lines") or item.get("release_name_lines") or []
    if isinstance(lines, list) and len(lines) > 1:
        line_value = str(lines[1] or "").strip()
        if line_value:
            return line_value

    ke_name = str(item.get("ke_name") or "").strip()
    ke_id = str(item.get("ke_id") or "").strip()
    if ke_name and ke_id:
        return f"{ke_name}({ke_id})"
    return ke_name or ""


def _get_release_domain_from_url(url_value):
    url_value = str(url_value or "").strip()
    if not url_value:
        return ""
    if "/browse/" in url_value:
        return url_value.split("/browse/", 1)[0].rstrip("/")
    return url_value.rsplit("/", 1)[0].rstrip("/")


def _build_delta_issue_url(issue_key):
    issue_key = str(issue_key or "").strip()
    return f"{JIRA_DELTA_BASE}/browse/{issue_key}" if issue_key else ""


def _resolve_manual_zni_url(issue_key, url_value=""):
    url_value = str(url_value or "").strip()
    if not url_value or "/browse/" in url_value:
        return _build_delta_issue_url(issue_key)
    return url_value


def _apply_manual_release_overrides(items, overrides=None):
    overrides = _normalize_manual_release_overrides(overrides) if overrides is not None else _load_manual_release_overrides()
    for item in items:
        assignment_key = _get_assignment_key_for_item(item)
        override = overrides.get(assignment_key) or overrides.get(item.get("release_key"), {})
        override_base_summary = str(override.get("base_release_summary") or "").strip() if isinstance(override, dict) else ""
        override_base_version = str(override.get("base_release_version") or "").strip() if isinstance(override, dict) else ""
        override_base_url = str(override.get("base_release_dist_url") or "").strip() if isinstance(override, dict) else ""
        override_base_ke = str(override.get("base_ke") or "").strip() if isinstance(override, dict) else ""
        override_base_zni_key = str(override.get("base_zni_key") or "").strip() if isinstance(override, dict) else ""
        override_base_zni_url = str(override.get("base_zni_url") or "").strip() if isinstance(override, dict) else ""
        if not str(item.get("base_release_summary") or "").strip():
            item["base_release_summary"] = override_base_summary or str(item.get("release_summary") or "").strip()
        if not str(item.get("base_release_version") or "").strip():
            item["base_release_version"] = override_base_version or str(item.get("release_version") or "").strip()
        if not str(item.get("base_release_dist_url") or "").strip():
            item["base_release_dist_url"] = override_base_url or str(item.get("release_dist_url") or "").strip()
        if not str(item.get("base_ke") or "").strip():
            item["base_ke"] = override_base_ke or str(item.get("ke") or "").strip()
        if not str(item.get("base_zni_key") or "").strip():
            item["base_zni_key"] = override_base_zni_key or str(item.get("zni_key") or "").strip()
        if not str(item.get("base_zni_url") or "").strip():
            item["base_zni_url"] = override_base_zni_url or str(item.get("zni_url") or "").strip()
        if not isinstance(item.get("base_release_name_lines"), list) or not item.get("base_release_name_lines"):
            item["base_release_name_lines"] = list(item.get("release_name_lines") or [])

        base_summary = str(item.get("base_release_summary") or item.get("release_summary") or "").strip()
        base_version = str(item.get("base_release_version") or item.get("release_version") or "").strip()
        base_url = str(item.get("base_release_dist_url") or item.get("release_dist_url") or "").strip()
        base_ke = str(item.get("base_ke") or item.get("ke") or "").strip()
        base_zni_key = str(item.get("base_zni_key") or item.get("zni_key") or "").strip()
        base_zni_url = str(item.get("base_zni_url") or item.get("zni_url") or "").strip()
        base_name_lines = item.get("base_release_name_lines")

        item["release_summary"] = base_summary
        item["release_version"] = base_version
        item["release_dist_url"] = base_url
        item["ke"] = base_ke
        item["zni_key"] = base_zni_key
        item["zni_url"] = base_zni_url
        if isinstance(base_name_lines, list) and base_name_lines:
            item["release_name_lines"] = list(base_name_lines)

        if not isinstance(override, dict) or not override:
            item["has_manual_release_override"] = False
            item["manual_release_summary"] = ""
            item["manual_release_version"] = ""
            item["manual_release_dist_url"] = ""
            item["manual_ke"] = ""
            item["manual_zni_key"] = ""
            item["manual_zni_url"] = ""
            item["manual_clear_zni"] = False
            continue

        manual_summary = str(override.get("release_summary") or "").strip()
        manual_version = str(override.get("release_version") or "").strip()
        manual_url = _normalize_artifact_url(str(override.get("release_dist_url") or "").strip())
        manual_ke = str(override.get("ke") or "").strip()
        manual_zni_key = str(override.get("zni_key") or "").strip()
        manual_zni_url = str(override.get("zni_url") or "").strip()
        clear_zni = bool(override.get("clear_zni"))

        if manual_summary:
            item["release_summary"] = manual_summary
            item["release_name_lines"] = _build_release_name_lines(
                manual_summary,
                _get_release_name_ke_line(item),
                row_label=str(item.get("row_label") or "(Релиз)"),
            )
        if manual_version:
            item["release_version"] = manual_version
        if manual_url:
            item["release_dist_url"] = manual_url
        if manual_ke:
            item["ke"] = _format_ke_id(manual_ke) if re.fullmatch(r"\d+", manual_ke) else manual_ke
        if clear_zni:
            item["zni_key"] = ""
            item["zni_url"] = ""
        elif manual_zni_key:
            item["zni_key"] = manual_zni_key
            item["zni_url"] = _resolve_manual_zni_url(manual_zni_key, manual_zni_url)

        item["manual_release_summary"] = manual_summary
        item["manual_release_version"] = manual_version
        item["manual_release_dist_url"] = manual_url
        item["manual_ke"] = manual_ke
        item["manual_zni_key"] = manual_zni_key
        item["manual_zni_url"] = manual_zni_url
        item["manual_clear_zni"] = clear_zni
        item["has_manual_release_override"] = bool(
            manual_summary or manual_version or manual_url or manual_ke or manual_zni_key or manual_zni_url or clear_zni
        )
    return items


def _get_assignment_key_for_item(item):
    if not isinstance(item, dict):
        return ""
    if item.get("row_key"):
        return str(item.get("row_key"))

    release_key = str(item.get("release_key") or "").strip()
    rov_key = str(item.get("rov_key") or "").strip()
    if release_key:
        return f"{release_key}::{rov_key or 'no-rov'}"
    return ""


def _normalize_text(value):
    return str(value or "").strip().lower().replace("С‘", "Рµ")


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


def _normalize_cell_text(value):
    value = (value or "").replace("\xa0", " ")
    value = re.sub(r"\s*\n\s*", "\n", value)
    value = re.sub(r"[ \t]+", " ", value)
    return value.strip()


class _ConfluenceTableParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tables = []
        self._table_depth = 0
        self._current_table = None
        self._current_row = None
        self._current_cell = None

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self._table_depth += 1
            if self._table_depth == 1:
                self._current_table = []
            return

        if self._table_depth != 1:
            return

        if tag == "tr":
            self._current_row = []
        elif tag in {"td", "th"}:
            self._current_cell = {"text_parts": [], "links": []}
        elif tag == "a" and self._current_cell is not None:
            href = dict(attrs).get("href")
            if href:
                self._current_cell["links"].append(href)
        elif tag in {"br", "p", "div", "li"} and self._current_cell is not None:
            self._current_cell["text_parts"].append("\n")

    def handle_data(self, data):
        if self._table_depth == 1 and self._current_cell is not None:
            self._current_cell["text_parts"].append(data)

    def handle_endtag(self, tag):
        if tag == "table":
            if self._table_depth == 1 and self._current_table is not None:
                self.tables.append(self._current_table)
                self._current_table = None
            self._table_depth = max(0, self._table_depth - 1)
            return

        if self._table_depth != 1:
            return

        if tag in {"td", "th"} and self._current_row is not None and self._current_cell is not None:
            self._current_row.append(
                {
                    "text": _normalize_cell_text("".join(self._current_cell["text_parts"])),
                    "links": list(self._current_cell["links"]),
                }
            )
            self._current_cell = None
        elif tag == "tr" and self._current_table is not None and self._current_row is not None:
            if self._current_row:
                self._current_table.append(self._current_row)
            self._current_row = None


def _extract_issue_key_from_cell(cell):
    cell = cell or {}
    for href in cell.get("links", []):
        match = re.search(r"/browse/([A-Z]+-\d+)", href or "")
        if match:
            return match.group(1)

    match = re.search(r"([A-Z]+-\d+)", cell.get("text", "") or "")
    return match.group(1) if match else ""


def _match_oplot_name(raw_name):
    normalized_raw = _normalize_text(raw_name).replace(".", "")
    if not normalized_raw:
        return ""

    exact_map = {
        _normalize_text(option).replace(".", ""): option
        for option in OPLOT_VALUES
    }
    if normalized_raw in exact_map:
        return exact_map[normalized_raw]

    surname_match = re.match(r"^([\u0430-\u044f\u0451a-z-]+)\s+([\u0430-\u044f\u0451a-z])", normalized_raw, re.IGNORECASE)
    if not surname_match:
        return ""

    surname = surname_match.group(1)
    first_initial = surname_match.group(2)
    candidates = []
    for option in OPLOT_VALUES:
        normalized_option = _normalize_text(option).replace(".", "")
        option_match = re.match(r"^([\u0430-\u044f\u0451a-z-]+)\s+([\u0430-\u044f\u0451a-z])", normalized_option, re.IGNORECASE)
        if option_match and option_match.group(1) == surname and option_match.group(2) == first_initial:
            candidates.append(option)

    return candidates[0] if len(candidates) == 1 else ""


def _parse_confluence_assignment_cell(cell_text):
    responsibles = []
    checker_lines = []
    mode = "responsibles"

    lines = [
        line.strip(" /")
        for line in re.split(r"[\n\r]+", cell_text or "")
        if line.strip(" /")
    ]

    for line in lines:
        normalized_line = _normalize_text(line)

        if normalized_line.startswith("\u043f\u0440\u043e\u0432\u0435\u0440\u044f\u0435\u0442"):
            mode = "checker"
            remainder = line.split(":", 1)[1].strip() if ":" in line else ""
            if remainder and "\u043e\u0442\u0441\u0443\u0442\u0441\u0442\u0432" not in _normalize_text(remainder):
                checker_lines.append(remainder)
            continue

        if normalized_line.startswith("\u0443\u0441\u0442\u0430\u043d\u0430\u0432\u043b\u0438\u0432\u0430\u0435\u0442"):
            mode = "ignore"
            continue

        if "\u043f\u0440\u043e\u0432\u0435\u0440\u043a\u0438 \u043e\u0442\u0441\u0443\u0442\u0441\u0442\u0432" in normalized_line:
            checker_lines = []
            mode = "ignore"
            continue

        if mode == "responsibles":
            for part in [chunk.strip(" /") for chunk in line.split("/")]:
                if part:
                    matched_name = _match_oplot_name(part)
                    if matched_name and matched_name not in responsibles:
                        responsibles.append(matched_name)
        elif mode == "checker":
            checker_lines.append(line.strip())

    checker = " ".join(checker_lines).strip()
    return responsibles, checker


def _find_release_assignment_table(storage_html):
    parser = _ConfluenceTableParser()
    parser.feed(storage_html or "")

    for table in parser.tables:
        if not table:
            continue
        headers = [_normalize_text(cell.get("text", "")) for cell in table[0]]
        if (
            any("id \u0440\u0435\u043b\u0438\u0437\u0430" in header for header in headers)
            and any("id \u0440\u0430\u0441\u043f\u043e\u0440\u044f\u0436\u0435\u043d\u0438\u044f" in header for header in headers)
            and any("\u043e\u0442\u0432\u0435\u0442\u0441\u0442\u0432\u0435\u043d\u043d\u044b\u0439" in header for header in headers)
        ):
            return table
    return []


def _get_confluence_release_page_id(year):
    # Публикуем только в одну заранее зафиксированную страницу релизов.
    return "18369778404"


def _fetch_confluence_release_page(year):
    page_id = _get_confluence_release_page_id(year)
    token = str(TOKENS.get("confluence_delta_token", "") or "").strip()
    if not page_id:
        raise ValueError(f"Не настроен pageId Confluence для {year} года")
    if not token:
        raise ValueError("Не настроен token доступа к Confluence")

    url = f"{CONFLUENCE_DELTA_BASE}/rest/api/content/{page_id}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    params = {"expand": "body.storage,version,title"}
    response = requests.get(url, headers=headers, params=params, verify=False, timeout=60)
    if not response.ok:
        detail = _extract_confluence_error_detail(response)
        raise ValueError(f"Confluence GET failed ({response.status_code}): {detail}")
    data = response.json()
    return {
        "page_id": page_id,
        "title": str(data.get("title") or "").strip(),
        "version": int(((data.get("version") or {}).get("number")) or 0),
        "storage_html": (((data.get("body") or {}).get("storage") or {}).get("value") or ""),
        "raw": data,
    }


def _extract_confluence_error_detail(response):
    try:
        payload = response.json()
        if isinstance(payload, dict):
            for key in ("message", "errorMessage", "reason"):
                value = str(payload.get(key) or "").strip()
                if value:
                    return value
            errors = payload.get("errors")
            if isinstance(errors, dict) and errors:
                return "; ".join(f"{key}: {value}" for key, value in errors.items())
            if isinstance(errors, list) and errors:
                return "; ".join(str(item) for item in errors if str(item).strip())
            return str(payload)[:1000]
    except Exception:
        pass

    body = str(getattr(response, "text", "") or "").strip()
    if body:
        return body[:1000]
    return str(getattr(response, "reason", "") or "empty response")


def _render_confluence_release_link(url, text):
    label = html.escape(str(text or "").strip()) or html.escape("—")
    href = str(url or "").strip()
    if not href:
        return label
    return f'<a href="{html.escape(href, quote=True)}">{label}</a>'


def _render_confluence_release_name_lines(item):
    lines = [str(line or "").strip() for line in (item.get("release_name_lines") or []) if str(line or "").strip()]
    if not lines:
        fallback = str(item.get("release_summary") or "").strip()
        if fallback:
            lines = [fallback]
    if not lines:
        return html.escape("—")
    return "".join(f"<div>{html.escape(line)}</div>" for line in lines)


def _render_confluence_release_name_cell(item):
    lines = [str(line or "").strip() for line in (item.get("release_name_lines") or []) if str(line or "").strip()]
    if not lines:
        fallback = str(item.get("release_summary") or "").strip()
        if fallback:
            lines = [fallback]

    build_version = str(item.get("release_version") or "").strip()
    build_url = str(item.get("release_dist_url") or "").strip()
    build_line = ""
    if build_version:
        build_label = html.escape(build_version)
        if build_url:
            build_line = (
                '<div class="release-name-line">'
                f'сборка: <a href="{html.escape(build_url, quote=True)}">{build_label}</a>'
                '</div>'
            )
        else:
            build_line = f'<div class="release-name-line">сборка: {build_label}</div>'

    if not lines and not build_line:
        return html.escape("—")

    rendered_lines = "".join(f"<div>{html.escape(line)}</div>" for line in lines)
    return rendered_lines + build_line


def _render_confluence_release_assignment_cell(item):
    responsibles = [
        str(value or "").strip()
        for value in (item.get("psi_responsibles") or [])
        if str(value or "").strip()
    ]
    checker = str(item.get("psi_checker") or "").strip()

    cell_lines = []
    if responsibles:
        cell_lines.append(" / ".join(html.escape(name) for name in responsibles))
    else:
        cell_lines.append(html.escape("Отсутствует"))

    if checker:
        cell_lines.append(f"Проверяет:<br/>{html.escape(checker)}")
    else:
        cell_lines.append("Проверяет:<br/>Отсутствует")

    return "<br/>".join(cell_lines)


def _parse_release_monitor_date_value(value):
    raw_value = str(value or "").strip()
    if not raw_value:
        return None

    for fmt in ("%d.%m.%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw_value, fmt)
        except ValueError:
            continue

    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(raw_value[:19], fmt)
        except ValueError:
            continue

    return None


def _is_release_far_future(item):
    deployment_date = (
        _parse_release_monitor_date_value(item.get("deployment_start"))
        or _parse_release_monitor_date_value(item.get("deployment_start_iso"))
        or _parse_release_monitor_date_value(item.get("source_deployment_start"))
        or _parse_release_monitor_date_value(item.get("source_deployment_start_iso"))
    )
    if not deployment_date:
        return False

    today = datetime.now().date()
    threshold = today + timedelta(days=10)
    return deployment_date.date() > threshold


def _render_confluence_release_row(item):
    row_state = str(item.get("row_state") or "planned").strip().lower()
    row_bg = {
        "notes": "#fff7db",
        "final": "#eaf7ef",
        "cancelled": "#fdebec",
        "overdue": "#fff0f0",
        "today": "",
        "planned": "",
    }.get(row_state, "")
    rollout_level = str(item.get("rollout_notes_level") or ("warning" if item.get("has_rollout_notes") else "")).strip().lower()
    if rollout_level == "success":
        row_bg = "#eaf7ef"
    elif rollout_level == "danger":
        row_bg = "#fdebec"
    elif rollout_level == "warning":
        row_bg = "#fff7db"
    row_style = f' style="background-color: {row_bg};"' if row_bg else ""

    row_number = str(item.get("release_number") or "").strip() or "—"
    zni_key = str(item.get("zni_key") or "").strip()
    ke_value = str(item.get("ke") or "").strip() or "—"
    release_key = str(item.get("release_key") or "").strip()
    rov_key = str(item.get("rov_key") or "").strip()
    start_date = str(item.get("deployment_start") or "").strip() or "—"
    end_date = str(item.get("deployment_end") or "").strip() or "—"

    cells = [
        f"<td>{html.escape(row_number)}</td>",
        f"<td>{_render_confluence_release_name_cell(item)}</td>",
        f"<td>{_render_confluence_release_link(item.get('zni_url'), zni_key) if zni_key else html.escape('—')}</td>",
        f"<td>{html.escape(ke_value)}</td>",
        f"<td>{_render_confluence_release_link(item.get('release_url'), release_key)}</td>",
        f"<td>{_render_confluence_release_link(item.get('rov_url'), rov_key) if rov_key else html.escape('—')}</td>",
        f"<td>{html.escape(start_date)}</td>",
        f"<td>{html.escape(end_date)}</td>",
        f"<td>{_render_confluence_release_assignment_cell(item)}</td>",
    ]
    return f"<tr{row_style}>{''.join(cells)}</tr>"


def _build_confluence_release_table(items, year):
    rows = []
    for item in items or []:
        if int(item.get("year", 0) or 0) != int(year or 0):
            continue
        if item.get("is_unnumbered"):
            continue
        if _is_release_far_future(item):
            continue
        rows.append(_render_confluence_release_row(item))

    table_rows = "".join(rows)
    return f"""
<table data-layout="wide" style="width: 100%; border-collapse: collapse; table-layout: fixed;">
    <colgroup>
        <col style="width: 5%;" />
        <col style="width: 28%;" />
        <col style="width: 9%;" />
        <col style="width: 10%;" />
        <col style="width: 12%;" />
        <col style="width: 12%;" />
        <col style="width: 8%;" />
        <col style="width: 8%;" />
        <col style="width: 18%;" />
    </colgroup>
    <thead>
        <tr>
            <th style="border: 1px solid #d9e2f2; background: #f6f8fc; padding: 8px 6px; text-align: left;">№ релиза</th>
            <th style="border: 1px solid #d9e2f2; background: #f6f8fc; padding: 8px 6px; text-align: left;">Название релиза</th>
            <th style="border: 1px solid #d9e2f2; background: #f6f8fc; padding: 8px 6px; text-align: left;">№ ЗНИ</th>
            <th style="border: 1px solid #d9e2f2; background: #f6f8fc; padding: 8px 6px; text-align: left;">КЭ</th>
            <th style="border: 1px solid #d9e2f2; background: #f6f8fc; padding: 8px 6px; text-align: left;">ID релиза</th>
            <th style="border: 1px solid #d9e2f2; background: #f6f8fc; padding: 8px 6px; text-align: left;">ID распоряжения</th>
            <th style="border: 1px solid #d9e2f2; background: #f6f8fc; padding: 8px 6px; text-align: left;">Дата начала внедрения</th>
            <th style="border: 1px solid #d9e2f2; background: #f6f8fc; padding: 8px 6px; text-align: left;">Дата окончания внедрения</th>
            <th style="border: 1px solid #d9e2f2; background: #f6f8fc; padding: 8px 6px; text-align: left;">Ответственный за ПСИ/Проверки</th>
        </tr>
    </thead>
    <tbody>
        {table_rows}
    </tbody>
</table>
""".strip()


def _replace_release_monitor_table_in_storage(storage_html, replacement_table_html):
    if not storage_html:
        return replacement_table_html, False

    table_pattern = re.compile(r"<table\b[^>]*>.*?</table>", re.IGNORECASE | re.DOTALL)
    for match in table_pattern.finditer(storage_html):
        if _find_release_assignment_table(match.group(0)):
            updated_html = storage_html[:match.start()] + replacement_table_html + storage_html[match.end():]
            return updated_html, True
    return storage_html, False


def _load_confluence_release_assignments(year):
    page_id = "18369778404"
    token = str(TOKENS.get("confluence_delta_token", "") or "").strip()
    if not page_id:
        raise ValueError(f"РќРµ РЅР°СЃС‚СЂРѕРµРЅ pageId Confluence РґР»СЏ {year} РіРѕРґР°")
    if not token:
        raise ValueError("РќРµ РЅР°СЃС‚СЂРѕРµРЅ С‚РѕРєРµРЅ РґРѕСЃС‚СѓРїР° Рє Confluence")

    url = f"{CONFLUENCE_DELTA_BASE}/rest/api/content/{page_id}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    params = {"expand": "body.storage,version"}
    response = requests.get(url, headers=headers, params=params, verify=False, timeout=60)
    response.raise_for_status()
    data = response.json()
    storage_html = (((data.get("body") or {}).get("storage") or {}).get("value") or "")
    table = _find_release_assignment_table(storage_html)
    if not table:
        raise ValueError("РќРµ СѓРґР°Р»РѕСЃСЊ РЅР°Р№С‚Рё С‚Р°Р±Р»РёС†Сѓ СЂРµР»РёР·РѕРІ РЅР° СЃС‚СЂР°РЅРёС†Рµ Confluence")

    assignments = {}
    for row in table[1:]:
        if len(row) < 9:
            continue
        release_key = _extract_issue_key_from_cell(row[4])
        rov_key = _extract_issue_key_from_cell(row[5])
        if not release_key:
            continue

        responsibles, checker = _parse_confluence_assignment_cell(row[8].get("text", ""))
        if not responsibles and not checker:
            continue

        row_key = f"{release_key}::{rov_key or 'no-rov'}"
        assignments[row_key] = {
            "release_key": release_key,
            "rov_key": rov_key,
            "responsibles": responsibles,
            "checker": checker,
        }

    return assignments


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
            match = RELEASE_VERSION_PATTERN.search(str(value))
            if match:
                return match.group(0)
    else:
        match = RELEASE_VERSION_PATTERN.search(str(dist_item))
        if match:
            return match.group(0)

    return ""


def _iter_nested_values(value):
    if isinstance(value, dict):
        yield value
        for nested_value in value.values():
            yield from _iter_nested_values(nested_value)
    elif isinstance(value, list):
        for nested_item in value:
            yield from _iter_nested_values(nested_item)
    elif value is not None:
        yield value


def _extract_nested_version(dist_item):
    for value in _iter_nested_values(dist_item):
        if isinstance(value, dict):
            for key in ("version", "buildVersion"):
                raw_value = value.get(key)
                if raw_value and RELEASE_VERSION_PATTERN.search(str(raw_value)):
                    return str(raw_value)
        else:
            match = RELEASE_VERSION_PATTERN.search(str(value))
            if match:
                return match.group(0)
    return _extract_version(dist_item)


def _extract_dist_url(dist_item):
    if not dist_item:
        return ""

    candidate_values = []
    if isinstance(dist_item, dict):
        for key in ("url", "value", "downloadUrl", "artifactUrl", "link"):
            value = dist_item.get(key)
            if value:
                candidate_values.append(str(value))
    else:
        candidate_values.append(str(dist_item))

    for value in candidate_values:
        match = ARTIFACT_URL_PATTERN.search(value)
        if match:
            return _normalize_artifact_url(match.group(0).rstrip('",)'))
        if value.startswith("http://") or value.startswith("https://"):
            return value

    return ""


def _normalize_artifact_url(url_value):
    url_value = str(url_value or "").strip()
    if not url_value:
        return ""
    if url_value.startswith(("http://", "https://")):
        return url_value
    return f"https://{url_value}"


def _extract_nested_dist_url(dist_item):
    for value in _iter_nested_values(dist_item):
        if isinstance(value, dict):
            for key in ("url", "downloadUrl", "artifactUrl", "link", "value"):
                raw_value = value.get(key)
                if not raw_value:
                    continue
                match = ARTIFACT_URL_PATTERN.search(str(raw_value))
                if match:
                    return _normalize_artifact_url(match.group(0).rstrip('",)'))
                if str(raw_value).startswith(("http://", "https://")):
                    return str(raw_value)
        else:
            match = ARTIFACT_URL_PATTERN.search(str(value))
            if match:
                return _normalize_artifact_url(match.group(0).rstrip('",)'))
    return _extract_dist_url(dist_item)


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


def _dist_item_score(item):
    if not item:
        return 0
    score = 0
    if _extract_nested_version(item):
        score += 4
    if _extract_nested_dist_url(item):
        score += 3
    if isinstance(item, dict) and (item.get("id") or item.get("PARENT_CI") or item.get("smId")):
        score += 2
    return score


def _extract_release_dist(fields, resolved_fields):
    candidates = []
    for logical_name in ("release_distributive", "delta_release_distributive"):
        raw_dist = fields.get(resolved_fields[logical_name])
        if isinstance(raw_dist, list):
            candidates.extend(item for item in raw_dist if item)
        elif raw_dist:
            candidates.append(raw_dist)
    if candidates:
        return max(candidates, key=_dist_item_score)
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

        normalized_aliases = [_normalize_text(alias) for alias in aliases if alias]

        for field_id, field_name in field_name_map.items():
            normalized_name = _normalize_text(field_name)
            if normalized_name in normalized_aliases:
                resolved[logical_name] = field_id
                break
        else:
            for field_id, field_name in field_name_map.items():
                normalized_name = _normalize_text(field_name)
                if any(alias in normalized_name for alias in normalized_aliases):
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
        linked_issues = []
        if link.get("inwardIssue"):
            linked_issues.append(link.get("inwardIssue"))
        if link.get("outwardIssue"):
            linked_issues.append(link.get("outwardIssue"))

        link_name = str(link_type.get("name") or "")
        inward_name = str(link_type.get("inward") or "")
        outward_name = str(link_type.get("outward") or "")
        is_release_io = (
            link_name == "ReleaseIO"
            or "Introduction Order" in inward_name
            or "Introduction Order" in outward_name
        )
        if not is_release_io:
            continue

        for linked_issue in linked_issues:
            key = linked_issue.get("key")
            if key:
                keys.append(key)

    def _sort_key(issue_key):
        match = re.search(r"-(\d+)$", issue_key or "")
        return int(match.group(1)) if match else -1

    return sorted(list(dict.fromkeys(keys)), key=_sort_key)


def _extract_release_io_key(issue):
    keys = _extract_release_io_keys(issue)
    return keys[-1] if keys else ""


def _sort_rov_records_for_release(rov_records):
    def _record_sort_key(rov_record):
        if not isinstance(rov_record, dict):
            return (datetime.min, datetime.min, "")

        start_dt = rov_record.get("start_dt") or datetime.min
        end_dt = rov_record.get("end_dt") or datetime.min
        issue_key = str(rov_record.get("key") or "")
        return (start_dt, end_dt, issue_key)

    return sorted(
        [record for record in (rov_records or []) if isinstance(record, dict)],
        key=_record_sort_key,
    )


def _clean_release_summary(summary):
    cleaned = re.sub(r"^\s*Р РµР»РёР·#\d+\s*", "", summary or "", flags=re.IGNORECASE)
    return cleaned.strip()


def _detect_system(prefix, summary, ke_name, system_info_text):
    raw_searchable = str(f"{summary} {ke_name} {system_info_text}" or "").lower()
    searchable = _normalize_text(raw_searchable)

    aist_markers = ("\u0430\u0438\u0441\u0442", "aist", "Р°РёСЃС‚".lower(), "РђРРЎРў".lower())
    if any(marker in raw_searchable or marker in searchable for marker in aist_markers):
        return "\u0410\u0418\u0421\u0422"
    if "clm" in searchable or prefix in {"SMECLM", "SMECSC"}:
        return "CLM"
    return "\u0424\u043e\u043a\u0443\u0441"


def _build_release_name_lines(summary, release_ke_line, row_label="(\u0420\u0435\u043b\u0438\u0437)"):
    lines = []
    short_name = _clean_release_summary(summary)
    if short_name:
        lines.append(short_name)

    if release_ke_line:
        lines.append(release_ke_line)

    lines.append("(Релиз)")
    lines[-1] = row_label

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
    release_version = _extract_nested_version(dist_item)
    release_dist_url = _extract_nested_dist_url(dist_item)
    dist_ke_raw = ""
    if isinstance(dist_item, dict):
        dist_ke_raw = dist_item.get("id") or dist_item.get("smId") or dist_item.get("PARENT_CI") or ""
    ke_distributive = _format_ke_id(dist_ke_raw)

    normalized_status = _normalize_text(status_name)
    is_cancelled = normalized_status == _normalize_text(CANCELLED_RELEASE_STATUS)
    is_final = normalized_status == _normalize_text(FINAL_RELEASE_STATUS)
    is_ready_for_prom = normalized_status == _normalize_text(READY_FOR_PROM_STATUS)
    is_non_final = not is_final and not is_cancelled
    is_pre_final = normalized_status in {_normalize_text(status) for status in PRE_FINAL_RELEASE_STATUSES}
    ke_name = ke_object.get("name") or ""
    ke_id = ke_object.get("id") or ""
    release_ke_line = f"{ke_name}({ke_id})" if ke_name and ke_id else (ke_name or "")
    if not release_ke_line:
        release_ke_line = (system_info_text or "").strip()
    linked_rov_keys = _extract_release_io_keys(issue)
    linked_rov_records = _sort_rov_records_for_release(
        [rov_map.get(key, {}) for key in linked_rov_keys if rov_map.get(key)]
    )
    row_variants = linked_rov_records or [{}]
    records = []
    now_dt = datetime.now()
    today = now_dt.date()
    attempt_outcomes = _load_release_attempt_outcomes()
    has_successful_final_attempt_before = False

    for index, rov_data in enumerate(row_variants):
        rov_key = rov_data.get("key", "")
        rov_start = rov_data.get("start_dt")
        rov_end = rov_data.get("end_dt")

        release_year = _pick_release_year(rov_start, rov_end, planned_prom_start, planned_prom_end, created_dt)
        if release_year not in {current_year, previous_year}:
            continue

        rov_start_date = rov_start.date() if rov_start else None
        rov_end_date = rov_end.date() if rov_end else None
        row_key = f"{issue.get('key')}::{rov_key or 'no-rov'}"
        is_deferred_attempt = row_key in attempt_outcomes
        is_reroll = bool(
            is_final
            and rov_key
            and len(linked_rov_records) > 1
            and index > 0
            and has_successful_final_attempt_before
            and not is_deferred_attempt
        )
        row_is_cancelled = is_cancelled
        row_is_final = is_final
        row_is_non_final = not row_is_final and not row_is_cancelled
        row_is_pre_final = is_pre_final and not row_is_final

        is_overdue = bool(row_is_non_final and _is_release_window_expired(rov_end, now_dt))
        is_today = bool(
            row_is_non_final
            and (
                (rov_start_date and rov_start_date == today)
                or (rov_end_date and rov_end_date == today)
            )
        )

        if row_is_cancelled:
            row_state = "cancelled"
        elif row_is_final:
            row_state = "final"
        elif is_overdue:
            row_state = "overdue"
        elif is_today:
            row_state = "today"
        else:
            row_state = "planned"

        days_overdue = _release_days_overdue(rov_end, now_dt)
        is_hotfix = str(release_version or "").upper().startswith("P-")
        if is_reroll:
            row_label = "(\u041f\u0435\u0440\u0435\u0440\u0430\u0441\u043a\u0430\u0442\u043a\u0430)"
        elif is_hotfix:
            row_label = "(\u0425\u043e\u0442\u0444\u0438\u043a\u0441)"
        else:
            row_label = "(\u0420\u0435\u043b\u0438\u0437)"

        is_unnumbered = (not rov_key) or (row_is_cancelled and not ke_distributive)

        records.append({
            "row_key": row_key,
            "year": release_year,
            "release_number": "",
            "release_key": issue.get("key"),
            "release_url": f"{domain}/browse/{issue.get('key')}",
            "release_status": status_name,
            "release_status_normalized": normalized_status,
            "release_summary": summary,
            "release_name_lines": _build_release_name_lines(summary, release_ke_line, row_label=row_label),
            "base_release_summary": summary,
            "base_release_name_lines": _build_release_name_lines(summary, release_ke_line, row_label=row_label),
            "is_reroll": is_reroll,
            "is_deferred_attempt": is_deferred_attempt,
            "row_label": row_label,
            "zni_key": "",
            "zni_url": "",
            "base_zni_key": "",
            "base_zni_url": "",
            "has_rollout_notes": False,
            "rollout_notes_level": "",
            "ke": ke_distributive,
            "base_ke": ke_distributive,
            "ke_name": ke_name,
            "ke_id": ke_id,
            "release_version": release_version,
            "release_dist_url": release_dist_url,
            "base_release_version": release_version,
            "base_release_dist_url": release_dist_url,
            "rov_key": rov_key,
            "rov_url": rov_data.get("url", ""),
            "rov_status": rov_data.get("status", ""),
            "has_rov": bool(rov_key),
            "deployment_start": rov_data.get("start", ""),
            "deployment_start_iso": rov_data.get("start_iso", ""),
            "deployment_end": rov_data.get("end", ""),
            "deployment_end_iso": rov_data.get("end_iso", ""),
            "source_deployment_start": rov_data.get("start", ""),
            "source_deployment_start_iso": rov_data.get("start_iso", ""),
            "source_deployment_end": rov_data.get("end", ""),
            "source_deployment_end_iso": rov_data.get("end_iso", ""),
            "psi_owner": "",
            "psi_responsibles": [],
            "psi_checker": "",
            "row_state": row_state,
            "is_final": row_is_final,
            "is_cancelled": row_is_cancelled,
            "is_non_final": row_is_non_final,
            "is_pre_final": row_is_pre_final,
            "is_ready_for_prom": is_ready_for_prom and not row_is_final,
            "is_overdue": is_overdue,
            "is_today": is_today,
            "days_overdue": days_overdue,
            "waits_for_rov": not rov_key and not is_cancelled,
            "is_unnumbered": is_unnumbered,
            "is_natural_unnumbered": is_unnumbered,
            "is_force_unnumbered": False,
            "source_prefix": prefix,
            "system_name": _detect_system(prefix, summary, ke_name, system_info_text),
            "sort_date": _pick_release_sort_dt(rov_start, rov_end, planned_prom_start, planned_prom_end).isoformat() if _pick_release_sort_dt(rov_start, rov_end, planned_prom_start, planned_prom_end) else "",
            "created_sort_date": created_dt.isoformat() if created_dt else "",
            "created": fields.get("created", ""),
        })

        if is_final and rov_key and not is_deferred_attempt:
            has_successful_final_attempt_before = True

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


def _apply_group_manual_order(year, group_name, items):
    if not items:
        return items

    manual_order = _load_manual_order()
    year_payload = manual_order.get(str(year), {}) if isinstance(manual_order, dict) else {}
    group_payload = year_payload.get(group_name, []) if isinstance(year_payload, dict) else []

    item_by_key = {
        str(item.get("row_key") or "").strip(): item
        for item in items
        if str(item.get("row_key") or "").strip()
    }

    merged_items = list(items)

    if isinstance(group_payload, dict):
        bucket_orders = {
            str(bucket or "").strip(): [
                str(row_key or "").strip()
                for row_key in (row_keys or [])
                if str(row_key or "").strip()
            ]
            for bucket, row_keys in group_payload.items()
            if str(bucket or "").strip() and isinstance(row_keys, list)
        }
    else:
        legacy_order = [
            str(value or "").strip()
            for value in (group_payload or [])
            if str(value or "").strip()
        ]
        bucket_orders = defaultdict(list)
        for row_key in legacy_order:
            item = item_by_key.get(row_key)
            if item:
                bucket_orders[_get_release_order_bucket(item)].append(row_key)

    for bucket, ordered_row_keys in bucket_orders.items():
        present_manual_keys = [row_key for row_key in ordered_row_keys if row_key in item_by_key]
        if not present_manual_keys:
            continue

        manual_key_set = set(present_manual_keys)
        manual_slot_indexes = [
            index
            for index, item in enumerate(merged_items)
            if _get_release_order_bucket(item) == bucket
            and str(item.get("row_key") or "").strip() in manual_key_set
        ]
        if not manual_slot_indexes:
            continue

        ordered_manual_items = [item_by_key[row_key] for row_key in present_manual_keys]
        for slot_index, manual_item in zip(manual_slot_indexes, ordered_manual_items):
            merged_items[slot_index] = manual_item
    return merged_items


def _derive_natural_unnumbered(item):
    if not isinstance(item, dict):
        return False
    has_rov = bool(item.get("has_rov") or str(item.get("rov_key") or "").strip())
    is_cancelled = bool(item.get("is_cancelled"))
    has_ke = bool(str(item.get("ke") or "").strip())
    has_manual_start = bool(str(item.get("manual_deployment_start") or "").strip())
    can_number_without_rov = bool(
        (not has_rov)
        and item.get("is_non_final")
        and has_manual_start
    )
    if can_number_without_rov:
        return bool(is_cancelled and not has_ke)
    return (not has_rov) or (is_cancelled and not has_ke)


def _apply_force_unnumbered_flags(year, items):
    manual_order = _load_manual_order()
    year_payload = manual_order.get(str(year), {}) if isinstance(manual_order, dict) else {}
    forced_keys = {
        str(row_key or "").strip()
        for row_key in (year_payload.get("force_unnumbered") or [])
        if str(row_key or "").strip()
    }

    for item in items:
        row_key = str(item.get("row_key") or item.get("release_key") or "").strip()
        has_manual_start = bool(str(item.get("manual_deployment_start") or "").strip())
        manual_numbering_override = bool(
            has_manual_start
            and item.get("is_non_final")
            and not item.get("has_rov")
        )
        is_natural_unnumbered = _derive_natural_unnumbered(item)
        is_forced = row_key in forced_keys
        item["is_natural_unnumbered"] = is_natural_unnumbered
        item["is_force_unnumbered"] = is_forced
        item["is_manual_numbering_override"] = manual_numbering_override
        item["is_unnumbered"] = bool((is_natural_unnumbered or is_forced) and not manual_numbering_override)


def _sort_and_number_records(records):
    records_by_year = defaultdict(list)
    for item in records:
        records_by_year[item["year"]].append(item)

    for year_items in records_by_year.values():
        _apply_force_unnumbered_flags(year_items[0]["year"], year_items)
        numbered_items = [item for item in year_items if not item.get("is_unnumbered")]
        waiting_items = [item for item in year_items if item.get("is_unnumbered")]

        numbered_items.sort(
            key=lambda item: (
                _sort_datetime_value(item, "sort_date"),
                _sort_datetime_value(item, "created_sort_date"),
                item.get("release_key", ""),
            ),
            reverse=True,
        )

        waiting_items.sort(
            key=lambda item: (
                _sort_datetime_value(item, "sort_date"),
                _sort_datetime_value(item, "created_sort_date"),
                item.get("release_key", ""),
            ),
            reverse=True,
        )

        waiting_items = _apply_group_manual_order(year_items[0]["year"], "waiting", waiting_items)
        numbered_items = _apply_group_manual_order(year_items[0]["year"], "numbered", numbered_items)

        total_numbered = len(numbered_items)
        for index, item in enumerate(numbered_items):
            item["release_number"] = total_numbered - index
            item["_group_rank"] = index

        for index, item in enumerate(waiting_items):
            item["release_number"] = ""
            item["_group_rank"] = index

    records.sort(
        key=lambda item: (
            -item.get("year", 0),
            0 if item.get("is_unnumbered") else 1,
            item.get("_group_rank", 0),
        )
    )
    return records


def save_release_monitor_manual_order(year, waiting_row_keys=None, numbered_row_keys=None, force_unnumbered_row_keys=None):
    global _cached_data, _last_cache_update

    year = int(year or datetime.now().year)

    def _normalize_order_payload(value):
        if isinstance(value, dict):
            normalized = {}
            for bucket, row_keys in value.items():
                bucket_key = str(bucket or "").strip()
                if not bucket_key:
                    continue
                normalized_keys = []
                for row_key in (row_keys or []):
                    row_key = str(row_key or "").strip()
                    if row_key and row_key not in normalized_keys:
                        normalized_keys.append(row_key)
                if normalized_keys:
                    normalized[bucket_key] = normalized_keys
            return normalized

        normalized_keys = []
        for row_key in (value or []):
            row_key = str(row_key or "").strip()
            if row_key and row_key not in normalized_keys:
                normalized_keys.append(row_key)
        return normalized_keys

    normalized_waiting = _normalize_order_payload(waiting_row_keys)
    normalized_numbered = _normalize_order_payload(numbered_row_keys)
    normalized_force_unnumbered = []

    for row_key in (force_unnumbered_row_keys or []):
        row_key = str(row_key or "").strip()
        if row_key and row_key not in normalized_force_unnumbered:
            normalized_force_unnumbered.append(row_key)

    with _cache_lock:
        manual_order = _load_manual_order()
        manual_order[str(year)] = {
            "waiting": normalized_waiting,
            "numbered": normalized_numbered,
            "force_unnumbered": normalized_force_unnumbered,
        }
        _save_manual_order(manual_order)

        if _cached_data is None:
            disk_payload = _load_snapshot_from_disk()
            if disk_payload:
                _cached_data = _hydrate_release_monitor_payload(disk_payload)
                _last_cache_update = _get_snapshot_mtime() or time.time()

        if _cached_data is not None:
            items = list(_cached_data.get("items", []))
            _apply_reviewer_assignments(items)
            _apply_date_overrides(items)
            _apply_duty_schedule_assignments(items, persist=True)
            _sort_and_number_records(items)
            _cached_data["items"] = items
            _save_snapshot_to_disk(_cached_data)
            disk_payload = _load_snapshot_from_disk()
            if disk_payload is not None:
                _cached_data = _hydrate_release_monitor_payload(disk_payload)
                _last_cache_update = _get_snapshot_mtime() or time.time()

        payload = _get_cached_payload_copy() or _build_empty_release_monitor_payload()
        if not payload.get("items"):
            disk_payload = _load_snapshot_from_disk()
            if disk_payload and disk_payload.get("items"):
                _cached_data = _hydrate_release_monitor_payload(disk_payload)
                _last_cache_update = _get_snapshot_mtime() or time.time()
                payload = _get_cached_payload_copy() or disk_payload
        return {
            "year": year,
            "data": payload,
        }


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
        summary["by_status"][item["release_status"] or "РќРµ СѓРєР°Р·Р°РЅ"] += 1
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
    _apply_date_overrides(all_records)
    _apply_duty_schedule_assignments(all_records, persist=True)
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
            "last_confluence_sync": None,
            "last_duty_schedule_upload": None,
            "quick_refresh_days": QUICK_REFRESH_DAYS,
            "auto_full_refresh_hour": AUTO_FULL_REFRESH_HOUR,
            "data_revision": _read_data_revision(),
            "is_cached": True,
        },
    }
    payload["meta"] = _append_duty_schedule_meta(payload["meta"])
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

        manual_overrides = _load_manual_release_overrides()
        _cached_data = _finalize_release_monitor_payload(_fetch_release_monitor_data(), manual_overrides)
        _last_cache_update = now
        _save_snapshot_to_disk(_cached_data)
        return _cached_data


def _run_release_monitor_refresh():
    global _cached_data, _last_cache_update

    try:
        logging.info("Release monitor: background refresh started")
        manual_overrides = _load_manual_release_overrides()
        data = _fetch_release_monitor_data()
        with _cache_lock:
            _cached_data = _finalize_release_monitor_payload(data, manual_overrides)
            _last_cache_update = time.time()
            _save_snapshot_to_disk(_cached_data)
            _refresh_status.update(
                {
                    "state": "completed",
                    "message": "Р”Р°РЅРЅС‹Рµ РїРѕ СЂРµР»РёР·Р°Рј РѕР±РЅРѕРІР»РµРЅС‹",
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
                    "message": "РћС€РёР±РєР° РѕР±РЅРѕРІР»РµРЅРёСЏ СЂРµР»РёР·РѕРІ",
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
                "message": "РРґРµС‚ РѕР±РЅРѕРІР»РµРЅРёРµ СЂРµР»РёР·РѕРІ РёР· Jira",
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
    manual_overrides = _load_manual_release_overrides()
    return {
        "items": list(_cached_data.get("items", [])),
        "manual_overrides": dict(manual_overrides),
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
                _cached_data = _hydrate_release_monitor_payload(disk_payload)
                _last_cache_update = time.time()

        now = time.time()
        if (
            not force_refresh
            and _cached_data is not None
            and _last_cache_update is not None
            and (now - _last_cache_update) < DASHBOARD_CACHE_TTL
        ):
            return _cached_data

    manual_overrides = _load_manual_release_overrides()
    data = _fetch_release_monitor_data()
    data["meta"]["last_full_sync"] = data["meta"].get("last_updated")
    data["meta"]["last_quick_sync"] = (_cached_data or {}).get("meta", {}).get("last_quick_sync")
    data["meta"]["last_confluence_sync"] = (_cached_data or {}).get("meta", {}).get("last_confluence_sync")

    with _cache_lock:
        _cached_data = _finalize_release_monitor_payload(data, manual_overrides)
        _last_cache_update = time.time()
        _save_snapshot_to_disk(_cached_data)
        return _cached_data


def _run_release_monitor_refresh(mode="full", trigger="manual"):
    global _cached_data, _last_cache_update

    try:
        logging.info("Release monitor: background %s refresh started", mode)
        manual_overrides = _load_manual_release_overrides()
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
        meta["last_confluence_sync"] = current_meta.get("last_confluence_sync")
        data["meta"] = meta
        data = _finalize_release_monitor_payload(data, manual_overrides)

        with _cache_lock:
            _cached_data = data
            _last_cache_update = time.time()
            _save_snapshot_to_disk(_cached_data)
            _refresh_status.update(
                {
                    "state": "completed",
                    "message": "Р”Р°РЅРЅС‹Рµ РїРѕ СЂРµР»РёР·Р°Рј РѕР±РЅРѕРІР»РµРЅС‹",
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
                    "message": "РћС€РёР±РєР° РѕР±РЅРѕРІР»РµРЅРёСЏ СЂРµР»РёР·РѕРІ",
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
                "message": "РРґРµС‚ РїРѕР»РЅРѕРµ РѕР±РЅРѕРІР»РµРЅРёРµ СЂРµР»РёР·РѕРІ РёР· Jira" if mode == "full" else f"РРґРµС‚ Р±С‹СЃС‚СЂРѕРµ РѕР±РЅРѕРІР»РµРЅРёРµ СЂРµР»РёР·РѕРІ Р·Р° РїРѕСЃР»РµРґРЅРёРµ {QUICK_REFRESH_DAYS} РґРЅРµР№",
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


def _normalize_release_payload(payload):
    if not isinstance(payload, dict):
        return _build_empty_release_monitor_payload()

    normalized_items = [dict(item) for item in (payload.get("items") or []) if isinstance(item, dict)]
    _apply_reviewer_assignments(normalized_items)
    _apply_release_status_consistency(normalized_items)
    _apply_date_overrides(normalized_items)
    _apply_release_attempt_outcomes(normalized_items)
    _apply_duty_schedule_assignments(normalized_items, persist=True)
    _apply_zni_assignments(normalized_items)
    _apply_manual_release_overrides(normalized_items, payload.get("manual_overrides") or {})
    _apply_week_control_flags(normalized_items)
    _sort_and_number_records(normalized_items)
    current_year = datetime.now().year
    previous_year = current_year - 1

    return {
        "items": normalized_items,
        "manual_overrides": _normalize_manual_release_overrides(payload.get("manual_overrides") or {}),
        "summary": _build_summary(normalized_items, current_year, previous_year),
        "meta": _append_revision_meta(_append_duty_schedule_meta(dict(payload.get("meta", {})))),
    }


def _finalize_release_monitor_payload(payload, manual_overrides=None):
    payload = dict(payload or {})
    normalized_manual_overrides = _normalize_manual_release_overrides(
        manual_overrides if manual_overrides is not None else payload.get("manual_overrides") or {}
    )
    payload["manual_overrides"] = dict(normalized_manual_overrides)
    finalized_payload = _normalize_release_payload(payload)
    finalized_payload["manual_overrides"] = dict(normalized_manual_overrides)
    return finalized_payload


def _hydrate_release_monitor_payload(payload):
    if payload is None:
        return None
    return _finalize_release_monitor_payload(payload, _load_manual_release_overrides())


def get_release_monitor_refresh_status():
    global _cached_data

    with _cache_lock:
        _ensure_scheduler_started()
        disk_payload = _load_snapshot_from_disk()
        if disk_payload is not None:
            _cached_data = _hydrate_release_monitor_payload(disk_payload)
            _last_cache_update = _get_snapshot_mtime() or time.time()

        payload = {
            "status": dict(_refresh_status),
        }
        payload["data"] = _normalize_release_payload(_get_cached_payload_copy() or _build_empty_release_monitor_payload())
        return payload


def get_release_monitor_snapshot():
    global _cached_data, _last_cache_update

    with _cache_lock:
        _ensure_scheduler_started()
        disk_payload = _load_snapshot_from_disk()
        if disk_payload is not None:
            _cached_data = _hydrate_release_monitor_payload(disk_payload)
            _last_cache_update = _get_snapshot_mtime() or time.time()
        if _cached_data is None:
            return _build_empty_release_monitor_payload()

        payload = _normalize_release_payload(_get_cached_payload_copy())
        payload["meta"] = {
            **payload.get("meta", {}),
            "is_cached": True,
        }
        return payload


def get_release_monitor_reviewer_options():
    return list(OPLOT_VALUES)


def get_release_monitor_week_control():
    snapshot = get_release_monitor_snapshot() or {}
    items = snapshot.get("items", []) if isinstance(snapshot, dict) else []
    week_start, week_end = _get_current_week_bounds()
    candidate_groups = _collect_week_candidate_availability(week_start, week_end)

    week_items = [
        item for item in items
        if _is_release_assignment_relevant_for_week(item, week_start, week_end)
    ]
    missing_responsible = [
        {
            "row_key": _get_assignment_key_for_item(item),
            "release_key": item.get("release_key", ""),
            "rov_key": item.get("rov_key", ""),
            "release_summary": item.get("release_summary", ""),
            "system_name": item.get("system_name", ""),
            "release_status": item.get("release_status", ""),
            "deployment_start": item.get("deployment_start", ""),
            "deployment_end": item.get("deployment_end", ""),
            "release_url": item.get("release_url", ""),
            "rov_url": item.get("rov_url", ""),
        }
        for item in week_items
        if not _has_release_responsible(item)
    ]

    assigned_load = defaultdict(int)
    for item in week_items:
        responsibles = item.get("psi_responsibles") or []
        if not isinstance(responsibles, list):
            responsibles = [responsibles] if responsibles else []
        for responsible in responsibles:
            responsible_name = str(responsible or "").strip()
            if responsible_name:
                assigned_load[responsible_name] += 1

    available_count = max(1, len(candidate_groups.get("available") or []))
    reserve_allowed = bool(missing_responsible and (len(missing_responsible) / available_count) > 5)

    return {
        "period": {
            "start": week_start.isoformat(),
            "end": week_end.isoformat(),
            "label": f"{week_start.strftime('%d.%m.%Y')} - {week_end.strftime('%d.%m.%Y')}",
        },
        "statistics": {
            "week_releases": len(week_items),
            "missing_responsible": len(missing_responsible),
            "available_candidates": len(candidate_groups.get("available") or []),
            "reserve_candidates": len(candidate_groups.get("reserve") or []),
            "excluded_candidates": len(candidate_groups.get("excluded") or []),
            "reserve_allowed": reserve_allowed,
        },
        "missing_responsible": missing_responsible,
        "candidates": candidate_groups,
        "assigned_load": dict(sorted(assigned_load.items(), key=lambda pair: (-pair[1], pair[0]))),
    }


def _extract_json_object(text_value):
    text_value = str(text_value or "").strip()
    if not text_value:
        return {}
    try:
        parsed = json.loads(text_value)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        pass

    match = re.search(r"\{.*\}", text_value, re.DOTALL)
    if not match:
        return {}
    try:
        parsed = json.loads(match.group(0))
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _build_responsible_history(items, candidate_names):
    candidate_set = set(candidate_names)
    history = {
        candidate: {
            "total": 0,
            "by_system": defaultdict(int),
            "by_prefix": defaultdict(int),
        }
        for candidate in candidate_names
    }

    for item in items:
        responsibles = item.get("psi_responsibles") or []
        if not isinstance(responsibles, list):
            responsibles = [responsibles] if responsibles else []

        prefix = str(item.get("release_key") or "").split("-", 1)[0]
        system_name = str(item.get("system_name") or "").strip() or prefix or "unknown"
        for responsible in responsibles:
            responsible_name = str(responsible or "").strip()
            if responsible_name not in candidate_set:
                continue
            history[responsible_name]["total"] += 1
            history[responsible_name]["by_system"][system_name] += 1
            history[responsible_name]["by_prefix"][prefix or "unknown"] += 1

    return {
        candidate: {
            "total": values["total"],
            "by_system": dict(sorted(values["by_system"].items(), key=lambda pair: (-pair[1], pair[0]))[:8]),
            "by_prefix": dict(sorted(values["by_prefix"].items(), key=lambda pair: (-pair[1], pair[0]))[:8]),
        }
        for candidate, values in history.items()
    }


def _normalize_giga_recommendation_name(value, allowed_names):
    value = str(value or "").strip()
    if not value:
        return ""
    if value in allowed_names:
        return value
    matched = _match_oplot_name(value)
    return matched if matched in allowed_names else ""


def get_release_monitor_week_responsible_recommendations():
    snapshot = get_release_monitor_snapshot() or {}
    items = snapshot.get("items", []) if isinstance(snapshot, dict) else []
    control = get_release_monitor_week_control()
    missing = control.get("missing_responsible") or []
    week_start, week_end = _get_current_week_bounds()
    week_items = [
        {
            "row_key": _get_assignment_key_for_item(item),
            "release_key": item.get("release_key", ""),
            "rov_key": item.get("rov_key", ""),
            "release_summary": item.get("release_summary", ""),
            "system_name": item.get("system_name", ""),
            "release_status": item.get("release_status", ""),
            "deployment_start": item.get("deployment_start", ""),
            "deployment_end": item.get("deployment_end", ""),
            "responsibles": item.get("psi_responsibles", []),
            "row_state": item.get("row_state", ""),
            "is_overdue": bool(item.get("is_overdue")),
        }
        for item in items
        if _is_release_assignment_relevant_for_week(item, week_start, week_end)
    ]

    available_candidates = [item.get("name") for item in control.get("candidates", {}).get("available", []) if item.get("name")]
    reserve_candidates = [item.get("name") for item in control.get("candidates", {}).get("reserve", []) if item.get("name")]
    allowed_candidates = list(available_candidates)
    if control.get("statistics", {}).get("reserve_allowed"):
        allowed_candidates.extend(name for name in reserve_candidates if name not in allowed_candidates)

    if missing and not allowed_candidates:
        return {
            "control": control,
            "recommendations": [],
            "source": "rules",
            "message": "Нет доступных кандидатов по графику дежурств.",
        }

    history = _build_responsible_history(items, allowed_candidates)
    recommendation_context = {
        "period": control.get("period", {}),
        "rules": {
            "allowed_candidates": allowed_candidates,
            "reserve_candidates": reserve_candidates,
            "reserve_allowed": bool(control.get("statistics", {}).get("reserve_allowed")),
            "excluded_candidates": control.get("candidates", {}).get("excluded", []),
            "do_not_assign_checkers": True,
        },
        "current_week_load": control.get("assigned_load", {}),
        "history": history,
        "current_week_releases": week_items,
        "releases_without_responsible": missing,
    }

    try:
        from services.gigachat_service import GIGA_HELPER
    except Exception as exc:
        logging.warning("Release week control: failed to import GigaChat helper: %s", exc)
        return {
            "control": control,
            "recommendations": [],
            "source": "unavailable",
            "message": f"GigaChat недоступен: {exc}",
        }

    if not getattr(GIGA_HELPER, "client", None):
        return {
            "control": control,
            "recommendations": [],
            "source": "unavailable",
            "message": "GigaChat недоступен или не инициализирован.",
        }

    prompt = f"""
Ты помогаешь планировать ответственных за ПСИ по релизам.

Сначала сформируй краткую операционную сводку по current_week_releases.
Если releases_without_responsible пустой, recommendations верни пустым массивом и в summary напиши, что по ответственным критичных пробелов нет.
Если releases_without_responsible не пустой, предложи ответственного только для релизов из этого списка.
Используй только ФИО из rules.allowed_candidates. Нельзя предлагать ФИО из excluded_candidates.
reserve_candidates можно использовать только если rules.reserve_allowed=true.
Проверяющих не назначай и не анализируй.

Учитывай:
- историю назначений по похожим системам, prefix и category/system_name;
- текущую недельную нагрузку current_week_load;
- равномерность распределения;
- причины исключения из графика.

Верни строго JSON без markdown:
{{
  "recommendations": [
    {{
      "row_key": "...",
      "release_key": "...",
      "recommended": "ФИО из allowed_candidates",
      "backup": "ФИО из allowed_candidates или пусто",
      "confidence": "high|medium|low",
      "reason": "коротко почему выбран кандидат"
    }}
  ],
  "summary": "краткая сводка по неделе и назначениям"
}}

Данные:
{json.dumps(recommendation_context, ensure_ascii=False, indent=2)}
"""

    try:
        response = GIGA_HELPER.client.chat(prompt)
        content = response.choices[0].message.content
        parsed = _extract_json_object(content)
    except Exception as exc:
        logging.warning("Release week control: GigaChat recommendation failed: %s", exc)
        return {
            "control": control,
            "recommendations": [],
            "source": "error",
            "message": f"Ошибка GigaChat: {exc}",
        }

    raw_recommendations = parsed.get("recommendations") if isinstance(parsed, dict) else []
    if not isinstance(raw_recommendations, list):
        raw_recommendations = []

    allowed_set = set(allowed_candidates)
    missing_by_row_key = {item.get("row_key"): item for item in missing if item.get("row_key")}
    normalized_recommendations = []
    for raw_item in raw_recommendations:
        if not isinstance(raw_item, dict):
            continue
        row_key = str(raw_item.get("row_key") or "").strip()
        if row_key not in missing_by_row_key:
            continue
        recommended = _normalize_giga_recommendation_name(raw_item.get("recommended"), allowed_set)
        if not recommended:
            continue
        backup = _normalize_giga_recommendation_name(raw_item.get("backup"), allowed_set)
        confidence = str(raw_item.get("confidence") or "medium").strip().lower()
        if confidence not in {"high", "medium", "low"}:
            confidence = "medium"
        source_item = missing_by_row_key[row_key]
        normalized_recommendations.append({
            "row_key": row_key,
            "release_key": source_item.get("release_key", ""),
            "rov_key": source_item.get("rov_key", ""),
            "recommended": recommended,
            "backup": backup,
            "confidence": confidence,
            "reason": str(raw_item.get("reason") or "").strip(),
        })

    return {
        "control": control,
        "recommendations": normalized_recommendations,
        "source": "gigachat",
        "summary": str(parsed.get("summary") or "").strip() if isinstance(parsed, dict) else "",
        "message": "" if normalized_recommendations else "GigaChat не вернул применимых рекомендаций.",
    }


def upload_release_monitor_duty_schedules(uploaded_files):
    global _cached_data, _last_cache_update

    files = [file_storage for file_storage in (uploaded_files or []) if getattr(file_storage, "filename", "")]
    if not files:
        raise ValueError("\u041d\u0435 \u0432\u044b\u0431\u0440\u0430\u043d\u044b \u0444\u0430\u0439\u043b\u044b \u0433\u0440\u0430\u0444\u0438\u043a\u0430")

    existing_payload = _load_duty_schedule_payload()
    merged_payload = dict(existing_payload)
    uploaded_names = []
    parsed_months = []
    warnings = []

    for file_storage in files:
        filename = str(getattr(file_storage, "filename", "") or "").strip()
        if not filename:
            continue

        file_bytes = file_storage.read()
        if not file_bytes:
            continue

        parsed_payload = _parse_duty_schedule_workbook(file_bytes, filename)
        parsed_payload["last_upload"] = _format_timestamp()
        merged_payload = _merge_duty_schedule_payload(merged_payload, parsed_payload)
        uploaded_names.append(filename)
        parsed_months.extend(parsed_payload.get("months", []))
        warnings.extend(parsed_payload.get("warnings", []))

    if not uploaded_names:
        raise ValueError("\u041d\u0435 \u0443\u0434\u0430\u043b\u043e\u0441\u044c \u043f\u0440\u043e\u0447\u0438\u0442\u0430\u0442\u044c \u043d\u0438 \u043e\u0434\u0438\u043d \u0444\u0430\u0439\u043b \u0433\u0440\u0430\u0444\u0438\u043a\u0430")

    merged_payload["last_upload"] = _format_timestamp()
    _save_duty_schedule_payload(merged_payload)

    with _cache_lock:
        _ensure_scheduler_started()
        if _cached_data is None:
            disk_payload = _load_snapshot_from_disk()
            if disk_payload is not None:
                _cached_data = _hydrate_release_monitor_payload(disk_payload)
                _last_cache_update = _get_snapshot_mtime() or time.time()

        applied_count = 0
        duty_debug_rows = []
        if _cached_data is not None:
            items = _cached_data.get("items") or []
            _apply_reviewer_assignments(items)
            _apply_date_overrides(items)
            duty_result = _apply_duty_schedule_assignments(items, persist=True, force=True, debug_limit=20)
            applied_count = duty_result.get("applied_count", 0) if isinstance(duty_result, dict) else int(duty_result or 0)
            duty_debug_rows = duty_result.get("debug_rows", []) if isinstance(duty_result, dict) else []
            _sort_and_number_records(items)
            meta = _cached_data.setdefault("meta", {})
            meta["last_duty_schedule_upload"] = merged_payload.get("last_upload")
            _save_snapshot_to_disk(_cached_data)

        payload = _normalize_release_payload(_get_cached_payload_copy() or _build_empty_release_monitor_payload())

    return {
        "uploaded_files": uploaded_names,
        "parsed_months": list(dict.fromkeys(parsed_months)),
        "warnings": warnings,
        "applied_count": applied_count,
        "duty_debug_rows": duty_debug_rows,
        "data": payload,
    }


def create_release_monitor_zni(release_key, reporter=""):
    global _cached_data, _last_cache_update

    release_key = str(release_key or "").strip()
    reporter = str(reporter or "").strip()
    if not release_key:
        raise ValueError("Не указан ключ строки релиза")

    with _cache_lock:
        _ensure_scheduler_started()
        if _cached_data is None:
            disk_payload = _load_snapshot_from_disk()
            if disk_payload is not None:
                _cached_data = _hydrate_release_monitor_payload(disk_payload)
                _last_cache_update = _get_snapshot_mtime() or time.time()

        if _cached_data is None:
            raise ValueError("Таблица релизов еще не загружена")

        items = _cached_data.get("items") or []
        _apply_reviewer_assignments(items)
        _apply_date_overrides(items)
        _apply_duty_schedule_assignments(items, persist=True)
        _apply_zni_assignments(items)
        target_item = None
        for item in items:
            if _get_assignment_key_for_item(item) == release_key:
                target_item = item
                break

        if not target_item:
            raise ValueError("Не удалось найти строку релиза")
        if target_item.get("zni_key"):
            return {
                "issue": {
                    "key": target_item.get("zni_key"),
                    "url": target_item.get("zni_url", ""),
                    "already_exists": True,
                },
                "data": _normalize_release_payload(_get_cached_payload_copy() or _build_empty_release_monitor_payload()),
            }

    issue = create_oplot_release_issue(target_item, reporter_name=reporter)

    with _cache_lock:
        assignments = _load_zni_assignments()
        assignments[release_key] = {
            "key": issue.get("key", ""),
            "url": issue.get("url", ""),
            "summary": issue.get("summary", ""),
            "created_at": _format_timestamp(),
            "assignee": issue.get("assignee", ""),
            "reporter": issue.get("reporter", ""),
        }
        _save_zni_assignments(assignments)

        if _cached_data is not None:
            items = _cached_data.get("items") or []
            for item in items:
                if _get_assignment_key_for_item(item) == release_key:
                    item["zni_key"] = issue.get("key", "")
                    item["zni_url"] = issue.get("url", "")
                    break
            _save_snapshot_to_disk(_cached_data)

        payload = _normalize_release_payload(_get_cached_payload_copy() or _build_empty_release_monitor_payload())

    return {
        "issue": issue,
        "data": payload,
    }


def set_release_monitor_rollout_notes(release_key, enabled=False, level=""):
    global _cached_data

    release_key = str(release_key or "").strip()
    if not release_key:
        raise ValueError("Не указан ключ строки релиза")

    enabled = bool(enabled)
    level = str(level or "").strip().lower()
    if enabled and level not in {"success", "warning", "danger"}:
        level = "warning"
    if not enabled:
        level = ""

    with _cache_lock:
        flags = _load_rollout_note_flags()
        if enabled:
            flags[release_key] = {
                "has_rollout_notes": True,
                "rollout_notes_level": level,
                "updated_at": _format_timestamp(),
            }
        else:
            flags.pop(release_key, None)
        _save_rollout_note_flags(flags)

        if _cached_data is None:
            disk_payload = _load_snapshot_from_disk()
            if disk_payload is not None:
                _cached_data = _hydrate_release_monitor_payload(disk_payload)

        if _cached_data is not None:
            for item in _cached_data.get("items") or []:
                if _get_assignment_key_for_item(item) == release_key:
                    if enabled and level == _get_release_auto_color_level(item):
                        enabled = False
                        level = ""
                        flags.pop(release_key, None)
                        _save_rollout_note_flags(flags)
                    item["has_rollout_notes"] = enabled
                    item["rollout_notes_level"] = level
                    break
            _save_snapshot_to_disk(_cached_data)
        else:
            _touch_release_monitor_revision()

        payload = _normalize_release_payload(_get_cached_payload_copy() or _build_empty_release_monitor_payload())

    return {
        "release_key": release_key,
        "has_rollout_notes": enabled,
        "rollout_notes_level": level,
        "data": payload,
    }


def set_release_monitor_date_override(release_key, start_value="", end_value="", reset=False):
    global _cached_data, _last_cache_update

    release_key = str(release_key or "").strip()
    if not release_key:
        raise ValueError("Не указан ключ строки релиза")

    overrides = _load_date_overrides()
    if reset:
        overrides.pop(release_key, None)
    else:
        normalized_start = _format_release_monitor_date(_parse_release_monitor_date(start_value)) if start_value else ""
        normalized_end = _format_release_monitor_date(_parse_release_monitor_date(end_value)) if end_value else ""
        if not normalized_start and not normalized_end:
            overrides.pop(release_key, None)
        else:
            overrides[release_key] = {
                "start": normalized_start,
                "end": normalized_end,
                "updated_at": _format_timestamp(),
            }

    _save_date_overrides(overrides)
    _remove_row_from_manual_order(release_key)

    with _cache_lock:
        if _cached_data is None:
            disk_payload = _load_snapshot_from_disk()
            if disk_payload:
                _cached_data = _hydrate_release_monitor_payload(disk_payload)
                _last_cache_update = _get_snapshot_mtime() or time.time()

        if _cached_data is not None:
            items = _cached_data.get("items") or []
            _apply_reviewer_assignments(items)
            _apply_date_overrides(items)
            _apply_duty_schedule_assignments(items, persist=True)
            _sort_and_number_records(items)
            _save_snapshot_to_disk(_cached_data)

        payload = _normalize_release_payload(_get_cached_payload_copy() or _build_empty_release_monitor_payload())
        return payload


def set_release_monitor_manual_override(
    release_key,
    *,
    release_summary="",
    release_version="",
    release_dist_url="",
    ke="",
    zni_key="",
    zni_url="",
    clear_zni=False,
    reset=False,
):
    global _cached_data, _last_cache_update

    release_key = str(release_key or "").strip()
    if not release_key:
        raise ValueError("Не указан ключ строки релиза")

    with _cache_lock:
        if _cached_data is None:
            disk_payload = _load_snapshot_from_disk()
            if disk_payload is not None:
                _cached_data = _hydrate_release_monitor_payload(disk_payload)
                _last_cache_update = _get_snapshot_mtime() or time.time()

        if _cached_data is None:
            raise ValueError("Таблица релизов еще не загружена")

        current_payload = _get_cached_payload_copy() or _build_empty_release_monitor_payload()
        current_items = current_payload.get("items") or []
        target_item = next(
            (item for item in current_items if _get_assignment_key_for_item(item) == release_key),
            None,
        )
        if not target_item:
            raise ValueError("Не удалось найти строку релиза")

        base_summary = str(target_item.get("base_release_summary") or target_item.get("release_summary") or "").strip()
        base_version = str(target_item.get("base_release_version") or target_item.get("release_version") or "").strip()
        base_url = str(target_item.get("base_release_dist_url") or target_item.get("release_dist_url") or "").strip()
        base_ke = str(target_item.get("base_ke") or target_item.get("ke") or "").strip()
        base_zni_key = str(target_item.get("base_zni_key") or target_item.get("zni_key") or "").strip()
        base_zni_url = str(target_item.get("base_zni_url") or target_item.get("zni_url") or "").strip()

        if reset:
            overrides = _load_manual_release_overrides()
            removed_override = dict(overrides.get(release_key) or {})
            overrides.pop(release_key, None)
            _save_manual_release_overrides(overrides)

            if _cached_data is not None:
                _cached_data["manual_overrides"] = dict(overrides)
                items = _cached_data.get("items") or []
                reset_base_summary = str(
                    target_item.get("base_release_summary")
                    or removed_override.get("base_release_summary")
                    or ""
                ).strip()
                reset_base_version = str(
                    target_item.get("base_release_version")
                    or removed_override.get("base_release_version")
                    or ""
                ).strip()
                reset_base_url = str(
                    target_item.get("base_release_dist_url")
                    or removed_override.get("base_release_dist_url")
                    or ""
                ).strip()
                reset_base_ke = str(
                    target_item.get("base_ke")
                    or removed_override.get("base_ke")
                    or ""
                ).strip()
                reset_base_zni_key = str(
                    target_item.get("base_zni_key")
                    or removed_override.get("base_zni_key")
                    or ""
                ).strip()
                reset_base_zni_url = str(
                    target_item.get("base_zni_url")
                    or removed_override.get("base_zni_url")
                    or ""
                ).strip()
                _apply_reviewer_assignments(items)
                _apply_date_overrides(items)
                _apply_duty_schedule_assignments(items, persist=True)
                _apply_zni_assignments(items)
                for item in items:
                    if _get_assignment_key_for_item(item) != release_key:
                        continue
                    item["base_release_summary"] = reset_base_summary
                    item["base_release_version"] = reset_base_version
                    item["base_release_dist_url"] = reset_base_url
                    item["base_ke"] = reset_base_ke
                    item["base_zni_key"] = reset_base_zni_key
                    item["base_zni_url"] = reset_base_zni_url
                    item["release_summary"] = reset_base_summary
                    item["release_version"] = reset_base_version
                    item["release_dist_url"] = reset_base_url
                    item["ke"] = reset_base_ke
                    item["zni_key"] = reset_base_zni_key
                    item["zni_url"] = reset_base_zni_url
                    if isinstance(item.get("base_release_name_lines"), list):
                        item["release_name_lines"] = list(item.get("base_release_name_lines") or [])
                    break
                _apply_manual_release_overrides(items)
                _sort_and_number_records(items)
                _save_snapshot_to_disk(_cached_data)

            payload = _normalize_release_payload(_get_cached_payload_copy() or _build_empty_release_monitor_payload())
            return payload

        normalized_summary = str(release_summary or "").strip()
        normalized_version = str(release_version or "").strip()
        normalized_url = _normalize_artifact_url(str(release_dist_url or "").strip())
        normalized_ke = str(ke or "").strip()
        normalized_zni_key = str(zni_key or "").strip()
        normalized_zni_url = str(zni_url or "").strip()
        normalized_clear_zni = bool(clear_zni)

        overrides = _load_manual_release_overrides()
        current_override = dict(overrides.get(release_key) or {})
        if base_summary and not str(current_override.get("base_release_summary") or "").strip():
            current_override["base_release_summary"] = base_summary
        if base_version and not str(current_override.get("base_release_version") or "").strip():
            current_override["base_release_version"] = base_version
        if base_url and not str(current_override.get("base_release_dist_url") or "").strip():
            current_override["base_release_dist_url"] = base_url
        if base_ke and not str(current_override.get("base_ke") or "").strip():
            current_override["base_ke"] = base_ke
        if base_zni_key and not str(current_override.get("base_zni_key") or "").strip():
            current_override["base_zni_key"] = base_zni_key
        if base_zni_url and not str(current_override.get("base_zni_url") or "").strip():
            current_override["base_zni_url"] = base_zni_url

        if normalized_summary and normalized_summary != base_summary:
            current_override["release_summary"] = normalized_summary
        else:
            current_override.pop("release_summary", None)

        if normalized_version and normalized_version != base_version:
            current_override["release_version"] = normalized_version
        else:
            current_override.pop("release_version", None)

        if normalized_url and normalized_url != base_url:
            current_override["release_dist_url"] = normalized_url
        else:
            current_override.pop("release_dist_url", None)

        if normalized_ke and normalized_ke != base_ke:
            current_override["ke"] = _format_ke_id(normalized_ke) if re.fullmatch(r"\d+", normalized_ke) else normalized_ke
        else:
            current_override.pop("ke", None)

        if normalized_clear_zni and base_zni_key:
            current_override["clear_zni"] = True
            current_override.pop("zni_key", None)
            current_override.pop("zni_url", None)
        else:
            current_override.pop("clear_zni", None)
            if normalized_zni_key and normalized_zni_key != base_zni_key:
                current_override["zni_key"] = normalized_zni_key
                derived_url = _resolve_manual_zni_url(normalized_zni_key, normalized_zni_url)
                if derived_url and derived_url != base_zni_url:
                    current_override["zni_url"] = derived_url
                else:
                    current_override.pop("zni_url", None)
            else:
                current_override.pop("zni_key", None)
                current_override.pop("zni_url", None)

        current_override = _normalize_manual_release_override(current_override)
        if current_override:
            current_override["updated_at"] = _format_timestamp()
            overrides[release_key] = current_override
        else:
            overrides.pop(release_key, None)

        _save_manual_release_overrides(overrides)

        if _cached_data is None:
            disk_payload = _load_snapshot_from_disk()
            if disk_payload is not None:
                _cached_data = _hydrate_release_monitor_payload(disk_payload)

        if _cached_data is not None:
            _cached_data["manual_overrides"] = dict(overrides)
            items = _cached_data.get("items") or []
            _apply_reviewer_assignments(items)
            _apply_date_overrides(items)
            _apply_duty_schedule_assignments(items, persist=True)
            _apply_zni_assignments(items)
            _apply_manual_release_overrides(items)
            _sort_and_number_records(items)
            _save_snapshot_to_disk(_cached_data)

        payload = _normalize_release_payload(_get_cached_payload_copy() or _build_empty_release_monitor_payload())
        return payload


def sync_release_monitor_assignments_from_confluence(year):
    global _cached_data, _last_cache_update

    year = int(year or datetime.now().year)
    with _cache_lock:
        _ensure_scheduler_started()
        if _cached_data is None:
            disk_payload = _load_snapshot_from_disk()
            if disk_payload is not None:
                _cached_data = _hydrate_release_monitor_payload(disk_payload)
                _last_cache_update = time.time()

        if _cached_data is None:
            raise ValueError("Таблица релизов еще не загружена. Сначала выполните обновление релизов.")

        payload = _normalize_release_payload(_get_cached_payload_copy() or _build_empty_release_monitor_payload())
        items = [item for item in payload.get("items", []) if int(item.get("year", 0) or 0) == year]
        if not items:
            raise ValueError(f"Для {year} года нет строк для выгрузки в Confluence")

        page_data = _fetch_confluence_release_page(year)
        replacement_table = _build_confluence_release_table(items, year)
        updated_storage, replaced = _replace_release_monitor_table_in_storage(page_data["storage_html"], replacement_table)
        if not replaced:
            updated_storage = replacement_table

        token = str(TOKENS.get("confluence_delta_token", "") or "").strip()
        url = f"{CONFLUENCE_DELTA_BASE}/rest/api/content/{page_data['page_id']}"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        next_version = max(int(page_data.get("version") or 0), 1) + 1
        page_title = page_data["title"] or f"Блок релизов {year}"
        payload_body = {
            "id": page_data["page_id"],
            "type": "page",
            "title": page_title,
            "version": {
                "number": next_version,
                "minorEdit": True,
                "message": f"Автообновление таблицы релизов за {year} год",
            },
            "body": {
                "storage": {
                    "value": updated_storage,
                    "representation": "storage",
                }
            },
        }

        response = requests.put(url, headers=headers, json=payload_body, verify=False, timeout=60)
        if not response.ok:
            detail = _extract_confluence_error_detail(response)
            logging.error(
                "Confluence release push failed: page_id=%s year=%s status=%s detail=%s payload_keys=%s",
                page_data["page_id"],
                year,
                response.status_code,
                detail,
                list(payload_body.keys()),
            )
            raise ValueError(f"Confluence PUT failed ({response.status_code}): {detail}")
        result_data = response.json()
        published_version = int(((result_data.get("version") or {}).get("number")) or next_version)
        page_url = f"{CONFLUENCE_DELTA_BASE}/pages/viewpage.action?pageId={page_data['page_id']}"

        _cached_data.setdefault("meta", {})["last_confluence_sync"] = _format_timestamp()
        _save_snapshot_to_disk(_cached_data)

        return {
            "year": year,
            "page_id": page_data["page_id"],
            "page_url": page_url,
            "page_title": page_title,
            "page_version": published_version,
            "rows_pushed": len(items),
            "replaced": replaced,
            "data": _normalize_release_payload(_get_cached_payload_copy() or _build_empty_release_monitor_payload()),
        }

    with _cache_lock:
        _ensure_scheduler_started()
        if _cached_data is None:
            disk_payload = _load_snapshot_from_disk()
            if disk_payload:
                _cached_data = _hydrate_release_monitor_payload(disk_payload)
                _last_cache_update = time.time()

        if _cached_data is None:
            raise ValueError("РўР°Р±Р»РёС†Р° СЂРµР»РёР·РѕРІ РµС‰Рµ РЅРµ Р·Р°РіСЂСѓР¶РµРЅР°. РЎРЅР°С‡Р°Р»Р° РІС‹РїРѕР»РЅРёС‚Рµ РѕР±РЅРѕРІР»РµРЅРёРµ СЂРµР»РёР·РѕРІ.")

        assignments = _load_reviewer_assignments()
        matched_rows = 0

        for item in _cached_data.get("items", []):
            if int(item.get("year", 0) or 0) != year:
                continue

            row_key = _get_assignment_key_for_item(item)
            release_key = item.get("release_key")
            fallback_row_key = f"{release_key}::no-rov" if release_key else ""
            source = (
                confluence_assignments.get(row_key)
                or confluence_assignments.get(fallback_row_key)
            )
            if not source:
                continue

            current_assignment = dict(assignments.get(row_key) or assignments.get(release_key) or {})
            current_responsibles = current_assignment.get("responsibles")
            if not isinstance(current_responsibles, list):
                current_responsibles = []
            current_responsibles = [str(value or "").strip() for value in current_responsibles if str(value or "").strip()]
            current_checker = str(current_assignment.get("checker", "") or "").strip()

            item_responsibles = item.get("psi_responsibles")
            if not isinstance(item_responsibles, list):
                item_responsibles = []
            item_responsibles = [str(value or "").strip() for value in item_responsibles if str(value or "").strip()]
            item_checker = str(item.get("psi_checker", "") or "").strip()

            effective_responsibles = current_responsibles or item_responsibles
            effective_checker = current_checker or item_checker
            applied = False

            if not effective_responsibles and source.get("responsibles"):
                new_responsibles = list(source.get("responsibles", []))
                current_assignment["responsibles"] = new_responsibles
                item["psi_responsibles"] = new_responsibles
                applied = True

            if not effective_checker and source.get("checker"):
                new_checker = str(source.get("checker", "") or "").strip()
                current_assignment["checker"] = new_checker
                item["psi_checker"] = new_checker
                applied = True

            if (
                current_assignment.get("reviewer")
                or current_assignment.get("checker")
                or current_assignment.get("responsibles")
            ):
                assignments[row_key] = current_assignment

            if applied:
                matched_rows += 1

        _save_reviewer_assignments(assignments)
        _cached_data.setdefault("meta", {})["last_confluence_sync"] = _format_timestamp()
        _save_snapshot_to_disk(_cached_data)

        return {
            "matched_rows": matched_rows,
            "source_rows": len(confluence_assignments),
            "year": year,
            "data": _get_cached_payload_copy() or _build_empty_release_monitor_payload(),
        }


def set_release_monitor_reviewer(release_key, reviewer):
    global _cached_data

    release_key = (release_key or "").strip()
    reviewer = (reviewer or "").strip()
    if not release_key:
        raise ValueError("РќРµ СѓРєР°Р·Р°РЅ РєР»СЋС‡ СЂРµР»РёР·Р°")

    if reviewer and reviewer not in OPLOT_VALUES:
        raise ValueError("Р’С‹Р±СЂР°РЅРЅС‹Р№ РїСЂРѕРІРµСЂСЏСЋС‰РёР№ РѕС‚СЃСѓС‚СЃС‚РІСѓРµС‚ РІ СЃРїРёСЃРєРµ РћРџР›РћРў")

    assignments = _load_reviewer_assignments()
    current_assignment = dict(assignments.get(release_key, {}))
    current_assignment["reviewer"] = reviewer
    current_assignment["reviewer_source"] = "manual" if reviewer else ""
    current_assignment["reviewer_date"] = "manual" if reviewer else ""

    if current_assignment.get("reviewer") or current_assignment.get("checker") or current_assignment.get("responsibles"):
        assignments[release_key] = current_assignment
    else:
        assignments.pop(release_key, None)
    _save_reviewer_assignments(assignments)

    with _cache_lock:
        if _cached_data is None:
            disk_payload = _load_snapshot_from_disk()
            if disk_payload is not None:
                _cached_data = _hydrate_release_monitor_payload(disk_payload)
        if _cached_data is not None:
            for item in _cached_data.get("items", []):
                item_key = _get_assignment_key_for_item(item)
                if item_key == release_key:
                    item["psi_owner"] = reviewer
                    item["psi_owner_source"] = current_assignment.get("reviewer_source", "")
                    item["psi_owner_date"] = current_assignment.get("reviewer_date", "")
                    item["psi_checker"] = current_assignment.get("checker", "")
                    item["psi_responsibles"] = list(current_assignment.get("responsibles", []))
                    break
            _save_snapshot_to_disk(_cached_data)

    return reviewer


def set_release_monitor_assignment(release_key, reviewer, checker, responsibles=None, reviewer_source=None):
    global _cached_data

    release_key = (release_key or "").strip()
    reviewer = (reviewer or "").strip()
    checker = (checker or "").strip()
    if not release_key:
        raise ValueError("РќРµ СѓРєР°Р·Р°РЅ РєР»СЋС‡ СЂРµР»РёР·Р°")

    if reviewer and reviewer_source != "manual_text" and reviewer not in OPLOT_VALUES:
        raise ValueError("Р’С‹Р±СЂР°РЅРЅС‹Р№ РґРµР¶СѓСЂРЅС‹Р№ РѕС‚СЃСѓС‚СЃС‚РІСѓРµС‚ РІ СЃРїРёСЃРєРµ РћРџР›РћРў")

    normalized_responsibles = []
    for responsible in (responsibles or []):
        responsible_name = str(responsible or "").strip()
        if not responsible_name:
            continue
        if responsible_name not in OPLOT_VALUES:
            raise ValueError("Р’С‹Р±СЂР°РЅРЅС‹Р№ РѕС‚РІРµС‚СЃС‚РІРµРЅРЅС‹Р№ РѕС‚СЃСѓС‚СЃС‚РІСѓРµС‚ РІ СЃРїРёСЃРєРµ РћРџР›РћРў")
        if responsible_name not in normalized_responsibles:
            normalized_responsibles.append(responsible_name)

    assignments = _load_reviewer_assignments()
    current_assignment = dict(assignments.get(release_key, {}))
    if reviewer_source is None:
        reviewer_source = current_assignment.get("reviewer_source")
    reviewer_source = str(reviewer_source or "").strip()
    if reviewer_source == "manual_text" and reviewer:
        resolved_reviewer_source = "manual_text"
        reviewer_date = "manual"
    elif reviewer_source == "duty_schedule" and reviewer:
        resolved_reviewer_source = "duty_schedule"
        reviewer_date = str(current_assignment.get("reviewer_date") or "").strip()
    else:
        resolved_reviewer_source = "manual" if reviewer else ""
        reviewer_date = "manual" if reviewer else ""

    if reviewer or checker or normalized_responsibles:
        assignments[release_key] = {
            "reviewer": reviewer,
            "reviewer_source": resolved_reviewer_source,
            "reviewer_date": reviewer_date,
            "checker": checker,
            "responsibles": normalized_responsibles,
        }
    else:
        assignments.pop(release_key, None)
    _save_reviewer_assignments(assignments)

    with _cache_lock:
        if _cached_data is None:
            disk_payload = _load_snapshot_from_disk()
            if disk_payload is not None:
                _cached_data = _hydrate_release_monitor_payload(disk_payload)
        if _cached_data is not None:
            for item in _cached_data.get("items", []):
                item_key = _get_assignment_key_for_item(item)
                if item_key == release_key:
                    item["psi_owner"] = reviewer
                    item["psi_owner_source"] = resolved_reviewer_source
                    item["psi_owner_date"] = reviewer_date
                    item["psi_checker"] = checker
                    item["psi_responsibles"] = list(normalized_responsibles)
                    break
            _save_snapshot_to_disk(_cached_data)
        else:
            _touch_release_monitor_revision()

    return {
        "reviewer": reviewer,
        "reviewer_source": resolved_reviewer_source,
        "reviewer_date": reviewer_date,
        "checker": checker,
        "responsibles": normalized_responsibles,
        "data_revision": _read_data_revision(),
    }

