import html
import logging
import re
from datetime import datetime
from typing import Dict, Iterable, List, Optional, Set, Tuple

import requests

from config import TOKENS
from services.release_monitor_service import get_release_monitor_snapshot


CONFLUENCE_DELTA_BASE = "https://confluence.delta.sbrf.ru"
REPORT_MARKER = "rm-unassigned-report-v1"
ROW_ANCHOR_PREFIX = "rm-unassigned-row-"
DEFAULT_PAGE_TITLE = "Информирование о новых релизах"


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


def sync_unassigned_release_confluence_page() -> Dict:
    page_id = _get_page_id()
    snapshot = get_release_monitor_snapshot() or {}
    selected_items = select_unassigned_current_week_items(snapshot.get("items") or [])
    current_row_keys = {
        str(item.get("row_key") or "").strip()
        for item in selected_items
        if str(item.get("row_key") or "").strip()
    }

    for attempt in range(2):
        page = _fetch_page(page_id)
        initialized, previous_row_keys = extract_report_state(page.get("storage_html", ""))
        new_row_keys = current_row_keys - previous_row_keys
        removed_row_keys = previous_row_keys - current_row_keys

        if initialized and current_row_keys == previous_row_keys:
            return {
                "updated": False,
                "rows_count": len(current_row_keys),
                "new_rows_count": 0,
                "removed_rows_count": 0,
                "page_id": page_id,
                "page_url": f"{CONFLUENCE_DELTA_BASE}/pages/viewpage.action?pageId={page_id}",
                "page_title": page.get("title") or DEFAULT_PAGE_TITLE,
                "message": "Список не изменился, уведомление не отправлялось.",
            }

        storage_html = build_unassigned_release_page_storage(
            selected_items,
            new_row_keys=new_row_keys,
            snapshot_meta=snapshot.get("meta") or {},
        )
        version_message = (
            "Обновление релизов без ответственного: "
            f"добавлено {len(new_row_keys)}, удалено {len(removed_row_keys)}, "
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
            "removed_rows_count": len(removed_row_keys),
            "page_id": page_id,
            "page_url": f"{CONFLUENCE_DELTA_BASE}/pages/viewpage.action?pageId={page_id}",
            "page_title": page.get("title") or DEFAULT_PAGE_TITLE,
            "page_version": int(((response.json().get("version") or {}).get("number")) or 0),
            "message": "Страница релизов без ответственных обновлена.",
        }

    raise ValueError("Не удалось обновить страницу Confluence из-за конфликта версий")
