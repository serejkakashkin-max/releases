import copy
import hashlib
import html
import json
import logging
import os
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple
from uuid import uuid4

import requests

from config import TOKENS
from services.feature_flags_service import is_automation_enabled


CONFLUENCE_DELTA_BASE = "https://confluence.delta.sbrf.ru"
REPORT_MARKER = "rm-unassigned-report-v1"
ROW_ANCHOR_PREFIX = "rm-unassigned-row-"
DEFAULT_PAGE_TITLE = "Информирование о новых релизах"
AUTO_SYNC_FLAG = "confluence_unassigned_auto_sync"
AUTO_SYNC_THROTTLE_SECONDS = 300
AUTO_SYNC_LOCK_STALE_SECONDS = 900
NOTIFY_STATE_FILE = Path(__file__).resolve().parent.parent / "cache" / "release_monitor_unassigned_notify_state.json"
AUTO_SYNC_LOCK_FILE = NOTIFY_STATE_FILE.with_suffix(".lock")

_auto_sync_process_lock = threading.Lock()
_auto_sync_queue_lock = threading.Lock()
_auto_sync_worker_thread = None
_queued_auto_sync_job = None
_last_observed_auto_sync_enabled = is_automation_enabled(AUTO_SYNC_FLAG)


def _default_notify_state() -> Dict:
    return {
        "version": 1,
        "tracking_state": "uninitialized",
        "week_key": "",
        "notified_row_keys": [],
        "active_row_keys": [],
        "pending_row_keys": [],
        "last_evaluated_at": "",
        "last_auto_attempt_at": "",
        "last_auto_success_at": "",
        "last_page_update_at": "",
        "last_auto_sync_revision": "",
        "last_new_row_keys": [],
        "last_new_count": 0,
        "last_result": "",
        "last_error": "",
        "last_page_update_hash": "",
    }


def _normalize_row_keys(values) -> List[str]:
    return sorted({
        str(value or "").strip()
        for value in (values or [])
        if str(value or "").strip()
    })


def _normalize_notify_state(payload) -> Dict:
    state = _default_notify_state()
    if isinstance(payload, dict):
        for key in state:
            if key in payload:
                state[key] = payload[key]
    for key in ("notified_row_keys", "active_row_keys", "pending_row_keys", "last_new_row_keys"):
        state[key] = _normalize_row_keys(state.get(key))
    state["version"] = 1
    state["last_new_count"] = int(state.get("last_new_count") or 0)
    return state


def _load_notify_state() -> Tuple[Dict, bool]:
    if not NOTIFY_STATE_FILE.exists():
        return _default_notify_state(), False
    try:
        with NOTIFY_STATE_FILE.open("r", encoding="utf-8-sig") as handle:
            return _normalize_notify_state(json.load(handle)), True
    except Exception as exc:
        logging.error("Confluence unassigned auto-sync: failed to read notify state: %s", exc)
        return _default_notify_state(), False


def _atomic_write_notify_state(state: Dict) -> None:
    NOTIFY_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    normalized = _normalize_notify_state(state)
    temp_path = NOTIFY_STATE_FILE.with_name(f".{NOTIFY_STATE_FILE.name}.{uuid4().hex}.tmp")
    try:
        with temp_path.open("w", encoding="utf-8") as handle:
            json.dump(normalized, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, NOTIFY_STATE_FILE)
    finally:
        if temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass


def _format_state_timestamp(value: Optional[datetime] = None) -> str:
    return (value or datetime.now()).isoformat(timespec="seconds")


def _current_week_key(value: Optional[datetime] = None) -> str:
    iso_year, iso_week, _ = (value or datetime.now()).isocalendar()
    return f"{iso_year}-W{iso_week:02d}"


def _parse_state_timestamp(value: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(str(value or "").replace("Z", "+00:00")).replace(tzinfo=None)
    except (TypeError, ValueError):
        return None


def _snapshot_revision(snapshot: Dict) -> str:
    meta = dict((snapshot or {}).get("meta") or {})
    return str(meta.get("data_revision") or meta.get("accepted_revision") or "").strip()


def _row_key_hash(row_keys: Iterable[str]) -> str:
    encoded = "\n".join(sorted(set(row_keys or []))).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _page_url(page_id: Optional[str] = None) -> str:
    resolved_page_id = str(page_id or TOKENS.get("release_monitor_unassigned_confluence_page_id") or "").strip()
    if not resolved_page_id:
        return ""
    return f"{CONFLUENCE_DELTA_BASE}/pages/viewpage.action?pageId={resolved_page_id}"


def _confluence_headers() -> Dict[str, str]:
    token = str(TOKENS.get("confluence_delta_token", "") or "").strip()
    if not token:
        raise ValueError("Не настроен token доступа к Confluence")
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _get_page_id() -> str:
    page_id = str(TOKENS.get("release_monitor_unassigned_confluence_page_id", "") or "").strip()
    if not page_id:
        raise ValueError(
            "Не настроен release_monitor_unassigned_confluence_page_id в config.json"
        )
    if not page_id.isdigit():
        raise ValueError("Некорректный pageId страницы уведомлений Confluence")
    return page_id


def _extract_error_detail(response) -> str:
    try:
        payload = response.json()
        if isinstance(payload, dict):
            for key in ("message", "errorMessage", "reason"):
                value = str(payload.get(key) or "").strip()
                if value:
                    return value
    except Exception:
        pass
    return str(getattr(response, "text", "") or getattr(response, "reason", "") or "empty response")[:1000]


def _fetch_page(page_id: str) -> Dict:
    response = requests.get(
        f"{CONFLUENCE_DELTA_BASE}/rest/api/content/{page_id}",
        headers=_confluence_headers(),
        params={"expand": "body.storage,version,title"},
        verify=False,
        timeout=60,
    )
    if not response.ok:
        raise ValueError(
            f"Confluence GET failed ({response.status_code}): {_extract_error_detail(response)}"
        )
    data = response.json()
    return {
        "page_id": page_id,
        "title": str(data.get("title") or "").strip() or DEFAULT_PAGE_TITLE,
        "version": int(((data.get("version") or {}).get("number")) or 0),
        "storage_html": str(
            (((data.get("body") or {}).get("storage") or {}).get("value")) or ""
        ),
    }


def _has_responsible(item: Dict) -> bool:
    responsibles = item.get("psi_responsibles") or []
    if not isinstance(responsibles, list):
        responsibles = [responsibles] if responsibles else []
    return any(str(value or "").strip() for value in responsibles)


def select_unassigned_current_week_items(items: Iterable[Dict]) -> List[Dict]:
    selected = []
    for source_index, item in enumerate(items or []):
        if not isinstance(item, dict):
            continue
        if not item.get("is_current_week_assignment_scope"):
            continue
        if not item.get("is_missing_week_responsible"):
            continue
        if _has_responsible(item):
            continue
        if item.get("is_cancelled") or item.get("is_final"):
            continue
        row_key = str(item.get("row_key") or "").strip()
        if not row_key:
            continue
        selected_item = dict(item)
        selected_item["_notification_source_index"] = source_index
        selected.append(selected_item)
    return selected


def _encode_row_key(row_key: str) -> str:
    return str(row_key or "").encode("utf-8").hex()


def _decode_row_key(encoded: str) -> str:
    try:
        return bytes.fromhex(encoded).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return ""


def extract_report_state(storage_html: str) -> Tuple[bool, Set[str]]:
    storage_html = str(storage_html or "")
    initialized = REPORT_MARKER in storage_html
    row_keys = {
        decoded
        for encoded in re.findall(
            rf"{re.escape(ROW_ANCHOR_PREFIX)}([0-9a-fA-F]+)",
            storage_html,
        )
        if (decoded := _decode_row_key(encoded))
    }
    return initialized, row_keys


def _parse_date(value) -> Optional[datetime]:
    raw_value = str(value or "").strip()
    if not raw_value:
        return None
    for fmt in ("%d.%m.%Y", "%Y-%m-%d", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(raw_value[:19] if "T" in fmt else raw_value, fmt)
        except ValueError:
            continue
    return None


def _sort_items(items: Iterable[Dict], new_row_keys: Set[str]) -> List[Dict]:
    far_future = datetime.max

    def sort_key(item):
        row_key = str(item.get("row_key") or "").strip()
        deployment_date = (
            _parse_date(item.get("deployment_start_iso"))
            or _parse_date(item.get("deployment_start"))
            or far_future
        )
        source_index = int(item.get("_notification_source_index", 0) or 0)
        return (0 if row_key in new_row_keys else 1, deployment_date, source_index)

    return sorted(items or [], key=sort_key)


def _anchor(name: str) -> str:
    return (
        '<ac:structured-macro ac:name="anchor">'
        f'<ac:parameter ac:name="">{html.escape(name)}</ac:parameter>'
        "</ac:structured-macro>"
    )


def _status_badge(title: str, colour: str = "Blue") -> str:
    return (
        '<ac:structured-macro ac:name="status">'
        f'<ac:parameter ac:name="title">{html.escape(title)}</ac:parameter>'
        f'<ac:parameter ac:name="colour">{html.escape(colour)}</ac:parameter>'
        "</ac:structured-macro>"
    )


def _link(url, label) -> str:
    url = str(url or "").strip()
    label = str(label or "").strip() or "Открыть"
    if not url:
        return "—"
    return f'<a href="{html.escape(url, quote=True)}">{html.escape(label)}</a>'


def _format_snapshot_timestamp(meta: Dict) -> str:
    raw_value = str(
        meta.get("accepted_at")
        or meta.get("last_updated")
        or meta.get("last_full_sync")
        or ""
    ).strip()
    if not raw_value:
        return "не указано"
    normalized = raw_value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
        return parsed.strftime("%d.%m.%Y %H:%M:%S")
    except ValueError:
        return raw_value


def _render_release_name(item: Dict, is_new: bool) -> str:
    row_key = str(item.get("row_key") or "").strip()
    release_key = str(item.get("release_key") or "").strip() or "—"
    summary = str(item.get("release_summary") or "").strip()
    marker = _anchor(f"{ROW_ANCHOR_PREFIX}{_encode_row_key(row_key)}")
    badge = f" {_status_badge('Новый')}" if is_new else ""
    summary_html = f"<br/><span>{html.escape(summary)}</span>" if summary else ""
    return f"{marker}<strong>{html.escape(release_key)}</strong>{badge}{summary_html}"


def _render_owner(item: Dict) -> str:
    owner = str(item.get("psi_owner") or "").strip()
    if not owner:
        return "—"
    source = str(item.get("psi_owner_source") or "").strip()
    label = "Устанавливает" if source == "manual_text" else "Дежурный"
    return f"<strong>{label}:</strong><br/>{html.escape(owner)}"


def build_unassigned_release_page_storage(
    items: Iterable[Dict],
    *,
    new_row_keys: Set[str],
    snapshot_meta: Optional[Dict] = None,
    updated_at: Optional[datetime] = None,
) -> str:
    snapshot_meta = dict(snapshot_meta or {})
    updated_at = updated_at or datetime.now()
    sorted_items = _sort_items(items, set(new_row_keys or set()))
    report_anchor = _anchor(REPORT_MARKER)
    updated_label = updated_at.strftime("%d.%m.%Y %H:%M:%S")
    snapshot_label = _format_snapshot_timestamp(snapshot_meta)

    info_block = f"""
<ac:structured-macro ac:name="info">
  <ac:rich-text-body>
    <p><strong>Обновлено:</strong> {html.escape(updated_label)}</p>
    <p><strong>Подтвержденный снимок:</strong> {html.escape(snapshot_label)}</p>
    <p><strong>Найдено релизов:</strong> {len(sorted_items)}</p>
    <p>Необходимо назначить ответственных в Блоке релизов. После назначения и повторного обновления страницы релиз исчезнет из списка.</p>
  </ac:rich-text-body>
</ac:structured-macro>
""".strip()

    if not sorted_items:
        content = """
<ac:structured-macro ac:name="tip">
  <ac:rich-text-body>
    <p><strong>На текущей неделе нет релизов без назначенного ответственного.</strong></p>
  </ac:rich-text-body>
</ac:structured-macro>
""".strip()
    else:
        rows = []
        for index, item in enumerate(sorted_items, start=1):
            row_key = str(item.get("row_key") or "").strip()
            release_key = str(item.get("release_key") or "").strip()
            rov_key = str(item.get("rov_key") or "").strip()
            status = str(item.get("release_status") or item.get("row_label") or "").strip() or "—"
            rows.append(
                "<tr>"
                f"<td>{index}</td>"
                f"<td>{html.escape(str(item.get('deployment_start') or '—'))}</td>"
                f"<td>{_render_release_name(item, row_key in new_row_keys)}</td>"
                f"<td>{html.escape(rov_key or '—')}</td>"
                f"<td>{html.escape(str(item.get('ke') or '—'))}</td>"
                f"<td>{html.escape(str(item.get('release_version') or '—'))}</td>"
                f"<td>{html.escape(status)}</td>"
                f"<td>{_render_owner(item)}</td>"
                f"<td>{_link(item.get('release_url'), release_key or 'Открыть')}</td>"
                f"<td>{_link(item.get('rov_url'), rov_key or 'Открыть')}</td>"
                "</tr>"
            )
        content = f"""
<table data-layout="wide">
  <thead>
    <tr>
      <th>№</th>
      <th>Дата внедрения</th>
      <th>Релиз</th>
      <th>РОВ</th>
      <th>КЭ</th>
      <th>Версия</th>
      <th>Статус/этап</th>
      <th>Дежурный/устанавливает</th>
      <th>Jira Release</th>
      <th>Jira РОВ</th>
    </tr>
  </thead>
  <tbody>{''.join(rows)}</tbody>
</table>
""".strip()

    return (
        f"{report_anchor}"
        "<h1>Релизы текущей недели без назначенного ответственного</h1>"
        f"{info_block}{content}"
    )


def _put_page(page: Dict, storage_html: str, version_message: str):
    next_version = max(int(page.get("version") or 0), 1) + 1
    payload = {
        "id": page["page_id"],
        "type": "page",
        "title": page.get("title") or DEFAULT_PAGE_TITLE,
        "version": {
            "number": next_version,
            "minorEdit": False,
            "message": version_message,
        },
        "body": {
            "storage": {
                "value": storage_html,
                "representation": "storage",
            }
        },
    }
    return requests.put(
        f"{CONFLUENCE_DELTA_BASE}/rest/api/content/{page['page_id']}",
        headers=_confluence_headers(),
        json=payload,
        verify=False,
        timeout=60,
    )


def _sync_unassigned_release_confluence_page(
    snapshot: Dict,
    *,
    new_row_keys: Set[str],
) -> Dict:
    page_id = _get_page_id()
    selected_items = select_unassigned_current_week_items(snapshot.get("items") or [])
    current_row_keys = {
        str(item.get("row_key") or "").strip()
        for item in selected_items
        if str(item.get("row_key") or "").strip()
    }

    for attempt in range(2):
        page = _fetch_page(page_id)
        initialized, previous_row_keys = extract_report_state(page.get("storage_html", ""))

        if initialized and current_row_keys == previous_row_keys:
            return {
                "updated": False,
                "rows_count": len(current_row_keys),
                "new_rows_count": len(new_row_keys),
                "page_id": page_id,
                "page_url": _page_url(page_id),
                "page_title": page.get("title") or DEFAULT_PAGE_TITLE,
                "message": "Страница уже содержит актуальный состав.",
            }

        storage_html = build_unassigned_release_page_storage(
            selected_items,
            new_row_keys=new_row_keys,
            snapshot_meta=snapshot.get("meta") or {},
        )
        version_message = (
            "Автоматическое обновление: "
            f"новых релизов {len(new_row_keys)}, "
            f"всего {len(current_row_keys)}"
        )
        response = _put_page(page, storage_html, version_message)
        if response.status_code == 409 and attempt == 0:
            logging.warning(
                "Confluence unassigned release page version conflict; retrying page_id=%s",
                page_id,
            )
            continue
        if not response.ok:
            raise ValueError(
                f"Confluence PUT failed ({response.status_code}): {_extract_error_detail(response)}"
            )

        return {
            "updated": True,
            "rows_count": len(current_row_keys),
            "new_rows_count": len(new_row_keys),
            "page_id": page_id,
            "page_url": _page_url(page_id),
            "page_title": page.get("title") or DEFAULT_PAGE_TITLE,
            "page_version": int(((response.json().get("version") or {}).get("number")) or 0),
            "message": "Страница релизов без ответственных обновлена автоматически.",
        }

    raise ValueError("Не удалось обновить страницу Confluence из-за конфликта версий")


def _acquire_auto_sync_file_lock() -> Optional[int]:
    AUTO_SYNC_LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(2):
        try:
            descriptor = os.open(
                str(AUTO_SYNC_LOCK_FILE),
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            )
            os.write(descriptor, f"{os.getpid()} {_format_state_timestamp()}".encode("utf-8"))
            return descriptor
        except FileExistsError:
            try:
                age_seconds = time.time() - AUTO_SYNC_LOCK_FILE.stat().st_mtime
            except OSError:
                return None
            if age_seconds <= AUTO_SYNC_LOCK_STALE_SECONDS:
                return None
            try:
                AUTO_SYNC_LOCK_FILE.unlink()
                logging.warning("Confluence unassigned auto-sync: removed stale lock file")
            except OSError:
                return None
    return None


def _release_auto_sync_file_lock(descriptor: Optional[int]) -> None:
    if descriptor is not None:
        try:
            os.close(descriptor)
        except OSError:
            pass
    try:
        AUTO_SYNC_LOCK_FILE.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        logging.warning("Confluence unassigned auto-sync: failed to remove lock file: %s", exc)


def _create_baseline_state(
    state: Dict,
    *,
    current_row_keys: Set[str],
    snapshot: Dict,
    week_key: str,
    result: str,
) -> Dict:
    now_text = _format_state_timestamp()
    state.update({
        "tracking_state": "active",
        "week_key": week_key,
        "notified_row_keys": sorted(current_row_keys),
        "active_row_keys": sorted(current_row_keys),
        "pending_row_keys": [],
        "last_evaluated_at": now_text,
        "last_auto_sync_revision": _snapshot_revision(snapshot),
        "last_new_row_keys": [],
        "last_new_count": 0,
        "last_result": result,
        "last_error": "",
    })
    _atomic_write_notify_state(state)
    return state


def _run_unassigned_auto_sync(
    snapshot: Dict,
    *,
    refresh_mode: str,
    force_baseline: bool = False,
) -> Dict:
    if not is_automation_enabled(AUTO_SYNC_FLAG):
        return {"result": "disabled"}

    with _auto_sync_process_lock:
        file_lock = _acquire_auto_sync_file_lock()
        if file_lock is None:
            logging.info("Confluence unassigned auto-sync: another worker owns the lock")
            return {"result": "locked"}

        try:
            selected_items = select_unassigned_current_week_items((snapshot or {}).get("items") or [])
            current_row_keys = {
                str(item.get("row_key") or "").strip()
                for item in selected_items
                if str(item.get("row_key") or "").strip()
            }
            state, state_exists = _load_notify_state()
            week_key = _current_week_key()

            if force_baseline or not state_exists or state.get("tracking_state") != "active":
                _create_baseline_state(
                    state,
                    current_row_keys=current_row_keys,
                    snapshot=snapshot,
                    week_key=week_key,
                    result="baseline_created",
                )
                logging.info(
                    "Confluence unassigned auto-sync: baseline created, rows=%s, mode=%s",
                    len(current_row_keys),
                    refresh_mode,
                )
                return {"result": "baseline_created", "rows_count": len(current_row_keys)}

            if state.get("week_key") != week_key:
                _create_baseline_state(
                    state,
                    current_row_keys=current_row_keys,
                    snapshot=snapshot,
                    week_key=week_key,
                    result="weekly_baseline_created",
                )
                logging.info(
                    "Confluence unassigned auto-sync: weekly baseline created, rows=%s, week=%s",
                    len(current_row_keys),
                    week_key,
                )
                return {"result": "weekly_baseline_created", "rows_count": len(current_row_keys)}

            notified_row_keys = set(_normalize_row_keys(state.get("notified_row_keys")))
            pending_row_keys = set(_normalize_row_keys(state.get("pending_row_keys"))) & current_row_keys
            new_row_keys = (current_row_keys - notified_row_keys) | pending_row_keys
            now = datetime.now()
            now_text = _format_state_timestamp(now)

            state.update({
                "tracking_state": "active",
                "active_row_keys": sorted(current_row_keys),
                "pending_row_keys": sorted(new_row_keys),
                "last_evaluated_at": now_text,
                "last_auto_sync_revision": _snapshot_revision(snapshot),
                "last_new_row_keys": sorted(new_row_keys),
                "last_new_count": len(new_row_keys),
            })

            if not new_row_keys:
                state["last_result"] = "waiting"
                state["last_error"] = ""
                _atomic_write_notify_state(state)
                return {"result": "waiting", "rows_count": len(current_row_keys)}

            last_attempt_at = _parse_state_timestamp(state.get("last_auto_attempt_at"))
            if (
                last_attempt_at is not None
                and (now - last_attempt_at).total_seconds() < AUTO_SYNC_THROTTLE_SECONDS
            ):
                state["last_result"] = "throttled"
                _atomic_write_notify_state(state)
                return {
                    "result": "throttled",
                    "new_rows_count": len(new_row_keys),
                }

            state["last_auto_attempt_at"] = now_text
            state["last_result"] = "running"
            _atomic_write_notify_state(state)

            try:
                result = _sync_unassigned_release_confluence_page(
                    snapshot,
                    new_row_keys=set(new_row_keys),
                )
            except Exception as exc:
                state["last_result"] = "error"
                state["last_error"] = str(exc)
                state["pending_row_keys"] = sorted(new_row_keys)
                _atomic_write_notify_state(state)
                logging.exception("Confluence unassigned auto-sync failed")
                return {"result": "error", "error": str(exc)}

            success_at = _format_state_timestamp()
            notified_row_keys.update(new_row_keys)
            state.update({
                "notified_row_keys": sorted(notified_row_keys),
                "pending_row_keys": [],
                "last_auto_success_at": success_at,
                "last_page_update_at": success_at if result.get("updated") else state.get("last_page_update_at", ""),
                "last_result": "updated" if result.get("updated") else "confirmed",
                "last_error": "",
                "last_page_update_hash": _row_key_hash(current_row_keys),
            })
            _atomic_write_notify_state(state)
            logging.info(
                "Confluence unassigned auto-sync completed, updated=%s, new=%s, rows=%s",
                result.get("updated"),
                len(new_row_keys),
                len(current_row_keys),
            )
            return {
                "result": state["last_result"],
                "new_rows_count": len(new_row_keys),
                "rows_count": len(current_row_keys),
            }
        finally:
            _release_auto_sync_file_lock(file_lock)


def _auto_sync_worker_loop() -> None:
    global _queued_auto_sync_job, _auto_sync_worker_thread

    while True:
        with _auto_sync_queue_lock:
            job = _queued_auto_sync_job
            _queued_auto_sync_job = None
            if not job:
                _auto_sync_worker_thread = None
                return

        try:
            _run_unassigned_auto_sync(
                job["snapshot"],
                refresh_mode=job["refresh_mode"],
                force_baseline=job.get("force_baseline", False),
            )
        except Exception:
            logging.exception("Confluence unassigned auto-sync worker failed unexpectedly")


def schedule_unassigned_auto_sync(snapshot: Dict, *, refresh_mode: str) -> bool:
    global _auto_sync_worker_thread, _queued_auto_sync_job, _last_observed_auto_sync_enabled

    enabled = is_automation_enabled(AUTO_SYNC_FLAG)
    with _auto_sync_queue_lock:
        previous_enabled = _last_observed_auto_sync_enabled
        _last_observed_auto_sync_enabled = enabled
        if not enabled:
            return False

        force_baseline = previous_enabled is False
        snapshot_copy = {
            "items": copy.deepcopy(list((snapshot or {}).get("items") or [])),
            "meta": copy.deepcopy(dict((snapshot or {}).get("meta") or {})),
        }
        if _queued_auto_sync_job:
            force_baseline = force_baseline or bool(_queued_auto_sync_job.get("force_baseline"))
        _queued_auto_sync_job = {
            "snapshot": snapshot_copy,
            "refresh_mode": str(refresh_mode or ""),
            "force_baseline": force_baseline,
        }

        if _auto_sync_worker_thread and _auto_sync_worker_thread.is_alive():
            return True

        _auto_sync_worker_thread = threading.Thread(
            target=_auto_sync_worker_loop,
            daemon=True,
            name="release-monitor-confluence-unassigned-auto-sync",
        )
        _auto_sync_worker_thread.start()
        return True


def get_unassigned_auto_sync_status() -> Dict:
    global _last_observed_auto_sync_enabled

    enabled = is_automation_enabled(AUTO_SYNC_FLAG)
    if not enabled:
        with _auto_sync_queue_lock:
            _last_observed_auto_sync_enabled = False
    state, state_exists = _load_notify_state()
    if not enabled:
        status = "disabled"
    elif not state_exists:
        status = "waiting_refresh"
    elif state.get("last_result") in {"baseline_created", "weekly_baseline_created"}:
        status = "baseline_created"
    elif state.get("last_result") == "error" or state.get("last_error"):
        status = "error"
    elif state.get("last_result") in {"updated", "confirmed"}:
        status = "updated"
    else:
        status = "waiting"

    return {
        "enabled": enabled,
        "status": status,
        "running": bool(_auto_sync_worker_thread and _auto_sync_worker_thread.is_alive()),
        "week_key": state.get("week_key", "") if state_exists else "",
        "last_evaluated_at": state.get("last_evaluated_at", "") if state_exists else "",
        "last_auto_attempt_at": state.get("last_auto_attempt_at", "") if state_exists else "",
        "last_auto_success_at": state.get("last_auto_success_at", "") if state_exists else "",
        "last_new_count": int(state.get("last_new_count") or 0) if state_exists else 0,
        "pending_count": len(state.get("pending_row_keys") or []) if state_exists else 0,
        "last_result": state.get("last_result", "") if state_exists else "",
        "last_error": state.get("last_error", "") if state_exists else "",
        "page_url": _page_url(),
    }
