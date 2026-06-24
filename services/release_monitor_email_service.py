import copy
import hashlib
import html
import json
import logging
import os
import smtplib
import ssl
import threading
import time
from datetime import datetime, timedelta
from email.message import EmailMessage
from email.utils import parseaddr
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple
from urllib.parse import urlparse
from uuid import uuid4

from config import TOKENS
from services.feature_flags_service import get_automation_config


EMAIL_AUTOMATION_FLAG = "release_monitor_unassigned_email"
EMAIL_THROTTLE_SECONDS = 300
EMAIL_LOCK_STALE_SECONDS = 900
MAX_EMAIL_ROWS = 100

EVENT_NEW_UNASSIGNED = "new_unassigned"
EVENT_RESPONSIBLE_REMOVED = "responsible_removed"
VALID_EVENT_TYPES = {EVENT_NEW_UNASSIGNED, EVENT_RESPONSIBLE_REMOVED}

NOTIFY_STATE_FILE = (
    Path(__file__).resolve().parent.parent
    / "cache"
    / "release_monitor_unassigned_email_notify_state.json"
)
EMAIL_LOCK_FILE = NOTIFY_STATE_FILE.with_suffix(".lock")

_process_lock = threading.Lock()
_queue_lock = threading.Lock()
_worker_thread = None
_queued_job = None
_last_observed_enabled = bool(
    (get_automation_config(EMAIL_AUTOMATION_FLAG) or {}).get("enabled", False)
)


class ReleaseMonitorEmailError(RuntimeError):
    """Safe, user-facing email notification error."""


def _default_state() -> Dict:
    return {
        "version": 1,
        "tracking_state": "uninitialized",
        "week_key": "",
        "notified_row_keys": [],
        "active_row_keys": [],
        "responsible_row_keys": [],
        "pending_events": {},
        "last_evaluated_at": "",
        "last_email_attempt_at": "",
        "last_email_success_at": "",
        "last_snapshot_revision": "",
        "last_result": "",
        "last_error": "",
        "last_sent_fingerprint": "",
        "last_email_subject": "",
        "last_email_recipients": [],
        "last_email_new_count": 0,
        "last_email_total_count": 0,
        "last_email_responsible_removed_count": 0,
    }


def _normalize_row_keys(values: Iterable[str]) -> List[str]:
    return sorted(
        {
            str(value or "").strip()
            for value in (values or [])
            if str(value or "").strip()
        }
    )


def _normalize_pending_events(value) -> Dict[str, Dict[str, str]]:
    normalized = {}
    if not isinstance(value, dict):
        return normalized
    for row_key, raw_event in value.items():
        clean_key = str(row_key or "").strip()
        if not clean_key:
            continue
        raw_event = raw_event if isinstance(raw_event, dict) else {}
        event_type = str(raw_event.get("event_type") or EVENT_NEW_UNASSIGNED).strip()
        if event_type not in VALID_EVENT_TYPES:
            event_type = EVENT_NEW_UNASSIGNED
        normalized[clean_key] = {
            "event_type": event_type,
            "detected_at": str(raw_event.get("detected_at") or "").strip(),
        }
    return normalized


def _normalize_state(payload) -> Dict:
    state = _default_state()
    if isinstance(payload, dict):
        for key in state:
            if key in payload:
                state[key] = payload[key]
    for key in ("notified_row_keys", "active_row_keys", "responsible_row_keys"):
        state[key] = _normalize_row_keys(state.get(key))
    state["pending_events"] = _normalize_pending_events(state.get("pending_events"))
    state["last_email_recipients"] = _normalize_recipients(
        state.get("last_email_recipients"),
        strict=False,
    )
    for key in (
        "last_email_new_count",
        "last_email_total_count",
        "last_email_responsible_removed_count",
    ):
        try:
            state[key] = max(0, int(state.get(key) or 0))
        except (TypeError, ValueError):
            state[key] = 0
    state["version"] = 1
    return state


def _load_state() -> Tuple[Dict, bool]:
    if not NOTIFY_STATE_FILE.exists():
        return _default_state(), False
    try:
        with NOTIFY_STATE_FILE.open("r", encoding="utf-8-sig") as handle:
            return _normalize_state(json.load(handle)), True
    except Exception as exc:
        logging.error("Release monitor email: failed to read notify state: %s", exc)
        return _default_state(), False


def _atomic_write_state(state: Dict) -> None:
    NOTIFY_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    temp_path = NOTIFY_STATE_FILE.with_name(
        f".{NOTIFY_STATE_FILE.name}.{uuid4().hex}.tmp"
    )
    try:
        with temp_path.open("w", encoding="utf-8") as handle:
            json.dump(_normalize_state(state), handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, NOTIFY_STATE_FILE)
    finally:
        if temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass


def _automation_settings() -> Dict:
    config = get_automation_config(EMAIL_AUTOMATION_FLAG)
    if not isinstance(config, dict):
        return {"enabled": False, "recipients": []}
    return {
        "enabled": bool(config.get("enabled", False)),
        "recipients": _normalize_recipients(config.get("recipients"), strict=False),
    }


def _normalize_recipients(values, *, strict: bool) -> List[str]:
    if not isinstance(values, (list, tuple, set)):
        values = []
    recipients = []
    seen = set()
    for value in values:
        address = str(value or "").strip()
        if not address:
            continue
        parsed = parseaddr(address)[1]
        is_valid = (
            parsed == address
            and "@" in address
            and "\r" not in address
            and "\n" not in address
        )
        if not is_valid:
            if strict:
                raise ReleaseMonitorEmailError(
                    f"Некорректный адрес получателя: {address}"
                )
            continue
        key = address.casefold()
        if key not in seen:
            seen.add(key)
            recipients.append(address)
    return recipients


def _as_bool(value, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _mail_settings() -> Dict:
    return {
        "host": str(
            os.getenv("MAIL_SMTP_HOST")
            or TOKENS.get("mail_smtp_host")
            or ""
        ).strip(),
        "port": int(
            os.getenv("MAIL_SMTP_PORT")
            or TOKENS.get("mail_smtp_port")
            or 587
        ),
        "username": str(
            os.getenv("MAIL_USERNAME")
            or TOKENS.get("mail_username")
            or ""
        ).strip(),
        "password": str(
            os.getenv("MAIL_PASSWORD")
            or TOKENS.get("mail_password")
            or ""
        ),
        "sender": str(
            os.getenv("MAIL_FROM")
            or TOKENS.get("mail_from")
            or ""
        ).strip(),
        "ssl_verify": _as_bool(
            os.getenv("MAIL_SSL_VERIFY"),
            default=_as_bool(TOKENS.get("mail_ssl_verify"), default=False),
        ),
        "public_url": str(
            os.getenv("RELEASE_MONITOR_PUBLIC_BASE_URL")
            or TOKENS.get("release_monitor_public_base_url")
            or ""
        ).strip(),
    }


def _validate_delivery_settings(settings: Dict, recipients: List[str]) -> None:
    missing = [
        name
        for name in ("host", "username", "password", "sender")
        if not settings.get(name)
    ]
    if missing:
        raise ReleaseMonitorEmailError(
            "Не заполнены настройки почты: " + ", ".join(missing)
        )
    if not 1 <= int(settings["port"]) <= 65535:
        raise ReleaseMonitorEmailError("Указан некорректный SMTP-порт.")
    if not _normalize_recipients([settings["sender"]], strict=False):
        raise ReleaseMonitorEmailError("Указан некорректный адрес отправителя.")
    if not recipients:
        raise ReleaseMonitorEmailError(
            "В feature_flags.json не указаны получатели email-уведомлений."
        )
    parsed_url = urlparse(settings.get("public_url") or "")
    if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
        raise ReleaseMonitorEmailError(
            "В config.json не настроен абсолютный release_monitor_public_base_url."
        )


def _format_timestamp(value: Optional[datetime] = None) -> str:
    return (value or datetime.now()).astimezone().isoformat(timespec="seconds")


def _parse_timestamp(value: str) -> Optional[datetime]:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
        return parsed.astimezone().replace(tzinfo=None)
    except (TypeError, ValueError):
        return None


def _current_week_key(value: Optional[datetime] = None) -> str:
    iso_year, iso_week, _ = (value or datetime.now()).isocalendar()
    return f"{iso_year}-W{iso_week:02d}"


def _current_week_period(value: Optional[datetime] = None) -> str:
    current = value or datetime.now()
    start = current.date() - timedelta(days=current.weekday())
    end = start + timedelta(days=6)
    return f"{start.strftime('%d.%m.%Y')} - {end.strftime('%d.%m.%Y')}"


def _operational_assignment_period(value: Optional[datetime] = None) -> str:
    current = value or datetime.now()
    start = current.date() - timedelta(days=current.weekday())
    next_monday = start + timedelta(days=7)
    return f"{start.strftime('%d.%m.%Y')} - {next_monday.strftime('%d.%m.%Y')}"


def _snapshot_revision(snapshot: Dict) -> str:
    meta = dict((snapshot or {}).get("meta") or {})
    return str(
        meta.get("data_revision")
        or meta.get("accepted_revision")
        or ""
    ).strip()


def _snapshot_label(snapshot: Dict) -> str:
    meta = dict((snapshot or {}).get("meta") or {})
    raw_value = str(
        meta.get("accepted_at")
        or meta.get("last_updated")
        or meta.get("last_full_sync")
        or ""
    ).strip()
    if not raw_value:
        return "не указано"
    try:
        parsed = datetime.fromisoformat(raw_value.replace("Z", "+00:00"))
        return parsed.astimezone().strftime("%d.%m.%Y %H:%M:%S")
    except ValueError:
        return raw_value


def _has_responsible(item: Dict) -> bool:
    responsibles = item.get("psi_responsibles") or []
    if not isinstance(responsibles, list):
        responsibles = [responsibles] if responsibles else []
    return any(str(value or "").strip() for value in responsibles)


def _is_current_week_active(item: Dict) -> bool:
    release_dt = _parse_item_date(item)
    if release_dt == datetime.max:
        return False
    release_date = release_dt.date()
    current = datetime.now()
    week_start = current.date() - timedelta(days=current.weekday())
    week_end = week_start + timedelta(days=6)
    next_monday = week_end + timedelta(days=1)
    return bool(
        (week_start <= release_date <= week_end or release_date == next_monday)
        and not item.get("is_cancelled")
        and not item.get("is_final")
        and str(item.get("row_key") or "").strip()
    )


def select_unassigned_current_week_items(items: Iterable[Dict]) -> List[Dict]:
    selected = []
    for source_index, item in enumerate(items or []):
        if not isinstance(item, dict) or not _is_current_week_active(item):
            continue
        if not item.get("is_missing_week_responsible") or _has_responsible(item):
            continue
        selected_item = dict(item)
        selected_item["_notification_source_index"] = source_index
        selected.append(selected_item)
    return selected


def _current_assignment_sets(items: Iterable[Dict]) -> Tuple[Set[str], Set[str]]:
    missing = set()
    responsible = set()
    for item in items or []:
        if not isinstance(item, dict) or not _is_current_week_active(item):
            continue
        row_key = str(item.get("row_key") or "").strip()
        if _has_responsible(item):
            responsible.add(row_key)
        elif item.get("is_missing_week_responsible"):
            missing.add(row_key)
    return missing, responsible


def _row_key_fingerprint(row_keys: Iterable[str]) -> str:
    payload = "\n".join(sorted(set(row_keys or []))).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _parse_item_date(item: Dict) -> datetime:
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
                return datetime.strptime(candidate, fmt)
            except ValueError:
                continue
    return datetime.max


def _sort_items(items: Iterable[Dict], event_row_keys: Set[str]) -> List[Dict]:
    return sorted(
        items or [],
        key=lambda item: (
            0 if str(item.get("row_key") or "").strip() in event_row_keys else 1,
            _parse_item_date(item),
            int(item.get("_notification_source_index") or 0),
        ),
    )


def _event_priority(event_type: str) -> int:
    return 2 if event_type == EVENT_RESPONSIBLE_REMOVED else 1


def _merge_event(
    events: Dict[str, Dict[str, str]],
    row_key: str,
    event_type: str,
    detected_at: Optional[str] = None,
) -> None:
    if event_type not in VALID_EVENT_TYPES:
        return
    row_key = str(row_key or "").strip()
    if not row_key:
        return
    current = events.get(row_key) or {}
    current_type = str(current.get("event_type") or "")
    if _event_priority(event_type) < _event_priority(current_type):
        return
    events[row_key] = {
        "event_type": event_type,
        "detected_at": (
            str(detected_at or current.get("detected_at") or "").strip()
            or _format_timestamp()
        ),
    }


def _create_baseline(
    state: Dict,
    *,
    snapshot: Dict,
    missing_keys: Set[str],
    responsible_keys: Set[str],
    week_key: str,
    result: str,
) -> None:
    state.update(
        {
            "tracking_state": "active",
            "week_key": week_key,
            "notified_row_keys": sorted(missing_keys),
            "active_row_keys": sorted(missing_keys),
            "responsible_row_keys": sorted(responsible_keys),
            "pending_events": {},
            "last_evaluated_at": _format_timestamp(),
            "last_snapshot_revision": _snapshot_revision(snapshot),
            "last_result": result,
            "last_error": "",
        }
    )
    _atomic_write_state(state)


def _acquire_file_lock() -> Optional[int]:
    EMAIL_LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(2):
        try:
            descriptor = os.open(
                EMAIL_LOCK_FILE,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            )
            os.write(
                descriptor,
                f"{os.getpid()} {_format_timestamp()}".encode("utf-8"),
            )
            return descriptor
        except FileExistsError:
            try:
                age_seconds = time.time() - EMAIL_LOCK_FILE.stat().st_mtime
            except OSError:
                return None
            if age_seconds <= EMAIL_LOCK_STALE_SECONDS:
                return None
            try:
                EMAIL_LOCK_FILE.unlink()
                logging.warning("Release monitor email: removed stale lock file")
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
        EMAIL_LOCK_FILE.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        logging.warning("Release monitor email: failed to remove lock file: %s", exc)


def _display(value, fallback: str = "—") -> str:
    clean_value = str(value or "").strip()
    return clean_value or fallback


def _owner_label(item: Dict) -> str:
    owner = _display(item.get("psi_owner"), "")
    if not owner:
        return "—"
    source = str(item.get("psi_owner_source") or "").strip()
    prefix = "Устанавливает" if source == "manual_text" else "Дежурный"
    return f"{prefix}: {owner}"


def _event_badge(event_type: str) -> Tuple[str, str, str]:
    if event_type == EVENT_RESPONSIBLE_REMOVED:
        return (
            "Требует повторного назначения",
            "#991b1b",
            "#fee2e2",
        )
    return ("Новый", "#1d4ed8", "#dbeafe")


def _event_count_label(count: int) -> str:
    count = abs(int(count or 0))
    if count % 10 == 1 and count % 100 != 11:
        return f"{count} новое событие"
    if count % 10 in {2, 3, 4} and count % 100 not in {12, 13, 14}:
        return f"{count} новых события"
    return f"{count} новых событий"


def _html_link(url, label) -> str:
    clean_url = str(url or "").strip()
    if not clean_url:
        return "—"
    return (
        f'<a href="{html.escape(clean_url, quote=True)}" '
        'style="color:#2563eb;text-decoration:none;font-weight:600;">'
        f"{html.escape(_display(label, 'Открыть'))}</a>"
    )


def _render_table(
    items: List[Dict],
    pending_events: Dict[str, Dict[str, str]],
    *,
    show_event_badge: bool,
) -> str:
    rows = []
    for item in items:
        row_key = str(item.get("row_key") or "").strip()
        event_type = str((pending_events.get(row_key) or {}).get("event_type") or "")
        badge = ""
        row_background = "#ffffff"
        if show_event_badge and event_type:
            label, colour, background = _event_badge(event_type)
            badge = (
                f'<span style="display:inline-block;margin-left:6px;padding:2px 7px;'
                f"border-radius:10px;background:{background};color:{colour};"
                'font-size:11px;font-weight:700;white-space:nowrap;">'
                f"{html.escape(label)}</span>"
            )
            row_background = "#f8fbff" if event_type == EVENT_NEW_UNASSIGNED else "#fff8f8"
        release_key = _display(item.get("release_key"))
        release_summary = _display(item.get("release_summary"), "")
        release_cell = f"<strong>{html.escape(release_key)}</strong>{badge}"
        if release_summary:
            release_cell += (
                '<div style="margin-top:3px;color:#475569;font-size:12px;line-height:1.35;">'
                f"{html.escape(release_summary)}</div>"
            )
        jira_links = "<br>".join(
            value
            for value in (
                _html_link(item.get("release_url"), release_key),
                _html_link(item.get("rov_url"), _display(item.get("rov_key"))),
            )
            if value != "—"
        ) or "—"
        values = (
            _display(item.get("deployment_start")),
            release_cell,
            html.escape(_display(item.get("rov_key"))),
            html.escape(_display(item.get("ke"))),
            html.escape(_display(item.get("release_version"))),
            html.escape(_owner_label(item)),
            jira_links,
        )
        cells = "".join(
            f'<td style="padding:9px 10px;border:1px solid #d8e0ea;'
            f'vertical-align:top;color:#1f2937;font-size:12px;line-height:1.4;">{value}</td>'
            for value in values
        )
        rows.append(f'<tr style="background:{row_background};">{cells}</tr>')

    headers = (
        "Дата внедрения",
        "Релиз",
        "РОВ",
        "КЭ",
        "Версия",
        "Дежурный",
        "Jira",
    )
    header_html = "".join(
        '<th style="padding:9px 10px;border:1px solid #cbd5e1;'
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


def _render_release_cards(
    items: List[Dict],
    pending_events: Dict[str, Dict[str, str]],
    *,
    show_event_badge: bool,
) -> str:
    cards = []
    for item in items:
        row_key = str(item.get("row_key") or "").strip()
        event_type = str((pending_events.get(row_key) or {}).get("event_type") or "")
        badge = ""
        card_border = "#d8e0ea"
        card_background = "#ffffff"
        if show_event_badge and event_type:
            label, colour, background = _event_badge(event_type)
            badge = (
                f'<span style="display:inline-block;padding:3px 8px;'
                f"border-radius:10px;background:{background};color:{colour};"
                'font-size:11px;font-weight:700;line-height:14px;white-space:nowrap;">'
                f"{html.escape(label)}</span>"
            )
            card_border = "#93c5fd" if event_type == EVENT_NEW_UNASSIGNED else "#fecaca"
            card_background = "#f8fbff" if event_type == EVENT_NEW_UNASSIGNED else "#fff8f8"

        release_key = _display(item.get("release_key"))
        release_summary = _display(item.get("release_summary"), "")
        rov_key = _display(item.get("rov_key"))
        release_label = _html_link(item.get("release_url"), release_key)
        rov_label = _html_link(item.get("rov_url"), rov_key)
        if release_label == "—":
            release_label = f"<strong>{html.escape(release_key)}</strong>"
        if rov_label == "—":
            rov_label = html.escape(rov_key)

        cards.append(
            f"""
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="width:100%;border-collapse:collapse;margin:0 0 7px 0;background:{card_background};border:1px solid {card_border};">
  <tr>
    <td style="padding:8px 10px 7px 10px;">
      <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="width:100%;border-collapse:collapse;">
        <tr>
          <td style="vertical-align:top;padding:0 8px 0 0;">
            <div style="font-size:14px;line-height:18px;font-weight:700;color:#0f172a;mso-line-height-rule:exactly;">{release_label}</div>
            <div style="margin-top:2px;font-size:12px;line-height:15px;color:#475569;mso-line-height-rule:exactly;">{html.escape(release_summary or "—")}</div>
          </td>
          <td align="right" style="vertical-align:top;width:190px;padding:0 0 0 8px;">{badge}</td>
        </tr>
        <tr>
          <td colspan="2" style="vertical-align:top;padding:6px 0 0 0;border-top:1px solid #e2e8f0;">
            <div style="font-size:12px;line-height:16px;color:#111827;mso-line-height-rule:exactly;">
              <span style="color:#64748b;">Дата:</span> <strong>{html.escape(_display(item.get("deployment_start")))}</strong>
              <span style="color:#94a3b8;"> · </span><span style="color:#64748b;">РОВ:</span> {rov_label}
              <span style="color:#94a3b8;"> · </span><span style="color:#64748b;">КЭ:</span> {html.escape(_display(item.get("ke")))}
              <span style="color:#94a3b8;"> · </span><span style="color:#64748b;">Версия:</span> {html.escape(_display(item.get("release_version")))}
              <span style="color:#94a3b8;"> · </span>{html.escape(_owner_label(item))}
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
        'background:#f8fafc;color:#64748b;font-size:13px;">Нет строк для отображения.</div>'
    )


def build_unassigned_email_content(
    snapshot: Dict,
    pending_events: Dict[str, Dict[str, str]],
    recipients: List[str],
) -> Tuple[str, str, str, Dict]:
    selected_items = select_unassigned_current_week_items(
        (snapshot or {}).get("items") or []
    )
    current_by_key = {
        str(item.get("row_key") or "").strip(): item
        for item in selected_items
        if str(item.get("row_key") or "").strip()
    }
    pending_events = {
        row_key: event
        for row_key, event in _normalize_pending_events(pending_events).items()
        if row_key in current_by_key
    }
    event_keys = set(pending_events)
    sorted_items = _sort_items(selected_items, event_keys)
    visible_items = sorted_items[:MAX_EMAIL_ROWS]
    visible_event_items = [
        item
        for item in visible_items
        if str(item.get("row_key") or "").strip() in event_keys
    ]
    total_count = len(sorted_items)
    current_keys = set(current_by_key)
    events_match_current_list = bool(event_keys) and event_keys == current_keys
    event_count = len(event_keys)
    removed_count = sum(
        1
        for event in pending_events.values()
        if event.get("event_type") == EVENT_RESPONSIBLE_REMOVED
    )
    generated_at = datetime.now().astimezone()
    period = _operational_assignment_period(generated_at)
    snapshot_label = _snapshot_label(snapshot)
    subject = (
        f"[Блок релизов] Требуется назначение ответственных: "
        f"{_event_count_label(event_count)}"
    )
    public_url = _mail_settings()["public_url"]

    limit_notice = ""
    if total_count > MAX_EMAIL_ROWS:
        limit_notice = (
            f'<div style="margin:14px 0;padding:11px 14px;border-left:4px solid #f59e0b;'
            'background:#fff7ed;color:#7c2d12;font-size:13px;">'
            f"Показаны первые {MAX_EMAIL_ROWS} записей из {total_count}. "
            "Для просмотра полного списка откройте Центр назначений.</div>"
        )

    if events_match_current_list:
        event_html_section = (
            '<div style="margin:4px 0 10px;font-size:18px;font-weight:700;color:#0f172a;">'
            "Релизы без ответственного</div>"
            '<div style="margin:0 0 12px 0;padding:10px 12px;border-left:4px solid #2563eb;'
            'background:#eff6ff;color:#1e3a8a;font-size:13px;line-height:18px;">'
            "Все текущие релизы без ответственного являются событиями этого уведомления.</div>"
            f"{limit_notice}"
            f"{_render_release_cards(visible_items, pending_events, show_event_badge=True)}"
        )
    else:
        event_html_section = (
            '<div style="margin:4px 0 10px;font-size:18px;font-weight:700;color:#0f172a;">'
            "Что изменилось</div>"
            f"{_render_release_cards(visible_event_items, pending_events, show_event_badge=True)}"
            '<div style="margin:22px 0 10px;font-size:18px;font-weight:700;color:#0f172a;">'
            "Актуальный список без ответственного</div>"
            f"{limit_notice}"
            f"{_render_release_cards(visible_items, pending_events, show_event_badge=True)}"
        )
    summary_html = f"""
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="width:100%;border-collapse:collapse;">
  <tr>
    <td style="padding:10px;border:1px solid #d8e0ea;background:#f8fafc;width:50%;">
      <div style="font-size:11px;line-height:14px;color:#64748b;mso-line-height-rule:exactly;">НОВЫХ СОБЫТИЙ</div>
      <div style="font-size:23px;line-height:28px;font-weight:700;color:#2563eb;mso-line-height-rule:exactly;">{event_count}</div>
    </td>
    <td style="padding:10px;border:1px solid #d8e0ea;background:#f8fafc;width:50%;">
      <div style="font-size:11px;line-height:14px;color:#64748b;mso-line-height-rule:exactly;">ВСЕГО БЕЗ ОТВЕТСТВЕННОГО</div>
      <div style="font-size:23px;line-height:28px;font-weight:700;color:#0f172a;mso-line-height-rule:exactly;">{total_count}</div>
    </td>
  </tr>
  <tr>
    <td style="padding:10px;border:1px solid #d8e0ea;background:#f8fafc;width:50%;">
      <div style="font-size:11px;line-height:14px;color:#64748b;mso-line-height-rule:exactly;">ПЕРИОД</div>
      <div style="font-size:14px;line-height:18px;font-weight:700;color:#0f172a;mso-line-height-rule:exactly;">{html.escape(period)}</div>
    </td>
    <td style="padding:10px;border:1px solid #d8e0ea;background:#f8fafc;width:50%;">
      <div style="font-size:11px;line-height:14px;color:#64748b;mso-line-height-rule:exactly;">СФОРМИРОВАНО</div>
      <div style="font-size:14px;line-height:18px;font-weight:700;color:#0f172a;mso-line-height-rule:exactly;">{generated_at.strftime('%d.%m.%Y %H:%M')}</div>
    </td>
  </tr>
</table>
""".strip()

    html_body = f"""<!doctype html>
<html lang="ru">
<body style="margin:0;padding:0;background:#f3f6fa;font-family:Arial,sans-serif;color:#1f2937;">
  <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#f3f6fa;">
    <tr><td align="center" style="padding:24px 12px;">
      <table role="presentation" width="760" cellspacing="0" cellpadding="0" style="width:100%;max-width:760px;background:#ffffff;border:1px solid #d8e0ea;">
        <tr><td style="padding:22px 26px;background:#14213d;color:#ffffff;">
          <div style="font-size:15px;font-weight:700;color:#93c5fd;">Блок релизов</div>
          <div style="margin-top:6px;font-size:23px;font-weight:700;line-height:1.25;">Требуется назначение ответственных по ближайшим релизам</div>
          <div style="margin-top:10px;color:#dbeafe;font-size:13px;line-height:1.5;">В Блоке релизов обнаружены релизы текущей недели или следующего понедельника, для которых не назначен ответственный исполнитель. Просьба выполнить назначение в Центре назначений или в Блоке релизов.</div>
        </td></tr>
        <tr><td style="padding:18px 26px 4px;">
          {summary_html}
          <div style="margin-top:8px;color:#64748b;font-size:12px;">Подтвержденный снимок данных: {html.escape(snapshot_label)}</div>
          <div style="padding:18px 0;text-align:center;">
            <a href="{html.escape(public_url, quote=True)}" style="display:inline-block;padding:11px 22px;background:#2563eb;color:#ffffff;text-decoration:none;font-size:14px;font-weight:700;border-radius:4px;">Открыть Центр назначений</a>
          </div>
        </td></tr>
        <tr><td style="padding:0 26px 20px;">
          {event_html_section}
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
        "Требуется назначение ответственных по ближайшим релизам",
        "",
        (
            "В Блоке релизов обнаружены релизы текущей недели или следующего понедельника, для которых "
            "не назначен ответственный исполнитель."
        ),
        "Просьба выполнить назначение в Центре назначений или в Блоке релизов.",
        "",
        f"Новых событий: {event_count}",
        f"Всего без ответственного: {total_count}",
        f"Период: {period}",
        f"Сформировано: {generated_at.strftime('%d.%m.%Y %H:%M:%S')}",
        f"Подтвержденный снимок данных: {snapshot_label}",
        f"Центр назначений: {public_url}",
        "",
        "Новые события:",
    ]
    for item in visible_event_items:
        row_key = str(item.get("row_key") or "").strip()
        event_type = (pending_events.get(row_key) or {}).get("event_type")
        event_label = _event_badge(event_type)[0]
        text_lines.extend(
            [
                (
                    f"- [{event_label}] {_display(item.get('deployment_start'))} | "
                    f"{_display(item.get('release_key'))} | "
                    f"РОВ: {_display(item.get('rov_key'))} | "
                    f"КЭ: {_display(item.get('ke'))} | "
                    f"Версия: {_display(item.get('release_version'))} | "
                    f"{_owner_label(item)}"
                ),
                f"  Jira Release: {_display(item.get('release_url'))}",
                f"  Jira РОВ: {_display(item.get('rov_url'))}",
            ]
        )
    text_lines.extend(["", "Текущий список без ответственного:"])
    for item in visible_items:
        text_lines.extend(
            [
                (
                    f"- {_display(item.get('deployment_start'))} | "
                    f"{_display(item.get('release_key'))} | "
                    f"РОВ: {_display(item.get('rov_key'))} | "
                    f"КЭ: {_display(item.get('ke'))} | "
                    f"Версия: {_display(item.get('release_version'))} | "
                    f"{_owner_label(item)}"
                ),
                f"  Jira Release: {_display(item.get('release_url'))}",
                f"  Jira РОВ: {_display(item.get('rov_url'))}",
            ]
        )
    if total_count > MAX_EMAIL_ROWS:
        text_lines.extend(
            [
                "",
                (
                    f"Показаны первые {MAX_EMAIL_ROWS} записей из {total_count}. "
                    "Для просмотра полного списка откройте Центр назначений."
                ),
            ]
        )
    text_lines.extend(
        [
            "",
            "Автоматическое уведомление системы Блок релизов.",
            "Отвечать на данное письмо не требуется.",
        ]
    )
    metadata = {
        "event_count": event_count,
        "total_count": total_count,
        "responsible_removed_count": removed_count,
        "recipients": list(recipients),
    }
    return subject, "\n".join(text_lines), html_body, metadata


def _send_email_message(
    *,
    settings: Dict,
    recipients: List[str],
    subject: str,
    text_body: str,
    html_body: str,
) -> None:
    context = (
        ssl.create_default_context()
        if settings.get("ssl_verify")
        else ssl._create_unverified_context()
    )
    if not settings.get("ssl_verify"):
        logging.warning(
            "Release monitor email: SMTP certificate verification is disabled"
        )

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = settings["sender"]
    message["To"] = ", ".join(recipients)
    message.set_content(text_body)
    message.add_alternative(html_body, subtype="html")

    try:
        with smtplib.SMTP(
            settings["host"],
            settings["port"],
            timeout=30,
        ) as server:
            server.ehlo()
            server.starttls(context=context)
            server.ehlo()
            server.login(settings["username"], settings["password"])
            server.send_message(
                message,
                from_addr=settings["sender"],
                to_addrs=recipients,
            )
    except smtplib.SMTPAuthenticationError as exc:
        raise ReleaseMonitorEmailError(
            "SMTP отклонил учетные данные отправителя."
        ) from exc
    except smtplib.SMTPRecipientsRefused as exc:
        raise ReleaseMonitorEmailError(
            "SMTP отклонил адресатов уведомления."
        ) from exc
    except smtplib.SMTPSenderRefused as exc:
        raise ReleaseMonitorEmailError(
            "SMTP отклонил адрес отправителя."
        ) from exc
    except (smtplib.SMTPException, OSError, ssl.SSLError) as exc:
        raise ReleaseMonitorEmailError(
            f"Не удалось отправить письмо через SMTP: {type(exc).__name__}."
        ) from exc


def _latest_snapshot() -> Dict:
    from services.release_monitor_service import get_release_monitor_snapshot

    return get_release_monitor_snapshot() or {}


def _run_notification(
    snapshot: Dict,
    *,
    refresh_mode: str,
    force_baseline: bool,
    explicit_events: Optional[Dict[str, str]],
) -> Dict:
    settings_config = _automation_settings()
    if not settings_config["enabled"]:
        return {"result": "disabled"}

    with _process_lock:
        descriptor = _acquire_file_lock()
        if descriptor is None:
            return {"result": "locked"}
        try:
            items = list((snapshot or {}).get("items") or [])
            current_missing, current_responsible = _current_assignment_sets(items)
            state, state_exists = _load_state()
            week_key = _current_week_key()
            is_refresh = refresh_mode in {
                "full",
                "reliable_full",
                "quick",
                "silent",
            }

            if not state_exists or state.get("tracking_state") != "active":
                if not is_refresh:
                    return {"result": "waiting_refresh"}
                _create_baseline(
                    state,
                    snapshot=snapshot,
                    missing_keys=current_missing,
                    responsible_keys=current_responsible,
                    week_key=week_key,
                    result="baseline_created",
                )
                return {
                    "result": "baseline_created",
                    "rows_count": len(current_missing),
                }

            if force_baseline or state.get("week_key") != week_key:
                if not is_refresh:
                    return {"result": "waiting_refresh"}
                _create_baseline(
                    state,
                    snapshot=snapshot,
                    missing_keys=current_missing,
                    responsible_keys=current_responsible,
                    week_key=week_key,
                    result=(
                        "weekly_baseline_created"
                        if state.get("week_key") != week_key
                        else "baseline_created"
                    ),
                )
                return {
                    "result": state["last_result"],
                    "rows_count": len(current_missing),
                }

            now = datetime.now()
            now_text = _format_timestamp(now)
            notified = set(_normalize_row_keys(state.get("notified_row_keys")))
            previous_responsible = set(
                _normalize_row_keys(state.get("responsible_row_keys"))
            )
            pending = {
                row_key: event
                for row_key, event in _normalize_pending_events(
                    state.get("pending_events")
                ).items()
                if row_key in current_missing
            }

            for row_key in current_missing - notified:
                _merge_event(pending, row_key, EVENT_NEW_UNASSIGNED, now_text)
            for row_key in current_missing & previous_responsible:
                _merge_event(pending, row_key, EVENT_RESPONSIBLE_REMOVED, now_text)
            for row_key, event_type in (explicit_events or {}).items():
                if row_key in current_missing:
                    _merge_event(pending, row_key, event_type, now_text)

            state.update(
                {
                    "active_row_keys": sorted(current_missing),
                    "responsible_row_keys": sorted(current_responsible),
                    "pending_events": pending,
                    "last_evaluated_at": now_text,
                    "last_snapshot_revision": _snapshot_revision(snapshot),
                }
            )
            if not pending:
                state["last_result"] = "waiting"
                state["last_error"] = ""
                _atomic_write_state(state)
                return {"result": "waiting", "rows_count": len(current_missing)}

            latest_snapshot = _latest_snapshot()
            latest_items = list((latest_snapshot or {}).get("items") or [])
            latest_missing, latest_responsible = _current_assignment_sets(latest_items)
            pending = {
                row_key: event
                for row_key, event in pending.items()
                if row_key in latest_missing
            }
            for row_key in latest_missing - notified:
                _merge_event(pending, row_key, EVENT_NEW_UNASSIGNED, now_text)
            state.update(
                {
                    "active_row_keys": sorted(latest_missing),
                    "responsible_row_keys": sorted(latest_responsible),
                    "pending_events": pending,
                    "last_evaluated_at": now_text,
                    "last_snapshot_revision": _snapshot_revision(latest_snapshot),
                }
            )
            if not pending:
                state["last_result"] = "waiting"
                state["last_error"] = ""
                _atomic_write_state(state)
                return {"result": "waiting", "rows_count": len(latest_missing)}

            current_fingerprint = _row_key_fingerprint(latest_missing)
            only_new_events = all(
                event.get("event_type") == EVENT_NEW_UNASSIGNED
                for event in pending.values()
            )
            if (
                only_new_events
                and current_fingerprint
                and current_fingerprint == state.get("last_sent_fingerprint")
            ):
                notified.update(pending)
                state.update(
                    {
                        "notified_row_keys": sorted(notified),
                        "pending_events": {},
                        "last_result": "waiting",
                        "last_error": "",
                    }
                )
                _atomic_write_state(state)
                return {"result": "duplicate_suppressed"}

            last_attempt = _parse_timestamp(state.get("last_email_attempt_at"))
            if (
                last_attempt is not None
                and (now - last_attempt).total_seconds() < EMAIL_THROTTLE_SECONDS
            ):
                state["last_result"] = "throttled"
                _atomic_write_state(state)
                return {
                    "result": "throttled",
                    "pending_count": len(pending),
                }

            recipients = _normalize_recipients(
                settings_config["recipients"],
                strict=True,
            )
            delivery_settings = _mail_settings()
            state.update(
                {
                    "last_email_attempt_at": now_text,
                    "last_result": "running",
                    "last_error": "",
                }
            )
            _atomic_write_state(state)

            try:
                _validate_delivery_settings(delivery_settings, recipients)
                subject, text_body, html_body, metadata = (
                    build_unassigned_email_content(
                        latest_snapshot,
                        pending,
                        recipients,
                    )
                )
                _send_email_message(
                    settings=delivery_settings,
                    recipients=recipients,
                    subject=subject,
                    text_body=text_body,
                    html_body=html_body,
                )
            except Exception as exc:
                safe_error = (
                    str(exc)
                    if isinstance(exc, ReleaseMonitorEmailError)
                    else f"Ошибка email-уведомления: {type(exc).__name__}."
                )
                state.update(
                    {
                        "pending_events": pending,
                        "last_result": "error",
                        "last_error": safe_error,
                    }
                )
                _atomic_write_state(state)
                logging.exception("Release monitor email notification failed")
                return {"result": "error", "error": safe_error}

            notified.update(pending)
            success_at = _format_timestamp()
            state.update(
                {
                    "notified_row_keys": sorted(notified),
                    "active_row_keys": sorted(latest_missing),
                    "responsible_row_keys": sorted(latest_responsible),
                    "pending_events": {},
                    "last_email_success_at": success_at,
                    "last_result": "sent",
                    "last_error": "",
                    "last_sent_fingerprint": current_fingerprint,
                    "last_email_subject": subject,
                    "last_email_recipients": recipients,
                    "last_email_new_count": int(metadata["event_count"]),
                    "last_email_total_count": int(metadata["total_count"]),
                    "last_email_responsible_removed_count": int(
                        metadata["responsible_removed_count"]
                    ),
                }
            )
            _atomic_write_state(state)
            logging.info(
                "Release monitor email sent: events=%s total=%s recipients=%s",
                metadata["event_count"],
                metadata["total_count"],
                len(recipients),
            )
            return {
                "result": "sent",
                "event_count": metadata["event_count"],
                "rows_count": metadata["total_count"],
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
            _run_notification(
                job["snapshot"],
                refresh_mode=job["refresh_mode"],
                force_baseline=job.get("force_baseline", False),
                explicit_events=job.get("explicit_events"),
            )
        except Exception:
            logging.exception("Release monitor email worker failed unexpectedly")


def schedule_unassigned_email_notification(
    snapshot: Dict,
    *,
    refresh_mode: str,
    explicit_events: Optional[Dict[str, str]] = None,
) -> bool:
    global _queued_job, _worker_thread, _last_observed_enabled

    automation = _automation_settings()
    enabled = automation["enabled"]
    with _queue_lock:
        previous_enabled = _last_observed_enabled
        _last_observed_enabled = enabled
        if not enabled:
            return False

        force_baseline = previous_enabled is False
        merged_events = {}
        if _queued_job:
            force_baseline = force_baseline or bool(
                _queued_job.get("force_baseline")
            )
            for row_key, event_type in (
                _queued_job.get("explicit_events") or {}
            ).items():
                _merge_event(
                    merged_events,
                    row_key,
                    event_type,
                )
        for row_key, event_type in (explicit_events or {}).items():
            _merge_event(merged_events, row_key, event_type)

        _queued_job = {
            "snapshot": {
                "items": copy.deepcopy(list((snapshot or {}).get("items") or [])),
                "meta": copy.deepcopy(dict((snapshot or {}).get("meta") or {})),
            },
            "refresh_mode": str(refresh_mode or ""),
            "force_baseline": force_baseline,
            "explicit_events": {
                row_key: event["event_type"]
                for row_key, event in merged_events.items()
            },
        }
        if _worker_thread and _worker_thread.is_alive():
            return True
        _worker_thread = threading.Thread(
            target=_worker_loop,
            daemon=True,
            name="release-monitor-unassigned-email",
        )
        _worker_thread.start()
        return True


def get_unassigned_email_status() -> Dict:
    global _last_observed_enabled

    automation = _automation_settings()
    enabled = automation["enabled"]
    if not enabled:
        with _queue_lock:
            _last_observed_enabled = False
    state, state_exists = _load_state()

    if not enabled:
        status = "disabled"
    elif not state_exists:
        status = "waiting_refresh"
    elif state.get("last_result") in {
        "baseline_created",
        "weekly_baseline_created",
    }:
        status = "baseline_created"
    elif state.get("last_result") == "error" or state.get("last_error"):
        status = "error"
    elif state.get("last_result") == "running":
        status = "sending"
    elif state.get("last_result") == "sent":
        status = "sent"
    else:
        status = "waiting"

    pending_count = (
        len(state.get("pending_events") or {})
        if state_exists
        else 0
    )
    return {
        "enabled": enabled,
        "status": status,
        "running": bool(_worker_thread and _worker_thread.is_alive()),
        "week_key": state.get("week_key", "") if state_exists else "",
        "last_evaluated_at": (
            state.get("last_evaluated_at", "") if state_exists else ""
        ),
        "last_email_attempt_at": (
            state.get("last_email_attempt_at", "") if state_exists else ""
        ),
        "last_email_success_at": (
            state.get("last_email_success_at", "") if state_exists else ""
        ),
        "last_email_new_count": (
            int(state.get("last_email_new_count") or 0)
            if state_exists
            else 0
        ),
        "last_email_total_count": (
            int(state.get("last_email_total_count") or 0)
            if state_exists
            else 0
        ),
        "pending_count": pending_count,
        "last_result": state.get("last_result", "") if state_exists else "",
        "last_error": state.get("last_error", "") if state_exists else "",
        "last_email_subject": (
            state.get("last_email_subject", "") if state_exists else ""
        ),
        "last_email_recipients": (
            list(state.get("last_email_recipients") or [])
            if state_exists
            else []
        ),
    }
