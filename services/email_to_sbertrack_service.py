import email
import hashlib
import html
import imaplib
import json
import logging
import os
import re
import ssl
import threading
import time
import base64
from datetime import datetime, timedelta, timezone
from email.header import decode_header, make_header
from email.message import Message
from email.utils import getaddresses, parsedate_to_datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import quote
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4

from config import TOKENS
from services.feature_flags_service import (
    get_automation_config,
    get_sbertrack_users_config,
)
from services.release_monitor_email_service import _format_timestamp


EMAIL_TO_SBERTRACK_AUTOMATION_FLAG = "email_to_sbertrack"
STATE_FILE = Path(__file__).resolve().parent.parent / "cache" / "email_to_sbertrack_state.json"
LOCK_FILE = STATE_FILE.with_suffix(".lock")
LOCK_STALE_SECONDS = 900
SUMMARY_MAX_CHARS = 220
DESCRIPTION_BODY_FALLBACK_LIMIT = 6000
MAX_STORED_KEYS = 2000
MAX_DRY_RUN_MATCHES = 500
MAX_MESSAGE_AGE = timedelta(hours=24)
WORKFLOW_STATUS_NEW = {"command": "NEW"}

_process_lock = threading.Lock()
_worker_thread = None
_worker_stop = threading.Event()


class EmailToSberTrackError(RuntimeError):
    pass


def _default_state() -> Dict[str, Any]:
    return {
        "version": 1,
        "last_checked_uid": 0,
        "processed_message_ids": [],
        "created_keys": {},
        "pending": {},
        "dry_run_matches": {},
        "last_checked_at": "",
        "last_success_at": "",
        "last_result": "idle",
        "last_error": "",
        "last_created_count": 0,
        "last_dry_run_count": 0,
        "last_pending_count": 0,
        "last_uid_seen": 0,
    }


def _read_state() -> Dict[str, Any]:
    try:
        with STATE_FILE.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
        if isinstance(payload, dict):
            state = _default_state()
            state.update(payload)
            state["created_keys"] = (
                state.get("created_keys") if isinstance(state.get("created_keys"), dict) else {}
            )
            state["pending"] = state.get("pending") if isinstance(state.get("pending"), dict) else {}
            state["dry_run_matches"] = (
                state.get("dry_run_matches")
                if isinstance(state.get("dry_run_matches"), dict)
                else {}
            )
            state["processed_message_ids"] = (
                state.get("processed_message_ids")
                if isinstance(state.get("processed_message_ids"), list)
                else []
            )
            return state
    except FileNotFoundError:
        pass
    except Exception as exc:
        logging.warning("Email to SberTrack: failed to read state: %s", exc)
    return _default_state()


def _atomic_write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temp_path.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass


def _write_state(state: Dict[str, Any]) -> None:
    _trim_state(state)
    _atomic_write_json(STATE_FILE, state)


def _trim_state(state: Dict[str, Any]) -> None:
    processed = list(state.get("processed_message_ids") or [])
    if len(processed) > MAX_STORED_KEYS:
        state["processed_message_ids"] = processed[-MAX_STORED_KEYS:]

    created = state.get("created_keys") if isinstance(state.get("created_keys"), dict) else {}
    if len(created) > MAX_STORED_KEYS:
        ordered = sorted(
            created.items(),
            key=lambda item: str((item[1] or {}).get("created_at") or ""),
            reverse=True,
        )
        state["created_keys"] = dict(ordered[:MAX_STORED_KEYS])

    dry_run = state.get("dry_run_matches") if isinstance(state.get("dry_run_matches"), dict) else {}
    if len(dry_run) > MAX_DRY_RUN_MATCHES:
        ordered = sorted(
            dry_run.items(),
            key=lambda item: str((item[1] or {}).get("created_at") or ""),
            reverse=True,
        )
        state["dry_run_matches"] = dict(ordered[:MAX_DRY_RUN_MATCHES])


def _acquire_file_lock() -> bool:
    now = time.time()
    try:
        if LOCK_FILE.exists():
            lock_age = now - LOCK_FILE.stat().st_mtime
            if lock_age > LOCK_STALE_SECONDS:
                try:
                    LOCK_FILE.unlink()
                except OSError:
                    pass
        fd = os.open(str(LOCK_FILE), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(f"{os.getpid()} {datetime.now().isoformat(timespec='seconds')}\n")
        return True
    except FileExistsError:
        return False
    except OSError as exc:
        logging.warning("Email to SberTrack: failed to acquire lock: %s", exc)
        return False


def _release_file_lock() -> None:
    try:
        LOCK_FILE.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None or value == "":
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _non_negative_int(value: Any, default: int) -> int:
    if isinstance(value, bool):
        return default
    try:
        normalized = int(value)
    except (TypeError, ValueError):
        return default
    return normalized if normalized >= 0 else default


def _normalize_string_list(value: Any) -> List[str]:
    values = value if isinstance(value, (list, tuple, set)) else [value]
    normalized = []
    seen = set()
    for raw_value in values:
        text = str(raw_value or "").strip()
        key = text.lower()
        if text and key not in seen:
            normalized.append(text)
            seen.add(key)
    return normalized


def _normalize_email(value: Any) -> str:
    return str(value or "").strip().lower()


def _automation_settings() -> Dict[str, Any]:
    config = get_automation_config(EMAIL_TO_SBERTRACK_AUTOMATION_FLAG)
    config = config if isinstance(config, dict) else {}
    routes = config.get("routes") if isinstance(config.get("routes"), list) else []
    normalized_routes = []
    for index, raw_route in enumerate(routes, start=1):
        if not isinstance(raw_route, dict):
            continue
        triggers = _normalize_string_list(raw_route.get("subject_triggers"))
        target_system = str(raw_route.get("target_system") or "sbertrack").strip().lower()
        if target_system not in {"sbertrack", "jira"}:
            target_system = "sbertrack"
        spaces = _normalize_string_list(
            raw_route.get("jira_projects") if target_system == "jira" else raw_route.get("spaces")
        )
        name = str(raw_route.get("name") or f"route_{index}").strip()
        if not name or not triggers or not spaces:
            continue
        normalized_routes.append(
            {
                "enabled": _as_bool(raw_route.get("enabled"), True),
                "name": name,
                "subject_triggers": triggers,
                "target_system": target_system,
                "spaces": spaces,
                "jira_domain": str(raw_route.get("jira_domain") or "sberbank").strip().lower(),
                "jira_issue_type": str(raw_route.get("jira_issue_type") or "Story").strip() or "Story",
                "jira_priority": str(raw_route.get("jira_priority") or "Minor").strip() or "Minor",
                "jira_labels": _normalize_string_list(raw_route.get("jira_labels") or ["MPR"]),
                "jira_team": raw_route.get("jira_team") if isinstance(raw_route.get("jira_team"), dict) else {},
                "suit": str(raw_route.get("suit") or "task").strip() or "task",
                "priority": str(raw_route.get("priority") or "low").strip() or "low",
                "summary_template": str(
                    raw_route.get("summary_template") or "Письмо: {subject}"
                ).strip()
                or "Письмо: {subject}",
            }
        )
    return {
        "enabled": _as_bool(config.get("enabled"), False),
        "dry_run": _as_bool(config.get("dry_run"), True),
        "poll_interval_seconds": _non_negative_int(
            config.get("poll_interval_seconds"), 300
        ),
        "lookback_limit": max(1, _non_negative_int(config.get("lookback_limit"), 20)),
        "max_pending_per_cycle": max(
            1, _non_negative_int(config.get("max_pending_per_cycle"), 10)
        ),
        "body_max_chars": max(
            1000, _non_negative_int(config.get("body_max_chars"), DESCRIPTION_BODY_FALLBACK_LIMIT)
        ),
        "technical_mailboxes": [
            _normalize_email(value) for value in _normalize_string_list(config.get("technical_mailboxes"))
        ],
        "routes": normalized_routes,
    }


def _imap_settings() -> Dict[str, Any]:
    host = str(os.getenv("MAIL_IMAP_HOST") or TOKENS.get("mail_imap_host") or "").strip()
    port = _non_negative_int(os.getenv("MAIL_IMAP_PORT") or TOKENS.get("mail_imap_port"), 993)
    return {
        "host": host,
        "port": port or 993,
        "username": str(
            os.getenv("MAIL_USERNAME") or TOKENS.get("mail_username") or ""
        ).strip(),
        "password": str(os.getenv("MAIL_PASSWORD") or TOKENS.get("mail_password") or ""),
        "ssl_verify": _as_bool(
            os.getenv("MAIL_SSL_VERIFY"),
            default=_as_bool(TOKENS.get("mail_ssl_verify"), default=False),
        ),
        "mail_from": str(os.getenv("MAIL_FROM") or TOKENS.get("mail_from") or "").strip(),
    }


def _sbertrack_settings() -> Dict[str, Any]:
    return {
        "username": str(
            os.getenv("SBERTRACK_USERNAME") or TOKENS.get("sbertrack_username") or ""
        ).strip(),
        "password": str(os.getenv("SBERTRACK_PASSWORD") or TOKENS.get("sbertrack_password") or ""),
        "api_base_url": str(
            os.getenv("SBERTRACK_API_BASE_URL")
            or TOKENS.get("sbertrack_api_base_url")
            or ""
        ).strip(),
        "ui_base_url": str(
            os.getenv("SBERTRACK_UI_BASE_URL")
            or TOKENS.get("sbertrack_ui_base_url")
            or ""
        ).strip(),
        "tenant": str(
            os.getenv("SBERTRACK_TENANT") or TOKENS.get("sbertrack_tenant") or "default"
        ).strip()
        or "default",
        "reporter_id": str(
            os.getenv("SBERTRACK_REPORTER_ID")
            or TOKENS.get("sbertrack_reporter_id")
            or TOKENS.get("sbertrack_reporter")
            or ""
        ).strip(),
        "ssl_verify": _as_bool(
            os.getenv("SBERTRACK_SSL_VERIFY"),
            default=_as_bool(TOKENS.get("sbertrack_ssl_verify"), default=False),
        ),
    }


def _ensure_imap_settings(settings: Dict[str, Any]) -> None:
    missing = [key for key in ("host", "username", "password") if not settings.get(key)]
    if missing:
        raise EmailToSberTrackError(
            "Не заполнены IMAP-настройки для Email → SberTrack: " + ", ".join(missing)
        )
    if not 1 <= int(settings.get("port") or 0) <= 65535:
        raise EmailToSberTrackError("Некорректный IMAP-порт для Email → SberTrack.")


def _ensure_sbertrack_settings(settings: Dict[str, Any]) -> None:
    missing = [
        key
        for key in ("username", "password", "api_base_url", "tenant", "reporter_id")
        if not settings.get(key)
    ]
    if missing:
        raise EmailToSberTrackError(
            "Не заполнены настройки SberTrack: " + ", ".join(missing)
        )


def _decode_header_value(value: Any) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(str(value))))
    except Exception:
        return str(value or "")


def _address_rows(value: Any) -> List[Dict[str, str]]:
    decoded = _decode_header_value(value)
    rows = []
    seen = set()
    for name, address in getaddresses([decoded]):
        clean_address = _normalize_email(address)
        clean_name = _decode_header_value(name).strip()
        if not clean_address:
            continue
        key = clean_address
        if key in seen:
            continue
        seen.add(key)
        rows.append({"name": clean_name, "email": clean_address})
    return rows


def _addresses_display(rows: Iterable[Dict[str, str]]) -> str:
    values = []
    for row in rows or []:
        name = str(row.get("name") or "").strip()
        address = str(row.get("email") or "").strip()
        if name and address:
            values.append(f"{name} <{address}>")
        elif address:
            values.append(address)
    return ", ".join(values)


def _message_date(value: Any) -> str:
    raw_value = _decode_header_value(value).strip()
    if not raw_value:
        return ""
    try:
        return parsedate_to_datetime(raw_value).astimezone().isoformat(timespec="seconds")
    except Exception:
        return raw_value


def _is_recent_message_date(value: Any) -> bool:
    raw_value = str(value or "").strip()
    if not raw_value:
        return False
    try:
        message_datetime = datetime.fromisoformat(raw_value.replace("Z", "+00:00"))
    except ValueError:
        try:
            message_datetime = parsedate_to_datetime(raw_value)
        except (TypeError, ValueError, OverflowError):
            return False
    if message_datetime.tzinfo is None:
        message_datetime = message_datetime.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    return now - message_datetime.astimezone(timezone.utc) <= MAX_MESSAGE_AGE


def _message_body(message: Message, limit: int) -> Tuple[str, bool]:
    plain_parts = []
    html_parts = []
    if message.is_multipart():
        for part in message.walk():
            disposition = str(part.get("Content-Disposition") or "").lower()
            if "attachment" in disposition:
                continue
            content_type = part.get_content_type()
            try:
                payload = part.get_payload(decode=True)
            except Exception:
                payload = None
            if payload is None:
                continue
            charset = part.get_content_charset() or "utf-8"
            text = payload.decode(charset, errors="replace")
            if content_type == "text/plain":
                plain_parts.append(text)
            elif content_type == "text/html":
                html_parts.append(text)
    else:
        try:
            payload = message.get_payload(decode=True)
        except Exception:
            payload = None
        if payload:
            charset = message.get_content_charset() or "utf-8"
            text = payload.decode(charset, errors="replace")
            if message.get_content_type() == "text/html":
                html_parts.append(text)
            else:
                plain_parts.append(text)

    body = "\n".join(part.strip() for part in plain_parts if part.strip())
    if not body and html_parts:
        body = "\n".join(_html_to_text(part) for part in html_parts if part.strip())
    body = _normalize_body_text(body)
    truncated = False
    if len(body) > limit:
        body = body[:limit].rstrip() + "\n\n[Текст письма сокращен]"
        truncated = True
    return body, truncated


def _html_to_text(value: str) -> str:
    text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", value or "")
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</p\s*>", "\n", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    return html.unescape(text)


def _normalize_body_text(value: str) -> str:
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _stable_message_id(message: Message, uid: int, subject: str, from_rows: List[Dict[str, str]], date: str) -> str:
    message_id = str(message.get("Message-ID") or message.get("Message-Id") or "").strip()
    if message_id:
        return message_id.strip("<>")
    fingerprint_source = json.dumps(
        {
            "uid": uid,
            "subject": subject,
            "from": from_rows,
            "date": date,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return "fallback-" + hashlib.sha256(fingerprint_source.encode("utf-8")).hexdigest()


def _parse_email_message(uid: int, raw_bytes: bytes, body_limit: int) -> Dict[str, Any]:
    message = email.message_from_bytes(raw_bytes)
    subject = _decode_header_value(message.get("Subject")).strip()
    from_rows = _address_rows(message.get("From"))
    to_rows = _address_rows(message.get("To"))
    cc_rows = _address_rows(message.get("Cc"))
    date = _message_date(message.get("Date"))
    body, truncated = _message_body(message, body_limit)
    message_id = _stable_message_id(message, uid, subject, from_rows, date)
    return {
        "uid": uid,
        "message_id": message_id,
        "subject": subject,
        "from": from_rows,
        "to": to_rows,
        "cc": cc_rows,
        "date": date,
        "body": body,
        "body_truncated": truncated,
    }


def _subject_matches(subject: str, route: Dict[str, Any]) -> bool:
    lowered_subject = subject.casefold()
    return any(str(trigger).casefold() in lowered_subject for trigger in route["subject_triggers"])


def _route_key(route: Dict[str, Any]) -> str:
    raw = str(route.get("name") or "").strip().lower()
    return re.sub(r"[^a-zа-я0-9_-]+", "_", raw, flags=re.IGNORECASE).strip("_") or "route"


def _dedupe_key(message_id: str, route: Dict[str, Any], space: str) -> str:
    source = (
        f"{message_id}|{_route_key(route)}|{route.get('target_system', 'sbertrack')}|"
        f"{route.get('jira_domain', '')}|{space}"
    ).lower()
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def _resolve_assignee(
    to_rows: List[Dict[str, str]],
    technical_mailboxes: List[str],
    user_map: Dict[str, Dict[str, Any]],
) -> Tuple[str, List[Dict[str, str]]]:
    technical = {_normalize_email(value) for value in technical_mailboxes if value}
    candidates = []
    seen = set()
    for row in to_rows or []:
        address = _normalize_email(row.get("email"))
        if not address or address in technical or address in seen:
            continue
        seen.add(address)
        user = user_map.get(address) if isinstance(user_map, dict) else None
        if not isinstance(user, dict):
            continue
        if not bool(user.get("enabled", True)):
            continue
        user_id = str(user.get("sbertrack_user_id") or "").strip()
        if not user_id:
            continue
        candidates.append(
            {
                "email": address,
                "name": str(user.get("name") or row.get("name") or "").strip(),
                "sbertrack_user_id": user_id,
            }
        )
    if len(candidates) == 1:
        return candidates[0]["sbertrack_user_id"], candidates
    return "", candidates


def _build_summary(route: Dict[str, Any], message_data: Dict[str, Any]) -> str:
    subject = str(message_data.get("subject") or "").strip()
    template = str(route.get("summary_template") or "Письмо: {subject}")
    try:
        summary = template.format(subject=subject, route=route.get("name") or "")
    except Exception:
        summary = f"Письмо: {subject}"
    summary = re.sub(r"\s+", " ", summary).strip()
    if len(summary) > SUMMARY_MAX_CHARS:
        summary = summary[: SUMMARY_MAX_CHARS - 1].rstrip() + "…"
    return summary or "Письмо без темы"


def _build_description(event: Dict[str, Any]) -> str:
    mail = event.get("mail") if isinstance(event.get("mail"), dict) else {}
    route = event.get("route") if isinstance(event.get("route"), dict) else {}
    lines = [
        f"*От:* {_addresses_display(mail.get('from') or []) or 'не указано'}",
        f"*Кому:* {_addresses_display(mail.get('to') or []) or 'не указано'}",
        f"*Копия:* {_addresses_display(mail.get('cc') or []) or 'не указано'}",
        f"*Тема:* {mail.get('subject') or 'без темы'}",
        f"*Дата:* {mail.get('date') or 'не указано'}",
        "*Источник:* email",
        f"*Сработавшее правило:* {route.get('name') or '-'}",
        f"*Пространство SberTrack:* {event.get('space') or '-'}",
    ]
    candidates = event.get("assignee_candidates") or []
    if candidates and not event.get("assigned_to"):
        rendered = ", ".join(
            f"{candidate.get('name') or candidate.get('email')} <{candidate.get('email')}>"
            for candidate in candidates
        )
        lines.append(f"*Кандидаты в исполнители:* {rendered}")
    if mail.get("body_truncated"):
        lines.append("*Примечание:* Текст письма сокращен.")
    lines.append("")
    lines.append("*Текст письма:*")
    lines.append(mail.get("body") or "")
    return "\n".join(lines).strip()


def _event_from_match(
    message_data: Dict[str, Any],
    route: Dict[str, Any],
    space: str,
    assigned_to: str,
    assignee_candidates: List[Dict[str, str]],
) -> Dict[str, Any]:
    return {
        "dedupe_key": _dedupe_key(message_data["message_id"], route, space),
        "message_id": message_data["message_id"],
        "uid": message_data["uid"],
        "mail": message_data,
        "route": {
            "name": route["name"],
            "suit": route["suit"],
            "priority": route["priority"],
            "summary_template": route["summary_template"],
            "target_system": route.get("target_system", "sbertrack"),
            "jira_domain": route.get("jira_domain", "sberbank"),
            "jira_issue_type": route.get("jira_issue_type", "Story"),
            "jira_priority": route.get("jira_priority", "Minor"),
            "jira_labels": route.get("jira_labels", []),
            "jira_team": route.get("jira_team", {}),
        },
        "space": space,
        "summary": _build_summary(route, message_data),
        "description": "",
        "assigned_to": assigned_to,
        "assignee_candidates": assignee_candidates,
        "created_at": _format_timestamp(),
        "last_error": "",
    }


def _connect_imap(settings: Dict[str, Any]) -> imaplib.IMAP4_SSL:
    _ensure_imap_settings(settings)
    context = ssl.create_default_context()
    if not settings.get("ssl_verify"):
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    client = imaplib.IMAP4_SSL(settings["host"], int(settings["port"]), ssl_context=context)
    client.login(settings["username"], settings["password"])
    client.select("INBOX")
    return client


def _fetch_new_messages(
    settings: Dict[str, Any],
    last_checked_uid: int,
    limit: int,
    body_limit: int,
) -> Tuple[List[Dict[str, Any]], int]:
    client = _connect_imap(settings)
    try:
        status, data = client.uid("search", None, "UID", f"{int(last_checked_uid) + 1}:*")
        if status != "OK":
            raise EmailToSberTrackError("IMAP UID search failed.")
        uids = []
        for chunk in data or []:
            if not chunk:
                continue
            for raw_uid in chunk.split():
                try:
                    uids.append(int(raw_uid))
                except ValueError:
                    continue
        uids = sorted(set(uids))[:limit]
        messages = []
        last_seen = int(last_checked_uid or 0)
        for uid in uids:
            status, fetched = client.uid("fetch", str(uid), "(RFC822)")
            if status != "OK":
                raise EmailToSberTrackError(f"IMAP fetch failed for UID {uid}.")
            raw_bytes = b""
            for part in fetched or []:
                if isinstance(part, tuple) and len(part) >= 2 and isinstance(part[1], bytes):
                    raw_bytes = part[1]
                    break
            if not raw_bytes:
                continue
            parsed_message = _parse_email_message(uid, raw_bytes, body_limit)
            if _is_recent_message_date(parsed_message.get("date")):
                messages.append(parsed_message)
            last_seen = max(last_seen, uid)
        return messages, last_seen
    finally:
        try:
            client.close()
        except Exception:
            pass
        try:
            client.logout()
        except Exception:
            pass


def _match_messages(
    messages: List[Dict[str, Any]],
    settings: Dict[str, Any],
    state: Dict[str, Any],
) -> List[Dict[str, Any]]:
    user_map = get_sbertrack_users_config()
    created = state.get("created_keys") if isinstance(state.get("created_keys"), dict) else {}
    pending = state.get("pending") if isinstance(state.get("pending"), dict) else {}
    matches = []
    for message_data in messages:
        subject = str(message_data.get("subject") or "")
        matched_destinations = set()
        assigned_to, candidates = _resolve_assignee(
            message_data.get("to") or [],
            settings.get("technical_mailboxes") or [],
            user_map,
        )
        for route in settings.get("routes") or []:
            if not route.get("enabled", True) or not _subject_matches(subject, route):
                continue
            for space in route.get("spaces") or []:
                normalized_space = str(space or "").strip()
                if not normalized_space:
                    continue
                destination_key = "|".join(
                    (
                        str(route.get("target_system") or "sbertrack").strip().lower(),
                        str(route.get("jira_domain") or "").strip().lower(),
                        normalized_space.lower(),
                    )
                )
                if destination_key in matched_destinations:
                    continue
                event = _event_from_match(
                    message_data,
                    route,
                    normalized_space,
                    assigned_to,
                    candidates,
                )
                event["description"] = _build_description(event)
                if event["dedupe_key"] in created or event["dedupe_key"] in pending:
                    matched_destinations.add(destination_key)
                    continue
                matches.append(event)
                matched_destinations.add(destination_key)
    return matches


def _sbertrack_url(settings: Dict[str, Any], space: str) -> str:
    return (
        settings["api_base_url"].rstrip("/")
        + "/rest/api/unit/v2"
        + f"?space={quote(str(space or '').strip())}"
        + f"&tenant={quote(settings['tenant'])}"
    )


def _create_sbertrack_task(event: Dict[str, Any], settings: Dict[str, Any]) -> Dict[str, Any]:
    _ensure_sbertrack_settings(settings)
    route = event.get("route") if isinstance(event.get("route"), dict) else {}
    attributes = {
        "priority": route.get("priority") or "low",
        "workflow_status": WORKFLOW_STATUS_NEW,
        "reporter": settings["reporter_id"],
    }
    assigned_to = str(event.get("assigned_to") or "").strip()
    if assigned_to:
        attributes["assigned_to"] = assigned_to
    payload = {
        "suit": route.get("suit") or "task",
        "summary": event.get("summary") or "Письмо без темы",
        "description": _build_description(event),
        "space": event.get("space") or "",
        "attributes": attributes,
    }
    request_body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    auth_raw = f"{settings['username']}:{settings['password']}".encode("utf-8")
    headers = {
        "Authorization": "Basic " + base64.b64encode(auth_raw).decode("ascii"),
        "Content-Type": "application/json; charset=utf-8",
        "Accept": "application/json",
    }
    context = ssl.create_default_context()
    if not settings.get("ssl_verify"):
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    request = Request(
        _sbertrack_url(settings, event.get("space") or ""),
        data=request_body,
        headers=headers,
        method="POST",
    )
    try:
        with urlopen(request, timeout=40, context=context) as response:
            response_text = response.read().decode("utf-8", errors="replace")
            status_code = int(getattr(response, "status", 200) or 200)
    except HTTPError as exc:
        response_text = exc.read().decode("utf-8", errors="replace")
        raise EmailToSberTrackError(
            f"SberTrack returned HTTP {exc.code}: {response_text[:500]}"
        ) from exc
    except URLError as exc:
        raise EmailToSberTrackError(f"SberTrack request failed: {exc}") from exc
    if status_code >= 400:
        raise EmailToSberTrackError(
            f"SberTrack returned HTTP {status_code}: {response_text[:500]}"
        )
    try:
        response_payload = json.loads(response_text) if response_text else {}
    except ValueError:
        response_payload = {}
    task_key = str(
        response_payload.get("key")
        or response_payload.get("id")
        or response_payload.get("unit")
        or ""
    ).strip()
    task_url = ""
    ui_base = str(settings.get("ui_base_url") or "").strip().rstrip("/")
    if ui_base and task_key:
        task_url = f"{ui_base}/{task_key}"
    return {
        "created_at": _format_timestamp(),
        "task_key": task_key,
        "task_url": task_url,
        "response": response_payload,
    }


def _create_task(event: Dict[str, Any], sbertrack_settings: Dict[str, Any]) -> Dict[str, Any]:
    target_system = str((event.get("route") or {}).get("target_system") or "sbertrack").lower()
    if target_system == "jira":
        from services.email_to_jira_service import create_email_jira_task

        return create_email_jira_task(event)
    return _create_sbertrack_task(event, sbertrack_settings)


def _retry_pending(
    state: Dict[str, Any],
    settings: Dict[str, Any],
    sbertrack_settings: Dict[str, Any],
) -> int:
    pending = state.get("pending") if isinstance(state.get("pending"), dict) else {}
    if not pending:
        return 0
    created_count = 0
    for dedupe_key, event in list(pending.items())[: settings["max_pending_per_cycle"]]:
        mail = event.get("mail") if isinstance(event.get("mail"), dict) else {}
        if not _is_recent_message_date(mail.get("date")):
            pending.pop(dedupe_key, None)
            continue
        if dedupe_key in state.get("created_keys", {}):
            pending.pop(dedupe_key, None)
            continue
        try:
            result = _create_task(event, sbertrack_settings)
            state.setdefault("created_keys", {})[dedupe_key] = result
            state.setdefault("processed_message_ids", []).append(str(event.get("message_id") or ""))
            pending.pop(dedupe_key, None)
            created_count += 1
        except Exception as exc:
            event["last_error"] = str(exc)
            event["last_attempt_at"] = _format_timestamp()
            pending[dedupe_key] = event
            state["last_error"] = str(exc)
            break
    state["pending"] = pending
    return created_count


def _process_dry_run_matches_when_live(
    state: Dict[str, Any],
    settings: Dict[str, Any],
) -> int:
    dry_run = state.get("dry_run_matches") if isinstance(state.get("dry_run_matches"), dict) else {}
    pending = state.get("pending") if isinstance(state.get("pending"), dict) else {}
    created = state.get("created_keys") if isinstance(state.get("created_keys"), dict) else {}
    moved = 0
    for dedupe_key, event in list(dry_run.items()):
        if dedupe_key in created:
            dry_run.pop(dedupe_key, None)
            continue
        if dedupe_key not in pending:
            event = dict(event)
            event.setdefault("created_at", _format_timestamp())
            event["last_error"] = ""
            pending[dedupe_key] = event
            moved += 1
        dry_run.pop(dedupe_key, None)
        if moved >= settings["max_pending_per_cycle"]:
            break
    state["dry_run_matches"] = dry_run
    state["pending"] = pending
    return moved


def run_email_to_sbertrack_cycle() -> Dict[str, Any]:
    settings = _automation_settings()
    state = _read_state()
    state["last_checked_at"] = _format_timestamp()
    if not settings["enabled"]:
        state["last_result"] = "disabled"
        state["last_error"] = ""
        return state

    if not _process_lock.acquire(blocking=False):
        state["last_result"] = "locked"
        return state
    if not _acquire_file_lock():
        _process_lock.release()
        state["last_result"] = "locked"
        return state
    try:
        state = _read_state()
        state["last_checked_at"] = _format_timestamp()
        state["last_error"] = ""
        state.setdefault("created_keys", {})
        state.setdefault("pending", {})
        state.setdefault("dry_run_matches", {})
        created_count = 0
        dry_run_count = 0
        sbertrack_settings = _sbertrack_settings()

        if not settings["dry_run"]:
            _process_dry_run_matches_when_live(state, settings)
            created_count += _retry_pending(state, settings, sbertrack_settings)

        imap_settings = _imap_settings()
        technical_mailboxes = set(settings.get("technical_mailboxes") or [])
        for address in (imap_settings.get("mail_from"), imap_settings.get("username")):
            normalized = _normalize_email(address)
            if normalized and "@" in normalized and normalized not in technical_mailboxes:
                settings["technical_mailboxes"].append(normalized)

        messages, last_seen_uid = _fetch_new_messages(
            imap_settings,
            int(state.get("last_checked_uid") or 0),
            settings["lookback_limit"],
            settings["body_max_chars"],
        )
        matches = _match_messages(messages, settings, state)
        if settings["dry_run"]:
            for event in matches:
                event["dry_run_at"] = _format_timestamp()
                state.setdefault("dry_run_matches", {})[event["dedupe_key"]] = event
                dry_run_count += 1
            state["last_result"] = "dry_run"
        else:
            pending = state.setdefault("pending", {})
            for event in matches:
                if event["dedupe_key"] not in pending and event["dedupe_key"] not in state["created_keys"]:
                    pending[event["dedupe_key"]] = event
            created_count += _retry_pending(state, settings, sbertrack_settings)
            state["last_result"] = "ok"

        if last_seen_uid:
            state["last_checked_uid"] = max(int(state.get("last_checked_uid") or 0), last_seen_uid)
            state["last_uid_seen"] = last_seen_uid
        state["last_created_count"] = created_count
        state["last_dry_run_count"] = dry_run_count
        state["last_pending_count"] = len(state.get("pending") or {})
        state["last_success_at"] = _format_timestamp()
        _write_state(state)
        return state
    except Exception as exc:
        state["last_result"] = "error"
        state["last_error"] = str(exc)
        state["last_pending_count"] = len(state.get("pending") or {})
        _write_state(state)
        logging.exception("Email to SberTrack cycle failed")
        return state
    finally:
        _release_file_lock()
        _process_lock.release()


def get_email_to_sbertrack_status() -> Dict[str, Any]:
    settings = _automation_settings()
    state = _read_state()
    if not settings["enabled"]:
        mode = "disabled"
    elif settings["dry_run"]:
        mode = "dry_run"
    else:
        mode = "active"
    return {
        "enabled": settings["enabled"],
        "dry_run": settings["dry_run"],
        "mode": mode,
        "last_checked_at": state.get("last_checked_at") or "",
        "last_success_at": state.get("last_success_at") or "",
        "last_checked_uid": int(state.get("last_checked_uid") or 0),
        "last_uid_seen": int(state.get("last_uid_seen") or 0),
        "pending_count": len(state.get("pending") or {}),
        "dry_run_match_count": len(state.get("dry_run_matches") or {}),
        "created_count": len(state.get("created_keys") or {}),
        "last_created_count": int(state.get("last_created_count") or 0),
        "last_dry_run_count": int(state.get("last_dry_run_count") or 0),
        "last_result": state.get("last_result") or "",
        "last_error": state.get("last_error") or "",
    }


def _worker_loop() -> None:
    while not _worker_stop.is_set():
        settings = _automation_settings()
        interval = max(30, int(settings.get("poll_interval_seconds") or 300))
        if settings.get("enabled"):
            try:
                run_email_to_sbertrack_cycle()
            except Exception:
                logging.exception("Email to SberTrack worker failed")
        _worker_stop.wait(interval)


def ensure_email_to_sbertrack_worker_started() -> None:
    global _worker_thread
    if _worker_thread and _worker_thread.is_alive():
        return
    _worker_stop.clear()
    _worker_thread = threading.Thread(
        target=_worker_loop,
        name="email-to-sbertrack-worker",
        daemon=True,
    )
    _worker_thread.start()
