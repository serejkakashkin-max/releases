"""
Excel statistics for ROV / Introduction Order issues.

This module is intentionally independent from the release monitor UI state:
it fetches Jira data directly and saves an XLSX report without changing the
dashboard table, release monitor cache, or chat history state.
"""

import logging
import os
import re
import time
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd

from services.feature_flags_service import get_enabled_release_prefixes
from services.release_monitor_service import (
    ROV_ISSUE_TYPE,
    _execute_search,
    _extract_field_value,
    _get_domain_groups,
    _parse_jira_date,
    _release_monitor_rov_fields_to_load,
    _resolve_field_ids,
)


REPORTS_DIR = Path(__file__).resolve().parent.parent / "reports" / "rov_statistics"
ROV_STATS_MAX_AGE_HOURS = int(os.getenv("ROV_STATS_MAX_AGE_HOURS", "1"))
ROV_START_JQL_FIELD = '"Дата/время начала работ по внедрению"'

MONTH_NAMES = {
    1: "янв",
    2: "фев",
    3: "мар",
    4: "апр",
    5: "май",
    6: "июн",
    7: "июл",
    8: "авг",
    9: "сен",
    10: "окт",
    11: "ноя",
    12: "дек",
}

DATE_PATTERN = re.compile(r"(\d{4}-\d{2}-\d{2}|\d{1,2}[.\-/]\d{1,2}[.\-/]\d{2,4})")


def _ensure_reports_dir() -> None:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)


def _parse_user_date(value: str) -> date:
    raw = str(value or "").strip()
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d-%m-%Y", "%d/%m/%Y", "%d.%m.%y", "%d-%m-%y", "%d/%m/%y"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"Некорректная дата: {raw}")


def _format_date(value: Optional[date]) -> str:
    return value.strftime("%d.%m.%Y") if value else ""


def _format_datetime(value: Optional[datetime]) -> str:
    return value.strftime("%d.%m.%Y %H:%M") if value else ""


def resolve_rov_statistics_period(message: str = "") -> Dict[str, Any]:
    """Resolve report period from a free-form chat message."""
    now = datetime.now()
    today = now.date()
    text = str(message or "").lower()

    dates = DATE_PATTERN.findall(message or "")
    if len(dates) >= 2:
        start = _parse_user_date(dates[0])
        end_inclusive = _parse_user_date(dates[1])
        if end_inclusive < start:
            raise ValueError("Дата окончания периода раньше даты начала.")
        end_exclusive = end_inclusive + timedelta(days=1)
        return {
            "type": "custom",
            "slug": f"{start:%Y%m%d}_{end_inclusive:%Y%m%d}",
            "start": start,
            "end": end_exclusive,
            "label": f"{_format_date(start)} - {_format_date(end_inclusive)}",
        }

    if "сегодня" in text or "за день" in text:
        return {
            "type": "today",
            "slug": f"today_{today:%Y%m%d}",
            "start": today,
            "end": today + timedelta(days=1),
            "label": f"сегодня, {_format_date(today)}",
        }

    if "год" in text:
        start = date(today.year, 1, 1)
        return {
            "type": "year",
            "slug": f"year_{today.year}",
            "start": start,
            "end": date(today.year + 1, 1, 1),
            "label": f"{today.year} год",
        }

    # Low-level fallback: chat flow asks for a period before calling this path.
    week_start = today - timedelta(days=today.weekday())
    week_end_exclusive = week_start + timedelta(days=7)
    return {
        "type": "current_week",
        "slug": f"week_{week_start:%Y%m%d}",
        "start": week_start,
        "end": week_end_exclusive,
        "label": f"текущая неделя, {_format_date(week_start)} - {_format_date(week_end_exclusive - timedelta(days=1))}",
    }


def _build_rov_jql(prefix: str, period: Dict[str, Any]) -> str:
    start = period["start"].strftime("%Y-%m-%d")
    end = period["end"].strftime("%Y-%m-%d")
    return (
        f'project = {prefix} AND '
        f'issuetype = "{ROV_ISSUE_TYPE}" AND '
        f"{ROV_START_JQL_FIELD} >= \"{start}\" AND "
        f"{ROV_START_JQL_FIELD} < \"{end}\" "
        f"ORDER BY {ROV_START_JQL_FIELD} ASC, key ASC"
    )


def _domain_label(domain: str) -> str:
    return "delta" if "delta" in str(domain or "").lower() else "sigma"


def _issue_type_name(issue: Dict[str, Any]) -> str:
    return str(((issue.get("fields") or {}).get("issuetype") or {}).get("name") or "")


def _status_name(issue: Dict[str, Any]) -> str:
    return str(((issue.get("fields") or {}).get("status") or {}).get("name") or "")


def _is_linked_release(issue: Dict[str, Any]) -> bool:
    key = str(issue.get("key") or "").strip().upper()
    issue_type = _issue_type_name(issue)
    if issue_type == "Release 2.0":
        return True
    return any(key.startswith(f"{prefix}-") for prefix in get_enabled_release_prefixes())


def _extract_linked_release_info(fields: Dict[str, Any]) -> Dict[str, str]:
    releases = []
    for link in fields.get("issuelinks") or []:
        link_type = link.get("type") or {}
        link_name = str(link_type.get("name") or "")
        inward_name = str(link_type.get("inward") or "")
        outward_name = str(link_type.get("outward") or "")
        is_release_io_link = (
            link_name == "ReleaseIO"
            or "Introduction Order" in inward_name
            or "Introduction Order" in outward_name
        )
        if not is_release_io_link:
            continue

        for linked_issue in (link.get("inwardIssue"), link.get("outwardIssue")):
            if not isinstance(linked_issue, dict) or not _is_linked_release(linked_issue):
                continue
            key = str(linked_issue.get("key") or "").strip()
            if not key:
                continue
            issue_type = _issue_type_name(linked_issue)
            status = _status_name(linked_issue)
            type_status = " / ".join(part for part in (issue_type, status) if part)
            releases.append((key, type_status))

    deduped = {}
    for key, type_status in releases:
        deduped[key] = type_status

    return {
        "linked_release": ", ".join(deduped.keys()),
        "linked_release_type_status": "; ".join(value for value in deduped.values() if value),
    }


def _row_from_rov_issue(issue: Dict[str, Any], domain: str, resolved_fields: Dict[str, Any]) -> Dict[str, Any]:
    fields = issue.get("fields") or {}
    project = fields.get("project") or {}
    status = fields.get("status") or {}
    created_dt = _parse_jira_date(fields.get("created"))
    start_dt = _parse_jira_date(_extract_field_value(fields.get(resolved_fields.get("rov_start"))))
    end_dt = _parse_jira_date(_extract_field_value(fields.get(resolved_fields.get("rov_end"))))
    linked_release = _extract_linked_release_info(fields)
    month_dt = start_dt or created_dt

    return {
        "Домен": _domain_label(domain),
        "Проект": project.get("name") or project.get("key") or "",
        "Ключ РОВ": issue.get("key") or "",
        "Название РОВ": fields.get("summary") or "",
        "Статус": status.get("name") or "",
        "Дата создания": _format_datetime(created_dt),
        "Начало внедрения": _format_datetime(start_dt),
        "Окончание внедрения": _format_datetime(end_dt),
        "Связанный релиз": linked_release["linked_release"],
        "Тип/статус связанного релиза": linked_release["linked_release_type_status"],
        "_month_sort": month_dt.strftime("%Y-%m") if month_dt else "",
        "_month_label": f"{MONTH_NAMES.get(month_dt.month, '')} {month_dt.year}" if month_dt else "без даты",
    }


def load_rov_statistics_items(period: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows = []
    for (domain, token), prefixes in _get_domain_groups().items():
        if not domain or not token:
            logging.warning("ROV statistics: skip domain group with missing domain/token for prefixes %s", prefixes)
            continue

        resolved_fields = _resolve_field_ids(domain, token)
        fields_to_load = _release_monitor_rov_fields_to_load(resolved_fields) | {
            "project",
            "created",
            "summary",
            "status",
            "issuetype",
            "issuelinks",
        }

        for prefix in prefixes:
            jql = _build_rov_jql(prefix, period)
            try:
                issues = _execute_search(domain, token, jql, fields_to_load)
                rows.extend(_row_from_rov_issue(issue, domain, resolved_fields) for issue in issues)
                logging.info("ROV statistics: loaded %s rows for prefix %s", len(issues), prefix)
            except Exception as exc:
                logging.error("ROV statistics: failed to load prefix %s from %s: %s", prefix, domain, exc)
                raise

    rows.sort(key=lambda row: (row.get("Начало внедрения") or "", row.get("Ключ РОВ") or ""))
    return rows


def _summary_frames(df: pd.DataFrame) -> Dict[str, pd.DataFrame]:
    if df.empty:
        empty = pd.DataFrame(columns=["Показатель", "Количество РОВ"])
        return {
            "projects": empty,
            "statuses": empty,
            "months": empty,
            "total": pd.DataFrame([{"Показатель": "Общий итог", "Количество РОВ": 0}]),
        }

    by_project = (
        df.groupby("Проект", dropna=False)
        .size()
        .reset_index(name="Количество РОВ")
        .sort_values(["Количество РОВ", "Проект"], ascending=[False, True])
    )
    by_status = (
        df.groupby("Статус", dropna=False)
        .size()
        .reset_index(name="Количество РОВ")
        .sort_values(["Количество РОВ", "Статус"], ascending=[False, True])
    )
    by_month = (
        df.groupby(["_month_sort", "_month_label"], dropna=False)
        .size()
        .reset_index(name="Количество РОВ")
        .sort_values("_month_sort")
    )
    by_month = by_month.rename(columns={"_month_label": "Месяц"}).drop(columns=["_month_sort"])
    total = pd.DataFrame([{"Показатель": "Общий итог", "Количество РОВ": int(len(df))}])
    return {
        "projects": by_project,
        "statuses": by_status,
        "months": by_month,
        "total": total,
    }


def _write_summary_sheet(writer: pd.ExcelWriter, df: pd.DataFrame) -> None:
    summary = _summary_frames(df)
    sheet_name = "Сводная"
    workbook = writer.book
    worksheet = workbook.create_sheet(sheet_name)
    writer.sheets[sheet_name] = worksheet

    current_row = 1
    blocks = [
        ("Количество РОВ по проектам", summary["projects"]),
        ("Количество РОВ по статусам", summary["statuses"]),
        ("Количество РОВ по месяцам", summary["months"]),
        ("Общий итог", summary["total"]),
    ]
    for title, block_df in blocks:
        worksheet.cell(row=current_row, column=1, value=title)
        current_row += 1
        block_df.to_excel(writer, sheet_name=sheet_name, startrow=current_row - 1, index=False)
        current_row += len(block_df.index) + 3


def save_rov_statistics_excel(rows: List[Dict[str, Any]], period: Dict[str, Any]) -> Dict[str, Any]:
    _ensure_reports_dir()
    report_id = uuid.uuid4().hex
    filename = f"rov_statistics_{period['slug']}_{datetime.now():%Y%m%d_%H%M%S}_{report_id}.xlsx"
    path = REPORTS_DIR / filename

    df = pd.DataFrame(rows)
    if df.empty:
        df = pd.DataFrame(columns=[
            "Домен",
            "Проект",
            "Ключ РОВ",
            "Название РОВ",
            "Статус",
            "Дата создания",
            "Начало внедрения",
            "Окончание внедрения",
            "Связанный релиз",
            "Тип/статус связанного релиза",
            "_month_sort",
            "_month_label",
        ])

    export_df = df.drop(columns=[col for col in ("_month_sort", "_month_label") if col in df.columns])
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        export_df.to_excel(writer, sheet_name="Все РОВ", index=False)
        _write_summary_sheet(writer, df)

    cleanup_old_rov_statistics_reports()

    return {
        "report_id": report_id,
        "path": str(path),
        "filename": filename,
        "total": int(len(rows)),
        "period": period,
    }


def generate_rov_statistics_excel(message: str = "") -> Dict[str, Any]:
    period = resolve_rov_statistics_period(message)
    rows = load_rov_statistics_items(period)
    return save_rov_statistics_excel(rows, period)


def get_rov_statistics_report_path(report_id: str) -> str:
    safe_id = re.sub(r"[^a-zA-Z0-9_-]", "", str(report_id or ""))
    if not safe_id or not REPORTS_DIR.exists():
        return ""
    for path in REPORTS_DIR.glob(f"*_{safe_id}.xlsx"):
        return str(path)
    for path in REPORTS_DIR.glob("*.xlsx"):
        if safe_id in path.name:
            return str(path)
    return ""


def cleanup_old_rov_statistics_reports(max_age_hours: int = ROV_STATS_MAX_AGE_HOURS) -> int:
    if not REPORTS_DIR.exists():
        return 0
    now = time.time()
    removed = 0
    for path in REPORTS_DIR.glob("*.xlsx"):
        try:
            age_hours = (now - path.stat().st_mtime) / 3600
            if age_hours > max_age_hours:
                path.unlink()
                removed += 1
                logging.info("ROV statistics: removed old report %s", path.name)
        except Exception as exc:
            logging.warning("ROV statistics: failed to cleanup %s: %s", path, exc)
    if removed:
        logging.info("ROV statistics: cleaned up %s old reports", removed)
    return removed
