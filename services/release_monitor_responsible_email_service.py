import copy
import hashlib
import html
import json
import logging
import os
import threading
import time
from collections import defaultdict
from datetime import datetime, timedelta, time as dt_time
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple
from uuid import uuid4

from services.feature_flags_service import get_automation_config
from services.release_monitor_email_service import (
    EMAIL_THROTTLE_SECONDS,
    MAX_EMAIL_ROWS,
    ReleaseMonitorEmailError,
    _format_timestamp,
    _html_link,
    _mail_settings,
    _normalize_recipients,
    _parse_timestamp,
    _send_email_message,
    _snapshot_label,
    _validate_delivery_settings,
)
from services.release_report_service import get_release_report_service


RESPONSIBLE_EMAIL_AUTOMATION_FLAG = "release_monitor_responsible_email"
UNASSIGNED_EMAIL_AUTOMATION_FLAG = "release_monitor_unassigned_email"
RESPONSIBLE_EMAIL_LOCK_STALE_SECONDS = 900
EVENT_ASSIGNED_TO_RESPONSIBLE = "assigned_to_responsible"
DEFAULT_ASSIGNMENT_EMAIL_DELAY_MINUTES = 6
DEFAULT_PERSONAL_EMAIL_SEND_INTERVAL_SECONDS = 5

RESPONSIBLE_NOTIFY_STATE_FILE = (
    Path(__file__).resolve().parent.parent
    / "cache"
    / "release_monitor_responsible_email_notify_state.json"
)
RESPONSIBLE_EMAIL_LOCK_FILE = RESPONSIBLE_NOTIFY_STATE_FILE.with_suffix(".lock")

_process_lock = threading.Lock()
_queue_lock = threading.Lock()
_worker_thread = None
_queued_job = None
_last_observed_enabled = bool(
    (get_automation_config(RESPONSIBLE_EMAIL_AUTOMATION_FLAG) or {}).get(
        "enabled",
        False,
    )
)


def _default_state() -> Dict:
    return {
        "version": 1,
        "tracking_state": "uninitialized",
        "week_key": "",
        "active_assignments": {},
        "pending_events": {},
        "missing_recipient_names": [],
        "last_evaluated_at": "",
        "last_email_attempt_at": "",
        "last_email_success_at": "",
        "last_snapshot_revision": "",
        "last_result": "",
        "last_error": "",
        "last_email_subject": "",
        "last_email_recipients": [],
        "last_email_event_count": 0,
        "last_weekly_digest_week_key": "",
        "last_weekly_digest_success_at": "",
        "last_weekly_digest_subject": "",
        "last_weekly_digest_recipients": [],
        "last_weekly_digest_total_count": 0,
        "last_weekly_digest_missing_count": 0,
    }


def _normalize_assignment_map(value) -> Dict[str, str]:
    if not isinstance(value, dict):
        return {}
    normalized = {}
    for row_key, responsible in value.items():
        clean_key = str(row_key or "").strip()
        clean_responsible = str(responsible or "").strip()
        if clean_key and clean_responsible:
            normalized[clean_key] = clean_responsible
    return normalized


def _normalize_pending_events(value) -> Dict[str, Dict[str, str]]:
    if not isinstance(value, dict):
        return {}
    normalized = {}
    for row_key, event in value.items():
        clean_key = str(row_key or "").strip()
        if not clean_key:
            continue
        event = event if isinstance(event, dict) else {}
        responsible = str(event.get("responsible") or "").strip()
        if not responsible:
            continue
        normalized[clean_key] = {
            "event_type": EVENT_ASSIGNED_TO_RESPONSIBLE,
            "responsible": responsible,
            "previous_responsible": str(event.get("previous_responsible") or "").strip(),
            "detected_at": str(event.get("detected_at") or "").strip(),
        }
    return normalized


def _normalize_string_list(values) -> List[str]:
    if not isinstance(values, (list, tuple, set)):
        values = []
    return sorted(
        {
            str(value or "").strip()
            for value in values
            if str(value or "").strip()
        }
    )


def _coerce_non_negative_int(value, default: int) -> int:
    if isinstance(value, bool):
        return default
    try:
        normalized = int(value)
    except (TypeError, ValueError):
        return default
    return normalized if normalized >= 0 else default


def _normalize_state(payload) -> Dict:
    state = _default_state()
    if isinstance(payload, dict):
        for key in state:
            if key in payload:
                state[key] = payload[key]
    state["active_assignments"] = _normalize_assignment_map(
        state.get("active_assignments")
    )
    state["pending_events"] = _normalize_pending_events(state.get("pending_events"))
    state["missing_recipient_names"] = _normalize_string_list(
        state.get("missing_recipient_names")
    )
    state["last_email_recipients"] = _normalize_recipients(
        state.get("last_email_recipients"),
        strict=False,
    )
    state["last_weekly_digest_recipients"] = _normalize_recipients(
        state.get("last_weekly_digest_recipients"),
        strict=False,
    )
    for key in (
        "last_email_event_count",
        "last_weekly_digest_total_count",
        "last_weekly_digest_missing_count",
    ):
        try:
            state[key] = max(0, int(state.get(key) or 0))
        except (TypeError, ValueError):
            state[key] = 0
    state["version"] = 1
    return state


def _load_state() -> Tuple[Dict, bool]:
    if not RESPONSIBLE_NOTIFY_STATE_FILE.exists():
        return _default_state(), False
    try:
        with RESPONSIBLE_NOTIFY_STATE_FILE.open("r", encoding="utf-8-sig") as handle:
            return _normalize_state(json.load(handle)), True
    except Exception as exc:
        logging.error(
            "Release monitor responsible email: failed to read notify state: %s",
            exc,
        )
        return _default_state(), False


def _atomic_write_state(state: Dict) -> None:
    RESPONSIBLE_NOTIFY_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    temp_path = RESPONSIBLE_NOTIFY_STATE_FILE.with_name(
        f".{RESPONSIBLE_NOTIFY_STATE_FILE.name}.{uuid4().hex}.tmp"
    )
    try:
        with temp_path.open("w", encoding="utf-8") as handle:
            json.dump(_normalize_state(state), handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, RESPONSIBLE_NOTIFY_STATE_FILE)
    finally:
        if temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass


def _responsible_settings() -> Dict:
    config = get_automation_config(RESPONSIBLE_EMAIL_AUTOMATION_FLAG)
    if not isinstance(config, dict):
        return {
            "enabled": False,
            "employee_recipients": {},
            "weekly_digest_enabled": False,
            "weekly_digest_time": "16:00",
            "assignment_email_delay_minutes": DEFAULT_ASSIGNMENT_EMAIL_DELAY_MINUTES,
            "personal_email_send_interval_seconds": DEFAULT_PERSONAL_EMAIL_SEND_INTERVAL_SECONDS,
        }
    recipients = {}
    raw_recipients = config.get("employee_recipients")
    if isinstance(raw_recipients, dict):
        for name, addresses in raw_recipients.items():
            clean_name = str(name or "").strip()
            address_values = (
                addresses
                if isinstance(addresses, (list, tuple, set))
                else [addresses]
            )
            clean_addresses = _normalize_recipients(address_values, strict=False)
            if clean_name and clean_addresses:
                recipients[clean_name] = clean_addresses
    return {
        "enabled": bool(config.get("enabled", False)),
        "employee_recipients": recipients,
        "weekly_digest_enabled": bool(config.get("weekly_digest_enabled", True)),
        "weekly_digest_time": str(config.get("weekly_digest_time") or "16:00").strip()
        or "16:00",
        "assignment_email_delay_minutes": _coerce_non_negative_int(
            config.get("assignment_email_delay_minutes"),
            DEFAULT_ASSIGNMENT_EMAIL_DELAY_MINUTES,
        ),
        "personal_email_send_interval_seconds": _coerce_non_negative_int(
            config.get("personal_email_send_interval_seconds"),
            DEFAULT_PERSONAL_EMAIL_SEND_INTERVAL_SECONDS,
        ),
    }


def _unassigned_recipients() -> List[str]:
    config = get_automation_config(UNASSIGNED_EMAIL_AUTOMATION_FLAG)
    if not isinstance(config, dict):
        return []
    return _normalize_recipients(config.get("recipients"), strict=False)


def _parse_digest_time(value: str) -> dt_time:
    raw_value = str(value or "").strip()
    for fmt in ("%H:%M", "%H:%M:%S"):
        try:
            return datetime.strptime(raw_value, fmt).time()
        except ValueError:
            continue
    return dt_time(hour=16, minute=0)


def _week_bounds(reference_dt: Optional[datetime] = None) -> Tuple[datetime.date, datetime.date]:
    current = reference_dt or datetime.now()
    start = current.date() - timedelta(days=current.weekday())
    return start, start + timedelta(days=6)


def _week_key(reference_dt: Optional[datetime] = None) -> str:
    start, _ = _week_bounds(reference_dt)
    iso_year, iso_week, _ = start.isocalendar()
    return f"{iso_year}-W{iso_week:02d}"


def _week_period(reference_dt: Optional[datetime] = None) -> str:
    start, end = _week_bounds(reference_dt)
    return f"{start.strftime('%d.%m.%Y')} - {end.strftime('%d.%m.%Y')}"


def _parse_item_date(item: Dict) -> Optional[datetime.date]:
    for value in (
        item.get("deployment_start_iso"),
        item.get("deployment_start"),
        item.get("sort_date"),
    ):
        raw_value = str(value or "").strip()
        if not raw_value:
            continue
        for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%Y-%m-%dT%H:%M:%S"):
            try:
                candidate = raw_value[:19] if "T" in fmt else raw_value[:10]
                return datetime.strptime(candidate, fmt).date()
            except ValueError:
                continue
    return None


def _item_source_index(item: Dict, fallback: int = 0) -> int:
    try:
        return int(item.get("_notification_source_index") or fallback or 0)
    except (TypeError, ValueError):
        return fallback


def _current_responsible(item: Dict) -> str:
    values = item.get("psi_responsibles") or []
    if not isinstance(values, list):
        values = [values] if values else []
    for value in values:
        responsible = str(value or "").strip()
        if responsible:
            return responsible
    return ""


def _is_operational_scope(item: Dict, reference_dt: Optional[datetime] = None) -> bool:
    if not isinstance(item, dict) or item.get("is_cancelled"):
        return False
    row_key = str(item.get("row_key") or "").strip()
    release_date = _parse_item_date(item)
    if not row_key or not release_date:
        return False
    week_start, week_end = _week_bounds(reference_dt)
    next_monday = week_end + timedelta(days=1)
    return week_start <= release_date <= week_end or release_date == next_monday


def _is_digest_week_scope(item: Dict, reference_dt: Optional[datetime] = None) -> bool:
    if not isinstance(item, dict) or item.get("is_cancelled"):
        return False
    row_key = str(item.get("row_key") or "").strip()
    release_date = _parse_item_date(item)
    if not row_key or not release_date:
        return False
    week_start, week_end = _week_bounds(reference_dt)
    return week_start <= release_date <= week_end


def _assignment_map(items: Iterable[Dict], reference_dt: Optional[datetime] = None) -> Dict[str, str]:
    assignments = {}
    for item in items or []:
        if not _is_operational_scope(item, reference_dt):
            continue
        responsible = _current_responsible(item)
        row_key = str(item.get("row_key") or "").strip()
        if row_key and responsible:
            assignments[row_key] = responsible
    return assignments


def _index_items(items: Iterable[Dict]) -> Dict[str, Dict]:
    indexed = {}
    for source_index, item in enumerate(items or []):
        if not isinstance(item, dict):
            continue
        row_key = str(item.get("row_key") or "").strip()
        if not row_key:
            continue
        selected = dict(item)
        selected["_notification_source_index"] = source_index
        indexed[row_key] = selected
    return indexed


def _snapshot_revision(snapshot: Dict) -> str:
    meta = dict((snapshot or {}).get("meta") or {})
    return str(meta.get("data_revision") or meta.get("accepted_revision") or "").strip()


def _fingerprint_assignments(assignments: Dict[str, str]) -> str:
    payload = json.dumps(assignments, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _acquire_file_lock() -> Optional[int]:
    RESPONSIBLE_EMAIL_LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(2):
        try:
            descriptor = os.open(
                RESPONSIBLE_EMAIL_LOCK_FILE,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            )
            os.write(descriptor, f"{os.getpid()} {_format_timestamp()}".encode("utf-8"))
            return descriptor
        except FileExistsError:
            try:
                age_seconds = time.time() - RESPONSIBLE_EMAIL_LOCK_FILE.stat().st_mtime
            except OSError:
                return None
            if age_seconds <= RESPONSIBLE_EMAIL_LOCK_STALE_SECONDS:
                return None
            try:
                RESPONSIBLE_EMAIL_LOCK_FILE.unlink()
                logging.warning(
                    "Release monitor responsible email: removed stale lock file"
                )
            except OSError:
                return None
    return None


def _release_file_lock(descriptor: Optional[int]) -> None:
    if descriptor is not None:
        try:
            os.close(descriptor)
        except OSError:
            pass
    try:
        RESPONSIBLE_EMAIL_LOCK_FILE.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        logging.warning(
            "Release monitor responsible email: failed to remove lock file: %s",
            exc,
        )


def _display(value, fallback: str = "—") -> str:
    value = str(value or "").strip()
    return value or fallback


def _owner_label(item: Dict) -> str:
    owner = str(item.get("psi_owner") or "").strip()
    if not owner:
        return "—"
    source = str(item.get("psi_owner_source") or "").strip()
    prefix = "Устанавливает" if source == "manual_text" else "Дежурный"
    return f"{prefix}: {owner}"


def _status_label(item: Dict) -> Tuple[str, str, str]:
    if item.get("is_final"):
        return "Завершен", "#166534", "#dcfce7"
    if item.get("is_cancelled"):
        return "Отменен", "#991b1b", "#fee2e2"
    return "Активен", "#1d4ed8", "#dbeafe"


def _release_label(item: Dict) -> str:
    release_key = _display(item.get("release_key"))
    release_link = _html_link(item.get("release_url"), release_key)
    return release_link if release_link != "—" else f"<strong>{html.escape(release_key)}</strong>"


def _secondary_meta(item: Dict) -> str:
    parts = []
    ke = str(item.get("ke") or "").strip()
    version = str(item.get("release_version") or "").strip()
    if ke:
        parts.append(f"КЭ: {html.escape(ke)}")
    if version:
        parts.append(f"Версия: {html.escape(version)}")
    return " · ".join(parts)


def _digest_release_title(item: Dict) -> str:
    lines = [
        str(part or "").strip()
        for part in (item.get("release_name_lines") or [])[:2]
        if str(part or "").strip()
    ]
    if lines:
        return " / ".join(lines)
    return str(item.get("release_summary") or "").strip()


def _release_type_label(item: Dict) -> str:
    try:
        return get_release_report_service()._get_item_kind_label(item)
    except Exception:
        logging.debug("Failed to classify release type via current-week report service", exc_info=True)
    release_type = str(item.get("release_type") or "").strip().lower()
    if item.get("is_reroll") or release_type == "reroll":
        return "Перераскатка"
    if item.get("is_hotfix") or release_type == "hotfix":
        return "Хотфикс"
    if release_type == "technical":
        return "Технический"
    return "Релиз"


def _system_label(item: Dict) -> str:
    try:
        return get_release_report_service()._get_item_system_name(item)
    except Exception:
        logging.debug("Failed to classify system via current-week report service", exc_info=True)
    if bool(item.get("is_ai_agent_template")):
        return "AI-Агенты"
    manual_system_name = str(item.get("manual_system_name") or "").strip()
    if manual_system_name:
        return manual_system_name
    source_prefix = str(item.get("source_prefix") or "").strip().upper()
    if source_prefix == "SMECSC":
        return "АИСТ"
    if source_prefix in {"AIGAS", "HELPERAI", "DRMMMB"}:
        return "AI-Агенты"
    if source_prefix == "EMRM":
        return "EMRM"
    if source_prefix in {"SMECLM", "CLM"}:
        return "CLM"
    system_name = str(item.get("system_name") or "").strip()
    if system_name:
        return system_name
    release_key = str(item.get("release_key") or "").strip()
    return release_key.split("-", 1)[0] if "-" in release_key else release_key


def _render_personal_cards(
    items: List[Dict],
    event_keys: Set[str],
) -> str:
    cards = []
    for item in items[:MAX_EMAIL_ROWS]:
        row_key = str(item.get("row_key") or "").strip()
        is_event = row_key in event_keys
        badge = ""
        border = "#93c5fd" if is_event else "#d8e0ea"
        background = "#f8fbff" if is_event else "#ffffff"
        if is_event:
            badge = (
                '<span style="display:inline-block;padding:3px 8px;border-radius:10px;'
                'background:#dbeafe;color:#1d4ed8;font-size:11px;font-weight:700;">'
                "Новое назначение</span>"
            )
        status, status_color, status_bg = _status_label(item)
        rov_key = _display(item.get("rov_key"))
        rov_link = _html_link(item.get("rov_url"), rov_key)
        if rov_link == "—":
            rov_link = html.escape(rov_key)
        summary = html.escape(_display(item.get("release_summary"), ""))
        secondary = _secondary_meta(item)
        secondary_html = (
            f'<span style="color:#94a3b8;"> · </span>{secondary}'
            if secondary
            else ""
        )
        cards.append(
            f"""
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="width:100%;border-collapse:collapse;margin:0 0 7px 0;background:{background};border:1px solid {border};">
  <tr>
    <td style="padding:8px 10px 7px 10px;">
      <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="width:100%;border-collapse:collapse;">
        <tr>
          <td style="vertical-align:top;padding:0 8px 0 0;">
            <div style="font-size:14px;line-height:18px;font-weight:700;color:#0f172a;mso-line-height-rule:exactly;">{_release_label(item)}</div>
            <div style="margin-top:2px;font-size:12px;line-height:15px;color:#475569;mso-line-height-rule:exactly;">{summary or "—"}</div>
          </td>
          <td align="right" style="vertical-align:top;width:190px;padding:0 0 0 8px;">{badge}</td>
        </tr>
        <tr>
          <td colspan="2" style="vertical-align:top;padding:6px 0 0 0;border-top:1px solid #e2e8f0;">
            <div style="font-size:12px;line-height:16px;color:#111827;mso-line-height-rule:exactly;">
              <span style="color:#64748b;">Дата:</span> <strong>{html.escape(_display(item.get("deployment_start")))}</strong>
              <span style="color:#94a3b8;"> · </span><span style="color:#64748b;">РОВ:</span> {rov_link}
              <span style="color:#94a3b8;"> · </span><span style="display:inline-block;padding:2px 7px;border-radius:10px;background:{status_bg};color:{status_color};font-size:11px;font-weight:700;">{html.escape(status)}</span>
              <span style="color:#94a3b8;"> · </span>{html.escape(_owner_label(item))}
              {secondary_html}
            </div>
          </td>
        </tr>
      </table>
    </td>
  </tr>
</table>
""".strip()
        )
    return "".join(cards) or (
        '<div style="padding:12px 14px;border:1px solid #d8e0ea;'
        'background:#f8fafc;color:#64748b;font-size:13px;">Нет релизов для отображения.</div>'
    )


def _sort_personal_items(items: Iterable[Dict], event_keys: Set[str]) -> List[Dict]:
    return sorted(
        items or [],
        key=lambda item: (
            0 if str(item.get("row_key") or "").strip() in event_keys else 1,
            _parse_item_date(item) or datetime.max.date(),
            _item_source_index(item),
        ),
    )


def _items_for_responsible(
    items: Iterable[Dict],
    responsible: str,
    reference_dt: Optional[datetime] = None,
) -> List[Dict]:
    selected = []
    for source_index, item in enumerate(items or []):
        if not _is_operational_scope(item, reference_dt):
            continue
        if _current_responsible(item) != responsible:
            continue
        selected_item = dict(item)
        selected_item["_notification_source_index"] = source_index
        selected.append(selected_item)
    return selected


def _build_personal_email_content(
    snapshot: Dict,
    responsible: str,
    events: Dict[str, Dict[str, str]],
    recipients: List[str],
) -> Tuple[str, str, str, Dict]:
    items = list((snapshot or {}).get("items") or [])
    event_keys = set(events)
    all_items = _sort_personal_items(
        _items_for_responsible(items, responsible),
        event_keys,
    )
    event_items = [item for item in all_items if str(item.get("row_key") or "") in event_keys]
    active_count = sum(1 for item in all_items if not item.get("is_final"))
    final_count = sum(1 for item in all_items if item.get("is_final"))
    generated_at = datetime.now().astimezone()
    period = _week_period(generated_at)
    release_monitor_url, _assignment_center_url = _release_monitor_links()
    subject = (
        f"[Блок релизов] Вы назначены ответственным: {len(event_keys)} "
        f"новое назначение"
    )
    if len(event_keys) != 1:
        subject = (
            f"[Блок релизов] Вы назначены ответственным: {len(event_keys)} "
            f"новых назначений"
        )
    summary_html = f"""
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="width:100%;border-collapse:collapse;">
  <tr>
    <td style="padding:10px;border:1px solid #d8e0ea;background:#f8fafc;width:33%;">
      <div style="font-size:11px;line-height:14px;color:#64748b;">НОВЫХ НАЗНАЧЕНИЙ</div>
      <div style="font-size:23px;line-height:28px;font-weight:700;color:#2563eb;">{len(event_keys)}</div>
    </td>
    <td style="padding:10px;border:1px solid #d8e0ea;background:#f8fafc;width:33%;">
      <div style="font-size:11px;line-height:14px;color:#64748b;">АКТУАЛЬНО В ОКНЕ</div>
      <div style="font-size:23px;line-height:28px;font-weight:700;color:#0f172a;">{len(all_items)}</div>
    </td>
    <td style="padding:10px;border:1px solid #d8e0ea;background:#f8fafc;width:33%;">
      <div style="font-size:11px;line-height:14px;color:#64748b;">АКТИВНО / ЗАВЕРШЕНО</div>
      <div style="font-size:23px;line-height:28px;font-weight:700;color:#0f172a;">{active_count} / {final_count}</div>
    </td>
  </tr>
</table>
""".strip()
    html_body = f"""<!doctype html>
<html lang="ru">
<head><meta http-equiv="Content-Type" content="text/html; charset=utf-8"></head>
<body style="margin:0;padding:0;background:#f3f6fa;font-family:Arial,sans-serif;color:#1f2937;">
  <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#f3f6fa;">
    <tr><td align="center" style="padding:24px 12px;">
      <table role="presentation" width="760" cellspacing="0" cellpadding="0" style="width:100%;max-width:760px;background:#ffffff;border:1px solid #d8e0ea;">
        <tr><td style="padding:22px 26px;background:#14213d;color:#ffffff;">
          <div style="font-size:15px;font-weight:700;color:#93c5fd;">Блок релизов</div>
          <div style="margin-top:6px;font-size:23px;font-weight:700;line-height:1.25;">Вы назначены ответственным по релизу</div>
          <div style="margin-top:10px;color:#dbeafe;font-size:13px;line-height:1.5;">В Блоке релизов появились новые назначения, где вы указаны актуальным ответственным. Последний добавленный ответственный считается актуальным.</div>
        </td></tr>
        <tr><td style="padding:18px 26px 4px;">
          {summary_html}
          <div style="margin-top:8px;color:#64748b;font-size:12px;">Период: {html.escape(period)} · Сформировано: {generated_at.strftime('%d.%m.%Y %H:%M')} · Снимок: {html.escape(_snapshot_label(snapshot))}</div>
          <div style="padding:18px 0;text-align:center;">
            <a href="{html.escape(release_monitor_url, quote=True)}" style="display:inline-block;padding:11px 22px;background:#2563eb;color:#ffffff;text-decoration:none;font-size:14px;font-weight:700;border-radius:4px;">Открыть Блок релизов</a>
          </div>
        </td></tr>
        <tr><td style="padding:0 26px 20px;">
          <div style="margin:4px 0 10px;font-size:18px;font-weight:700;color:#0f172a;">Новые назначения</div>
          {_render_personal_cards(event_items, event_keys)}
          <div style="margin:22px 0 10px;font-size:18px;font-weight:700;color:#0f172a;">Ваш актуальный список</div>
          {_render_personal_cards(all_items, event_keys)}
        </td></tr>
        <tr><td style="padding:17px 26px;background:#eef2f7;color:#64748b;font-size:12px;line-height:1.5;">
          Автоматическое уведомление системы Блок релизов.<br>Отвечать на данное письмо не требуется.
        </td></tr>
      </table>
    </td></tr>
  </table>
</body>
</html>"""
    text_lines = [
        "Блок релизов",
        "Вы назначены ответственным по релизу",
        "",
        f"Ответственный: {responsible}",
        f"Новых назначений: {len(event_keys)}",
        f"Актуально в окне: {len(all_items)}",
        f"Активно / завершено: {active_count} / {final_count}",
        f"Период: {period}",
        f"Блок релизов: {release_monitor_url}",
        "",
        "Новые назначения:",
    ]
    for item in event_items:
        text_lines.extend(_text_item_lines(item))
    text_lines.extend(["", "Ваш актуальный список:"])
    for item in all_items[:MAX_EMAIL_ROWS]:
        text_lines.extend(_text_item_lines(item))
    text_lines.extend(
        [
            "",
            "Автоматическое уведомление системы Блок релизов.",
            "Отвечать на данное письмо не требуется.",
        ]
    )
    return subject, "\n".join(text_lines), html_body, {
        "event_count": len(event_keys),
        "total_count": len(all_items),
        "recipients": list(recipients),
    }


def _text_item_lines(item: Dict) -> List[str]:
    status = _status_label(item)[0]
    lines = [
        (
            f"- {_display(item.get('deployment_start'))} | "
            f"{_display(item.get('release_key'))} | "
            f"РОВ: {_display(item.get('rov_key'))} | "
            f"{status} | {_owner_label(item)}"
        ),
        f"  {_display(item.get('release_summary'), '')}",
        f"  Jira Release: {_display(item.get('release_url'))}",
        f"  Jira РОВ: {_display(item.get('rov_url'))}",
    ]
    return lines


def _sort_digest_items(items: Iterable[Dict]) -> List[Dict]:
    return sorted(
        items or [],
        key=lambda item: (
            _parse_item_date(item) or datetime.max.date(),
            _item_source_index(item),
        ),
    )


def _digest_week_items(snapshot: Dict, reference_dt: Optional[datetime] = None) -> List[Dict]:
    selected = []
    for source_index, item in enumerate((snapshot or {}).get("items") or []):
        if not _is_digest_week_scope(item, reference_dt):
            continue
        selected_item = dict(item)
        selected_item["_notification_source_index"] = source_index
        selected.append(selected_item)
    return _sort_digest_items(selected)


def _system_summary_parts(items: List[Dict]) -> List[Tuple[str, int]]:
    counts = defaultdict(int)
    for item in items or []:
        system_name = _display(_system_label(item), "Не указано")
        counts[system_name] += 1
    return sorted(counts.items(), key=lambda pair: (-pair[1], pair[0].casefold()))


def _system_summary_html(items: List[Dict]) -> str:
    parts = _system_summary_parts(items)
    if not parts:
        return "—"
    chunks = []
    for label, count in parts[:6]:
        chunks.append(
            f'<span style="white-space:nowrap;color:#0f172a;font-weight:700;">'
            f'{html.escape(label)} <span style="color:#2563eb;">{count}</span></span>'
        )
    return '<span style="color:#94a3b8;"> · </span>'.join(chunks)


def _system_summary_text(items: List[Dict]) -> str:
    parts = _system_summary_parts(items)
    if not parts:
        return "—"
    return ", ".join(f"{label} {count}" for label, count in parts)


def _render_digest_table(items: List[Dict]) -> str:
    rows = []
    for item in items[:MAX_EMAIL_ROWS]:
        responsible = _current_responsible(item)
        missing = not responsible
        background = "#fff8f8" if missing else "#ffffff"
        responsible_cell = (
            '<span style="display:inline-block;padding:3px 8px;border-radius:10px;'
            'background:#fee2e2;color:#991b1b;font-size:11px;font-weight:700;">'
            "Нет ответственного</span>"
            if missing
            else html.escape(responsible)
        )
        release_key = _display(item.get("release_key"))
        rov_key = _display(item.get("rov_key"))
        release_link = _html_link(item.get("release_url"), release_key)
        rov_link = _html_link(item.get("rov_url"), rov_key)
        if release_link == "—":
            release_link = html.escape(release_key)
        if rov_link == "—":
            rov_link = html.escape(rov_key)
        values = (
            html.escape(_display(item.get("deployment_start"))),
            release_link,
            rov_link,
            html.escape(_display(_digest_release_title(item), "")),
            html.escape(_release_type_label(item)),
            html.escape(_display(_system_label(item))),
            responsible_cell,
            html.escape(_owner_label(item)),
        )
        cells = "".join(
            f'<td style="padding:8px 9px;border:1px solid #d8e0ea;'
            f'vertical-align:top;color:#1f2937;font-size:12px;line-height:1.35;">{value}</td>'
            for value in values
        )
        rows.append(f'<tr style="background:{background};">{cells}</tr>')
    headers = (
        "Дата",
        "Release",
        "РОВ",
        "Название",
        "Тип",
        "Система",
        "Ответственный",
        "Дежурный",
    )
    header_html = "".join(
        '<th style="padding:8px 9px;border:1px solid #cbd5e1;'
        'background:#e9f0fa;color:#334155;font-size:11px;'
        f'text-align:left;text-transform:uppercase;">{html.escape(title)}</th>'
        for title in headers
    )
    return (
        '<table role="presentation" width="100%" cellspacing="0" cellpadding="0" '
        'style="width:100%;border-collapse:collapse;table-layout:auto;">'
        f"<thead><tr>{header_html}</tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def _build_weekly_digest_content(
    snapshot: Dict,
    recipients: List[str],
) -> Tuple[str, str, str, Dict]:
    now = datetime.now().astimezone()
    items = _digest_week_items(snapshot, now)
    total_count = len(items)
    missing_count = sum(1 for item in items if not _current_responsible(item))
    assigned_count = total_count - missing_count
    period = _week_period(now)
    system_summary_html = _system_summary_html(items)
    system_summary_text = _system_summary_text(items)
    release_monitor_url, assignment_center_url = _release_monitor_links()
    subject = (
        f"[Блок релизов] Предварительная сводка недели: "
        f"{assigned_count} назначено, {missing_count} без ответственного"
    )
    limit_notice = ""
    if total_count > MAX_EMAIL_ROWS:
        limit_notice = (
            f'<div style="margin:14px 0;padding:11px 14px;border-left:4px solid #f59e0b;'
            'background:#fff7ed;color:#7c2d12;font-size:13px;">'
            f"Показаны первые {MAX_EMAIL_ROWS} записей из {total_count}. "
            "Для просмотра полного списка откройте Блок релизов.</div>"
        )
    summary_html = f"""
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="width:100%;border-collapse:collapse;">
  <tr>
    <td style="padding:8px 10px;border:1px solid #d8e0ea;background:#f8fafc;width:22%;">
      <div style="font-size:10px;line-height:13px;color:#64748b;">ВСЕГО</div>
      <div style="font-size:21px;line-height:24px;font-weight:700;color:#0f172a;">{total_count}</div>
    </td>
    <td style="padding:8px 10px;border:1px solid #d8e0ea;background:#f8fafc;width:22%;">
      <div style="font-size:10px;line-height:13px;color:#64748b;">НАЗНАЧЕНО</div>
      <div style="font-size:21px;line-height:24px;font-weight:700;color:#166534;">{assigned_count}</div>
    </td>
    <td style="padding:8px 10px;border:1px solid #d8e0ea;background:#f8fafc;width:22%;">
      <div style="font-size:10px;line-height:13px;color:#64748b;">БЕЗ ОТВЕТСТВЕННОГО</div>
      <div style="font-size:21px;line-height:24px;font-weight:700;color:#991b1b;">{missing_count}</div>
    </td>
    <td style="padding:8px 10px;border:1px solid #d8e0ea;background:#f8fafc;width:34%;">
      <div style="font-size:10px;line-height:13px;color:#64748b;">ПО СИСТЕМАМ</div>
      <div style="margin-top:4px;font-size:12px;line-height:16px;">{system_summary_html}</div>
    </td>
  </tr>
</table>
""".strip()
    html_body = f"""<!doctype html>
<html lang="ru">
<head><meta http-equiv="Content-Type" content="text/html; charset=utf-8"></head>
<body style="margin:0;padding:0;background:#f3f6fa;font-family:Arial,sans-serif;color:#1f2937;">
  <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#f3f6fa;">
    <tr><td align="center" style="padding:24px 12px;">
      <table role="presentation" width="860" cellspacing="0" cellpadding="0" style="width:100%;max-width:860px;background:#ffffff;border:1px solid #d8e0ea;">
        <tr><td style="padding:22px 26px;background:#14213d;color:#ffffff;">
          <div style="font-size:15px;font-weight:700;color:#93c5fd;">Блок релизов</div>
          <div style="margin-top:6px;font-size:23px;font-weight:700;line-height:1.25;">Предварительная сводка по релизам текущей недели</div>
          <div style="margin-top:10px;color:#dbeafe;font-size:13px;line-height:1.5;">Это предварительная сводка по предстоящим релизам текущей недели. Общую таблицу смотрите в Блоке релизов.</div>
        </td></tr>
        <tr><td style="padding:18px 26px 4px;">
          {summary_html}
          <div style="margin-top:8px;color:#64748b;font-size:12px;">Период: {html.escape(period)} · Сформировано: {now.strftime('%d.%m.%Y %H:%M')} · Снимок: {html.escape(_snapshot_label(snapshot))}</div>
          <div style="padding:18px 0;text-align:center;">
            <a href="{html.escape(release_monitor_url, quote=True)}" style="display:inline-block;padding:11px 22px;background:#2563eb;color:#ffffff;text-decoration:none;font-size:14px;font-weight:700;border-radius:4px;">Открыть Блок релизов</a>
            <span style="display:inline-block;width:10px;line-height:10px;">&nbsp;</span>
            <a href="{html.escape(assignment_center_url, quote=True)}" style="display:inline-block;padding:11px 22px;background:#0f766e;color:#ffffff;text-decoration:none;font-size:14px;font-weight:700;border-radius:4px;">Открыть Центр назначений</a>
          </div>
        </td></tr>
        <tr><td style="padding:0 26px 20px;">
          <div style="margin:4px 0 10px;font-size:18px;font-weight:700;color:#0f172a;">Релизы текущей недели</div>
          {limit_notice}
          {_render_digest_table(items)}
        </td></tr>
        <tr><td style="padding:17px 26px;background:#eef2f7;color:#64748b;font-size:12px;line-height:1.5;">
          Автоматическое уведомление системы Блок релизов.<br>Отвечать на данное письмо не требуется.
        </td></tr>
      </table>
    </td></tr>
  </table>
</body>
</html>"""
    text_lines = [
        "Блок релизов",
        "Предварительная сводка по релизам текущей недели",
        "",
        "Это предварительная сводка по предстоящим релизам текущей недели.",
        "Общую таблицу смотрите в Блоке релизов.",
        "",
        f"Всего релизов текущей недели: {total_count}",
        f"Назначено: {assigned_count}",
        f"Без ответственного: {missing_count}",
        f"По системам: {system_summary_text}",
        f"Период: {period}",
        f"Сформировано: {now.strftime('%d.%m.%Y %H:%M:%S')}",
        f"Блок релизов: {release_monitor_url}",
        f"Центр назначений: {assignment_center_url}",
        "",
        "Релизы текущей недели:",
    ]
    for item in items[:MAX_EMAIL_ROWS]:
        responsible = _current_responsible(item) or "Нет ответственного"
        text_lines.extend(
            [
                (
                    f"- {_display(item.get('deployment_start'))} | "
                    f"{_display(item.get('release_key'))} | "
                    f"РОВ: {_display(item.get('rov_key'))} | "
                    f"Тип: {_release_type_label(item)} | "
                    f"Система: {_display(_system_label(item))} | "
                    f"Ответственный: {responsible} | {_owner_label(item)}"
                ),
                f"  {_display(_digest_release_title(item), '')}",
                f"  Jira Release: {_display(item.get('release_url'))}",
                f"  Jira РОВ: {_display(item.get('rov_url'))}",
            ]
        )
    if total_count > MAX_EMAIL_ROWS:
        text_lines.append(
            f"Показаны первые {MAX_EMAIL_ROWS} записей из {total_count}. "
            "Для просмотра полного списка откройте Блок релизов."
        )
    text_lines.extend(
        [
            "",
            "Автоматическое уведомление системы Блок релизов.",
            "Отвечать на данное письмо не требуется.",
        ]
    )
    return subject, "\n".join(text_lines), html_body, {
        "total_count": total_count,
        "assigned_count": assigned_count,
        "missing_count": missing_count,
        "recipients": list(recipients),
    }


def _latest_snapshot() -> Dict:
    from services.release_monitor_service import get_release_monitor_snapshot

    return get_release_monitor_snapshot() or {}


def _release_monitor_links() -> Tuple[str, str]:
    public_url = _mail_settings()["public_url"].rstrip("/")
    assignment_suffix = "/assignment-center"
    if public_url.endswith(assignment_suffix):
        return public_url[: -len(assignment_suffix)], public_url
    return public_url, f"{public_url}{assignment_suffix}"


def _merge_assignment_event(
    pending: Dict[str, Dict[str, str]],
    row_key: str,
    responsible: str,
    *,
    previous_responsible: str = "",
    detected_at: Optional[str] = None,
) -> None:
    row_key = str(row_key or "").strip()
    responsible = str(responsible or "").strip()
    if not row_key or not responsible:
        return
    current = pending.get(row_key) if isinstance(pending.get(row_key), dict) else {}
    current_responsible = str(current.get("responsible") or "").strip()
    current_detected_at = str(current.get("detected_at") or "").strip()
    if current_responsible == responsible and current_detected_at:
        next_detected_at = current_detected_at
    else:
        next_detected_at = str(detected_at or "").strip() or _format_timestamp()
    pending[row_key] = {
        "event_type": EVENT_ASSIGNED_TO_RESPONSIBLE,
        "responsible": responsible,
        "previous_responsible": str(previous_responsible or "").strip(),
        "detected_at": next_detected_at,
    }


def _split_mature_assignment_events(
    pending: Dict[str, Dict[str, str]],
    now: datetime,
    delay_minutes: int,
) -> Tuple[Dict[str, Dict[str, str]], Dict[str, Dict[str, str]]]:
    if not pending:
        return {}, {}
    if delay_minutes <= 0:
        return dict(pending), {}

    mature = {}
    waiting = {}
    delay = timedelta(minutes=delay_minutes)
    now_text = _format_timestamp(now)
    for row_key, event in pending.items():
        detected_at = _parse_timestamp(event.get("detected_at"))
        normalized_event = dict(event)
        if not detected_at:
            normalized_event["detected_at"] = now_text
            waiting[row_key] = normalized_event
            continue
        if now - detected_at >= delay:
            mature[row_key] = normalized_event
        else:
            waiting[row_key] = normalized_event
    return mature, waiting


def _send_personal_events(
    snapshot: Dict,
    pending: Dict[str, Dict[str, str]],
    settings: Dict,
    employee_recipients: Dict[str, List[str]],
    send_interval_seconds: int,
) -> Tuple[Set[str], List[str], int, str, List[str], str]:
    if not pending:
        return set(), [], 0, "", [], ""

    items_by_key = _index_items((snapshot or {}).get("items") or [])
    by_responsible = defaultdict(dict)
    missing_recipients = set()
    for row_key, event in pending.items():
        responsible = str(event.get("responsible") or "").strip()
        if not responsible:
            continue
        if responsible not in employee_recipients:
            missing_recipients.add(responsible)
            continue
        if row_key not in items_by_key:
            continue
        by_responsible[responsible][row_key] = event

    sent_keys = set()
    last_subject = ""
    last_recipients = []
    sent_count = 0
    send_error = ""
    grouped_events = sorted(by_responsible.items())
    for index, (responsible, events) in enumerate(grouped_events):
        try:
            recipients = _normalize_recipients(
                employee_recipients[responsible],
                strict=True,
            )
            _validate_delivery_settings(settings, recipients)
            subject, text_body, html_body, metadata = _build_personal_email_content(
                snapshot,
                responsible,
                events,
                recipients,
            )
            _send_email_message(
                settings=settings,
                recipients=recipients,
                subject=subject,
                text_body=text_body,
                html_body=html_body,
            )
        except Exception as exc:
            send_error = (
                str(exc)
                if isinstance(exc, ReleaseMonitorEmailError)
                else f"Ошибка email-уведомления ответственных: {type(exc).__name__}."
            )
            logging.exception(
                "Release monitor responsible email failed for %s",
                responsible,
            )
            break
        sent_keys.update(events)
        sent_count += int(metadata.get("event_count") or 0)
        last_subject = subject
        last_recipients = recipients
        if send_interval_seconds > 0 and index < len(grouped_events) - 1:
            time.sleep(send_interval_seconds)
    return (
        sent_keys,
        sorted(missing_recipients),
        sent_count,
        last_subject,
        last_recipients,
        send_error,
    )


def _weekly_digest_due(state: Dict, settings: Dict, now: Optional[datetime] = None) -> bool:
    now = now or datetime.now()
    if not settings.get("weekly_digest_enabled"):
        return False
    if now.weekday() != 0:
        return False
    if now.time() < _parse_digest_time(settings.get("weekly_digest_time")):
        return False
    return state.get("last_weekly_digest_week_key") != _week_key(now)


def _send_weekly_digest(
    snapshot: Dict,
    state: Dict,
    settings: Dict,
) -> Tuple[bool, Dict]:
    if not _weekly_digest_due(state, settings):
        return False, {}
    recipients = _normalize_recipients(_unassigned_recipients(), strict=True)
    delivery_settings = _mail_settings()
    _validate_delivery_settings(delivery_settings, recipients)
    subject, text_body, html_body, metadata = _build_weekly_digest_content(
        snapshot,
        recipients,
    )
    _send_email_message(
        settings=delivery_settings,
        recipients=recipients,
        subject=subject,
        text_body=text_body,
        html_body=html_body,
    )
    return True, {
        **metadata,
        "subject": subject,
        "recipients": recipients,
    }


def _run_responsible_notification(
    snapshot: Dict,
    *,
    refresh_mode: str,
    process_assignments: bool,
    process_weekly_digest: bool,
    explicit_assignments: Optional[Dict[str, str]],
) -> Dict:
    settings = _responsible_settings()
    if not settings["enabled"]:
        return {"result": "disabled"}

    with _process_lock:
        descriptor = _acquire_file_lock()
        if descriptor is None:
            return {"result": "locked"}
        try:
            now = datetime.now()
            now_text = _format_timestamp(now)
            state, state_exists = _load_state()
            week_key = _week_key(now)
            items = list((snapshot or {}).get("items") or [])
            current_assignments = _assignment_map(items, now)
            is_refresh = refresh_mode in {"full", "reliable_full", "quick", "silent"}
            baseline_needed = (
                not state_exists
                or state.get("tracking_state") != "active"
                or state.get("week_key") != week_key
            )

            if baseline_needed:
                state.update(
                    {
                        "tracking_state": "active",
                        "week_key": week_key,
                        "active_assignments": dict(current_assignments),
                        "pending_events": {},
                        "missing_recipient_names": [],
                        "last_evaluated_at": now_text,
                        "last_snapshot_revision": _snapshot_revision(snapshot),
                        "last_result": (
                            "weekly_baseline_created"
                            if state_exists and state.get("week_key") != week_key
                            else "baseline_created"
                        ),
                        "last_error": "",
                    }
                )
                pending = {}
                if not is_refresh and explicit_assignments:
                    for row_key, responsible in explicit_assignments.items():
                        if current_assignments.get(row_key) == responsible:
                            _merge_assignment_event(
                                pending,
                                row_key,
                                responsible,
                                detected_at=now_text,
                            )
                    state["pending_events"] = pending
                _atomic_write_state(state)
                if not pending and not process_weekly_digest:
                    return {"result": state["last_result"]}
            else:
                pending = {
                    row_key: event
                    for row_key, event in _normalize_pending_events(
                        state.get("pending_events")
                    ).items()
                    if current_assignments.get(row_key) == event.get("responsible")
                }
                if process_assignments:
                    previous_assignments = _normalize_assignment_map(
                        state.get("active_assignments")
                    )
                    for row_key, responsible in current_assignments.items():
                        previous = previous_assignments.get(row_key, "")
                        if previous != responsible:
                            _merge_assignment_event(
                                pending,
                                row_key,
                                responsible,
                                previous_responsible=previous,
                                detected_at=now_text,
                            )
                    for row_key, responsible in (explicit_assignments or {}).items():
                        if current_assignments.get(row_key) == responsible:
                            _merge_assignment_event(
                                pending,
                                row_key,
                                responsible,
                                previous_responsible=previous_assignments.get(row_key, ""),
                                detected_at=now_text,
                            )
                state.update(
                    {
                        "active_assignments": dict(current_assignments),
                        "pending_events": pending,
                        "last_evaluated_at": now_text,
                        "last_snapshot_revision": _snapshot_revision(snapshot),
                    }
                )

            digest_sent = False
            digest_metadata = {}
            sent_keys = set()
            missing_recipients = []
            sent_count = 0
            last_subject = ""
            last_recipients = []
            send_error = ""

            if not pending and not process_weekly_digest:
                state["last_result"] = "waiting"
                state["last_error"] = ""
                _atomic_write_state(state)
                return {"result": "waiting"}

            latest_snapshot = _latest_snapshot()
            latest_items = list((latest_snapshot or {}).get("items") or [])
            latest_assignments = _assignment_map(latest_items, now)
            pending = {
                row_key: event
                for row_key, event in pending.items()
                if latest_assignments.get(row_key) == event.get("responsible")
            }
            mature_pending, waiting_pending = _split_mature_assignment_events(
                pending,
                now,
                int(settings.get("assignment_email_delay_minutes") or 0),
            )
            if not mature_pending and not process_weekly_digest:
                state.update(
                    {
                        "pending_events": {**waiting_pending},
                        "last_result": "waiting_delay" if waiting_pending else "waiting",
                        "last_error": "",
                    }
                )
                _atomic_write_state(state)
                return {
                    "result": state["last_result"],
                    "pending_count": len(waiting_pending),
                }

            last_attempt = _parse_timestamp(state.get("last_email_attempt_at"))
            personal_throttled = bool(
                mature_pending
                and last_attempt
                and (now - last_attempt).total_seconds() < EMAIL_THROTTLE_SECONDS
            )
            if personal_throttled and not process_weekly_digest:
                state["last_result"] = "throttled"
                _atomic_write_state(state)
                return {"result": "throttled", "pending_count": len(pending)}

            if (mature_pending and not personal_throttled) or process_weekly_digest:
                state.update(
                    {
                        "last_result": "running",
                        "last_error": "",
                    }
                )
                if mature_pending and not personal_throttled:
                    state["last_email_attempt_at"] = now_text
                _atomic_write_state(state)

            try:
                delivery_settings = _mail_settings()
                if mature_pending and not personal_throttled:
                    (
                        sent_keys,
                        missing_recipients,
                        sent_count,
                        last_subject,
                        last_recipients,
                        send_error,
                    ) = (
                        _send_personal_events(
                            latest_snapshot,
                            mature_pending,
                            delivery_settings,
                            settings["employee_recipients"],
                            int(settings.get("personal_email_send_interval_seconds") or 0),
                        )
                    )
                    pending = {
                        row_key: event
                        for row_key, event in pending.items()
                        if row_key not in sent_keys
                    }
                if process_weekly_digest:
                    digest_sent, digest_metadata = _send_weekly_digest(
                        latest_snapshot,
                        state,
                        settings,
                    )
            except Exception as exc:
                safe_error = (
                    str(exc)
                    if isinstance(exc, ReleaseMonitorEmailError)
                    else f"Ошибка email-уведомления ответственных: {type(exc).__name__}."
                )
                state.update(
                    {
                        "pending_events": pending,
                        "missing_recipient_names": missing_recipients,
                        "last_result": "error",
                        "last_error": safe_error,
                    }
                )
                if sent_count:
                    state.update(
                        {
                            "last_email_success_at": _format_timestamp(),
                            "last_email_subject": last_subject,
                            "last_email_recipients": last_recipients,
                            "last_email_event_count": sent_count,
                        }
                    )
                _atomic_write_state(state)
                logging.exception("Release monitor responsible email notification failed")
                return {"result": "error", "error": safe_error}

            pending = {
                row_key: event
                for row_key, event in pending.items()
                if row_key not in sent_keys
            }
            latest_assignments = _assignment_map(
                (latest_snapshot or {}).get("items") or [],
                now,
            )
            state.update(
                {
                    "active_assignments": dict(latest_assignments),
                    "pending_events": pending,
                    "missing_recipient_names": missing_recipients,
                    "last_evaluated_at": _format_timestamp(),
                    "last_snapshot_revision": _snapshot_revision(latest_snapshot),
                    "last_error": "",
                }
            )
            result_parts = []
            if sent_count:
                result_parts.append("sent")
                state.update(
                    {
                        "last_email_success_at": _format_timestamp(),
                        "last_email_subject": last_subject,
                        "last_email_recipients": last_recipients,
                        "last_email_event_count": sent_count,
                    }
                )
            if digest_sent:
                result_parts.append("weekly_digest_sent")
                state.update(
                    {
                        "last_weekly_digest_week_key": week_key,
                        "last_weekly_digest_success_at": _format_timestamp(),
                        "last_weekly_digest_subject": digest_metadata.get("subject", ""),
                        "last_weekly_digest_recipients": digest_metadata.get(
                            "recipients",
                            [],
                        ),
                        "last_weekly_digest_total_count": int(
                            digest_metadata.get("total_count") or 0
                        ),
                        "last_weekly_digest_missing_count": int(
                            digest_metadata.get("missing_count") or 0
                        ),
                    }
                )
            if send_error:
                result_parts.append("error")
                state["last_error"] = send_error
            if missing_recipients and pending:
                result_parts.append("missing_recipients")
            state["last_result"] = "+".join(result_parts) if result_parts else "waiting"
            _atomic_write_state(state)
            if send_error:
                return {
                    "result": state["last_result"],
                    "error": send_error,
                    "event_count": sent_count,
                    "pending_count": len(pending),
                    "digest_sent": digest_sent,
                }
            return {
                "result": state["last_result"],
                "event_count": sent_count,
                "pending_count": len(pending),
                "digest_sent": digest_sent,
            }
        finally:
            _release_file_lock(descriptor)


def _worker_loop() -> None:
    global _queued_job, _worker_thread
    while True:
        with _queue_lock:
            job = _queued_job
            _queued_job = None
            if not job:
                _worker_thread = None
                return
        try:
            _run_responsible_notification(
                job["snapshot"],
                refresh_mode=job["refresh_mode"],
                process_assignments=bool(job.get("process_assignments")),
                process_weekly_digest=bool(job.get("process_weekly_digest")),
                explicit_assignments=job.get("explicit_assignments"),
            )
        except Exception:
            logging.exception("Release monitor responsible email worker failed unexpectedly")


def _queue_job(
    snapshot: Dict,
    *,
    refresh_mode: str,
    process_assignments: bool,
    process_weekly_digest: bool,
    explicit_assignments: Optional[Dict[str, str]] = None,
) -> bool:
    global _queued_job, _worker_thread, _last_observed_enabled

    settings = _responsible_settings()
    enabled = settings["enabled"]
    with _queue_lock:
        _last_observed_enabled = enabled
        if not enabled:
            return False

        merged_explicit = {}
        if _queued_job:
            process_assignments = process_assignments or bool(
                _queued_job.get("process_assignments")
            )
            process_weekly_digest = process_weekly_digest or bool(
                _queued_job.get("process_weekly_digest")
            )
            merged_explicit.update(_queued_job.get("explicit_assignments") or {})
        merged_explicit.update(
            {
                str(row_key or "").strip(): str(responsible or "").strip()
                for row_key, responsible in (explicit_assignments or {}).items()
                if str(row_key or "").strip() and str(responsible or "").strip()
            }
        )
        _queued_job = {
            "snapshot": {
                "items": copy.deepcopy(list((snapshot or {}).get("items") or [])),
                "meta": copy.deepcopy(dict((snapshot or {}).get("meta") or {})),
            },
            "refresh_mode": str(refresh_mode or ""),
            "process_assignments": process_assignments,
            "process_weekly_digest": process_weekly_digest,
            "explicit_assignments": merged_explicit,
        }
        if _worker_thread and _worker_thread.is_alive():
            return True
        _worker_thread = threading.Thread(
            target=_worker_loop,
            daemon=True,
            name="release-monitor-responsible-email",
        )
        _worker_thread.start()
        return True


def schedule_responsible_email_notification(
    snapshot: Dict,
    *,
    refresh_mode: str,
    explicit_assignments: Optional[Dict[str, str]] = None,
) -> bool:
    return _queue_job(
        snapshot,
        refresh_mode=refresh_mode,
        process_assignments=True,
        process_weekly_digest=False,
        explicit_assignments=explicit_assignments,
    )


def schedule_responsible_weekly_digest(snapshot: Dict) -> bool:
    settings = _responsible_settings()
    if not settings["enabled"] or not settings.get("weekly_digest_enabled"):
        return False
    state, _ = _load_state()
    if not _weekly_digest_due(state, settings):
        return False
    return _queue_job(
        snapshot,
        refresh_mode="weekly_digest",
        process_assignments=False,
        process_weekly_digest=True,
    )


def get_responsible_email_status() -> Dict:
    global _last_observed_enabled

    settings = _responsible_settings()
    enabled = settings["enabled"]
    if not enabled:
        with _queue_lock:
            _last_observed_enabled = False
    state, state_exists = _load_state()
    if not enabled:
        status = "disabled"
    elif not state_exists:
        status = "waiting_refresh"
    elif state.get("last_result") == "error" or state.get("last_error"):
        status = "error"
    elif state.get("last_result") == "running":
        status = "sending"
    elif "sent" in str(state.get("last_result") or ""):
        status = "sent"
    elif state.get("last_result") in {"baseline_created", "weekly_baseline_created"}:
        status = "baseline_created"
    else:
        status = "waiting"
    return {
        "enabled": enabled,
        "status": status,
        "running": bool(_worker_thread and _worker_thread.is_alive()),
        "week_key": state.get("week_key", "") if state_exists else "",
        "pending_count": len(state.get("pending_events") or {}) if state_exists else 0,
        "missing_recipient_names": (
            list(state.get("missing_recipient_names") or []) if state_exists else []
        ),
        "last_result": state.get("last_result", "") if state_exists else "",
        "last_error": state.get("last_error", "") if state_exists else "",
        "last_email_success_at": (
            state.get("last_email_success_at", "") if state_exists else ""
        ),
        "last_email_event_count": (
            int(state.get("last_email_event_count") or 0) if state_exists else 0
        ),
        "last_weekly_digest_success_at": (
            state.get("last_weekly_digest_success_at", "") if state_exists else ""
        ),
        "last_weekly_digest_week_key": (
            state.get("last_weekly_digest_week_key", "") if state_exists else ""
        ),
    }
