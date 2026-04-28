"""
Service for release analytics and HTML report generation.
"""

from __future__ import annotations

import html
import re
from collections import Counter
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple


EM_DASH = "\u2014"


class ReleaseReportService:
    """Generate analytics and HTML reports for the release monitor table."""

    def generate_current_week_plan_report(self, items: List[Dict[str, Any]]) -> Dict[str, Any]:
        normalized_items = list(items or [])
        period = self._get_current_week_period()

        filtered_items: List[Dict[str, Any]] = []
        for item in normalized_items:
            if bool(item.get("is_unnumbered")):
                continue
            if bool(item.get("is_cancelled")):
                continue

            event_date = self._get_item_week_datetime(item)
            if event_date is None:
                continue
            if not (period["start"] <= event_date <= period["end"]):
                continue

            filtered_items.append(item)

        filtered_items.sort(
            key=lambda item: (
                self._get_item_week_datetime(item) or datetime.min,
                str(item.get("release_key") or ""),
                str(item.get("rov_key") or ""),
            ),
            reverse=False,
        )

        final_items = [item for item in filtered_items if bool(item.get("is_final"))]
        reroll_items = [item for item in filtered_items if bool(item.get("is_reroll"))]
        hotfix_items = [item for item in filtered_items if self._is_hotfix(item)]
        system_counter = Counter()
        status_counter = Counter()

        for item in filtered_items:
            system_name = self._normalize_system_name(item.get("system_name"), item.get("source_prefix"))
            system_counter[system_name] += 1
            status_name = str(item.get("release_status") or "Не указан").strip() or "Не указан"
            status_counter[status_name] += 1

        return {
            "report_mode": "current_week_plan",
            "period": {
                "start": period["start"].strftime("%Y-%m-%d"),
                "end": period["end"].strftime("%Y-%m-%d"),
                "label": period["label"],
                "mode": "current_week_plan",
            },
            "filters": {
                "kind": "current_week_plan",
                "system": "",
            },
            "statistics": {
                "total": len(filtered_items),
                "installed": len(final_items),
                "rerolls": len(reroll_items),
                "hotfixes": len(hotfix_items),
                "systems": dict(system_counter.most_common()),
                "statuses": dict(status_counter.most_common()),
            },
            "items": filtered_items,
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }

    def generate_release_report(
        self,
        items: List[Dict[str, Any]],
        *,
        quarter: Optional[int] = None,
        year: Optional[int] = None,
        days: Optional[int] = None,
        original_message: str = "",
    ) -> Dict[str, Any]:
        normalized_items = list(items or [])
        message_lower = (original_message or "").lower()

        report_kind = self._detect_report_kind(message_lower)
        system_filter = self._detect_system_filter(message_lower)
        period = self._resolve_period(quarter=quarter, year=year, days=days, message_lower=message_lower)

        filtered_items: List[Dict[str, Any]] = []
        for item in normalized_items:
            if system_filter and not self._matches_system(item, system_filter):
                continue
            if not self._matches_kind(item, report_kind):
                continue

            event_date = self._get_item_event_datetime(item)
            if event_date is None:
                continue
            if not (period["start"] <= event_date <= period["end"]):
                continue

            filtered_items.append(item)

        filtered_items.sort(
            key=lambda item: (
                self._get_item_event_datetime(item) or datetime.min,
                str(item.get("release_key") or ""),
                str(item.get("rov_key") or ""),
            ),
            reverse=True,
        )

        final_items = [item for item in filtered_items if bool(item.get("is_final"))]
        cancelled_items = [item for item in filtered_items if bool(item.get("is_cancelled"))]
        reroll_items = [item for item in filtered_items if bool(item.get("is_reroll"))]
        hotfix_items = [item for item in filtered_items if self._is_hotfix(item)]

        system_counter = Counter()
        duty_counter = Counter()
        responsible_counter = Counter()

        for item in filtered_items:
            system_name = self._normalize_system_name(item.get("system_name"), item.get("source_prefix"))
            system_counter[system_name] += 1

            duty_owner = str(item.get("psi_owner") or "").strip()
            if duty_owner:
                duty_counter[duty_owner] += 1

            for responsible in item.get("psi_responsibles") or []:
                responsible_name = str(responsible or "").strip()
                if responsible_name:
                    responsible_counter[responsible_name] += 1

        return {
            "period": {
                "start": period["start"].strftime("%Y-%m-%d"),
                "end": period["end"].strftime("%Y-%m-%d"),
                "label": period["label"],
                "mode": period["mode"],
                "quarter": period.get("quarter"),
                "year": period.get("year"),
                "days": period.get("days"),
            },
            "filters": {
                "kind": report_kind,
                "system": system_filter or "",
            },
            "statistics": {
                "total": len(filtered_items),
                "visible_total": len([item for item in filtered_items if not bool(item.get("is_unnumbered"))]),
                "hidden_total": len([item for item in filtered_items if bool(item.get("is_unnumbered"))]),
                "installed": len(final_items),
                "cancelled": len(cancelled_items),
                "rerolls": len(reroll_items),
                "hotfixes": len(hotfix_items),
                "systems": dict(system_counter.most_common()),
                "duty_owners": dict(duty_counter.most_common()),
                "responsibles": dict(responsible_counter.most_common()),
            },
            "items": filtered_items,
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }

    def generate_html_report(self, report_data: Dict[str, Any]) -> str:
        if report_data.get("report_mode") == "current_week_plan":
            return self.generate_current_week_plan_html(report_data)

        period = report_data["period"]
        stats = report_data["statistics"]
        filters = report_data.get("filters", {})
        items = report_data.get("items", [])
        visible_items = [item for item in items if not bool(item.get("is_unnumbered"))]
        hidden_items = [item for item in items if bool(item.get("is_unnumbered"))]

        visible_rows = self._render_rows(visible_items)
        hidden_rows = self._render_rows(hidden_items)

        hidden_section = ""
        if hidden_items:
            hidden_section = f"""
            <details class="hidden-table-card">
                <summary>Показать скрытые релизы ({len(hidden_items)})</summary>
                <div class="hidden-table-wrap">
                    {self._build_table(hidden_rows)}
                </div>
            </details>
            """

        def render_counter_list(title: str, data: Dict[str, int], filter_type: str = "") -> str:
            if not data:
                return f'<div class="mini-card"><h4>{html.escape(title)}</h4><p>Нет данных</p></div>'
            entries_parts = []
            for name, count in list(data.items())[:8]:
                label = html.escape(name)
                if filter_type:
                    entries_parts.append(
                        f'<li><button type="button" class="counter-filter" '
                        f'data-filter-type="{html.escape(filter_type)}" '
                        f'data-filter-value="{label}"><span class="counter-filter-label">{label}</span><strong>{count}</strong></button></li>'
                    )
                else:
                    entries_parts.append(f"<li><span>{label}</span><strong>{count}</strong></li>")
            entries = "".join(entries_parts)
            return f"""
            <div class="mini-card">
                <h4>{html.escape(title)}</h4>
                <ul class="counter-list">{entries}</ul>
            </div>
            """

        system_filter_html = ""
        if filters.get("system"):
            system_filter_html = f"<br>Система: <strong>{html.escape(filters['system'])}</strong>"

        return f"""<!doctype html>
<html lang="ru">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Отчет по релизам — {html.escape(period['label'])}</title>
    <style>
        * {{
            box-sizing: border-box;
        }}
        body {{
            margin: 0;
            font-family: "Segoe UI", Tahoma, sans-serif;
            background: linear-gradient(180deg, #eef3ff 0%, #f8fafc 100%);
            color: #18212f;
            padding: 24px;
        }}
        .container {{
            max-width: 1480px;
            margin: 0 auto;
        }}
        .hero {{
            background: #ffffff;
            border-radius: 22px;
            padding: 28px 32px;
            box-shadow: 0 18px 44px rgba(15, 23, 42, 0.08);
            margin-bottom: 22px;
        }}
        .hero h1 {{
            margin: 0 0 10px;
            font-size: 32px;
        }}
        .hero .meta {{
            color: #526071;
            font-size: 15px;
            line-height: 1.6;
        }}
        .summary-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
            gap: 16px;
            margin-bottom: 22px;
        }}
        .summary-card {{
            background: #ffffff;
            border-radius: 18px;
            padding: 22px;
            box-shadow: 0 16px 36px rgba(15, 23, 42, 0.07);
        }}
        .summary-card-button {{
            width: 100%;
            border: 0;
            background: transparent;
            padding: 0;
            text-align: left;
            font: inherit;
            color: inherit;
            cursor: pointer;
        }}
        .summary-card-button:hover h3,
        .summary-card-button:hover .value {{
            color: #0d6efd;
        }}
        .summary-card h3 {{
            margin: 0 0 10px;
            color: #5b6878;
            font-size: 13px;
            text-transform: uppercase;
            letter-spacing: 0.08em;
        }}
        .summary-card .value {{
            font-size: 38px;
            font-weight: 800;
        }}
        .detail-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
            gap: 16px;
            margin-bottom: 22px;
        }}
        .mini-card {{
            background: #ffffff;
            border-radius: 18px;
            padding: 20px 22px;
            box-shadow: 0 14px 34px rgba(15, 23, 42, 0.07);
        }}
        .mini-card h4 {{
            margin: 0 0 14px;
            font-size: 16px;
        }}
        .mini-card p {{
            margin: 0;
            color: #6b7785;
        }}
        .counter-list {{
            list-style: none;
            padding: 0;
            margin: 0;
            display: grid;
            gap: 10px;
        }}
        .counter-list li {{
            color: #394657;
        }}
        .counter-filter {{
            width: 100%;
            border: 0;
            background: transparent;
            padding: 0;
            display: flex;
            justify-content: space-between;
            gap: 12px;
            color: #394657;
            font: inherit;
            text-align: left;
            cursor: pointer;
        }}
        .counter-filter-label {{
            flex: 1 1 auto;
            padding-right: 18px;
        }}
        .counter-filter:hover span {{
            color: #0d6efd;
            text-decoration: underline;
        }}
        .report-toolbar {{
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 12px;
            margin-bottom: 16px;
            flex-wrap: wrap;
        }}
        .report-filter-state {{
            color: #607083;
            font-size: 14px;
        }}
        .report-filter-state strong {{
            color: #1d2a3a;
        }}
        .clear-filter-btn {{
            border: 1px solid #d7e1ef;
            border-radius: 999px;
            background: #ffffff;
            color: #1d2a3a;
            padding: 8px 14px;
            font: inherit;
            cursor: pointer;
        }}
        .clear-filter-btn[hidden] {{
            display: none;
        }}
        .counter-list strong {{
            color: #0d6efd;
        }}
        .table-card {{
            background: #ffffff;
            border-radius: 22px;
            padding: 22px;
            box-shadow: 0 18px 44px rgba(15, 23, 42, 0.08);
            overflow: hidden;
        }}
        .table-card h3 {{
            margin: 0 0 8px;
            font-size: 22px;
        }}
        .table-card .hint {{
            margin: 0 0 16px;
            color: #607083;
            font-size: 14px;
        }}
        .hidden-table-card {{
            margin-top: 18px;
            border: 1px solid #e2e8f0;
            border-radius: 18px;
            background: #f8fafc;
            overflow: hidden;
        }}
        .hidden-table-card summary {{
            list-style: none;
            cursor: pointer;
            padding: 16px 18px;
            font-weight: 700;
            color: #2a3647;
            user-select: none;
        }}
        .hidden-table-card summary::-webkit-details-marker {{
            display: none;
        }}
        .hidden-table-card summary::before {{
            content: "+";
            display: inline-flex;
            width: 20px;
            height: 20px;
            margin-right: 10px;
            align-items: center;
            justify-content: center;
            border-radius: 999px;
            background: #e7eefc;
            color: #2251b2;
            font-weight: 800;
        }}
        .hidden-table-card[open] summary::before {{
            content: "−";
        }}
        .hidden-table-wrap {{
            padding: 0 18px 18px;
            overflow: hidden;
        }}
        table {{
            width: 100%;
            border-collapse: collapse;
            table-layout: fixed;
        }}
        th, td {{
            padding: 12px 10px;
            text-align: left;
            border-bottom: 1px solid #e6ebf3;
            vertical-align: top;
            white-space: normal;
            word-break: break-word;
            overflow-wrap: anywhere;
        }}
        th {{
            background: #f5f8fe;
            color: #546274;
            font-size: 12px;
            text-transform: uppercase;
            letter-spacing: 0.06em;
        }}
        th:nth-child(1), td:nth-child(1) {{
            width: 54px;
        }}
        th:nth-child(2), td:nth-child(2) {{
            width: 26%;
        }}
        th:nth-child(3), td:nth-child(3),
        th:nth-child(4), td:nth-child(4) {{
            width: 9%;
        }}
        th:nth-child(5), td:nth-child(5) {{
            width: 10%;
        }}
        th:nth-child(6), td:nth-child(6) {{
            width: 8%;
        }}
        th:nth-child(7), td:nth-child(7) {{
            width: 10%;
        }}
        th:nth-child(8), td:nth-child(8),
        th:nth-child(9), td:nth-child(9) {{
            width: 8%;
        }}
        th:nth-child(10), td:nth-child(10) {{
            width: 8%;
        }}
        th:nth-child(11), td:nth-child(11),
        th:nth-child(12), td:nth-child(12) {{
            width: 12%;
        }}
        tr.state-final {{
            background: rgba(47, 158, 68, 0.07);
        }}
        tr.state-cancelled {{
            background: rgba(224, 49, 49, 0.07);
        }}
        .footer {{
            color: #6b7785;
            text-align: center;
            padding: 14px 0 4px;
            font-size: 14px;
        }}
    </style>
</head>
<body>
    <div class="container">
        <section class="hero">
            <h1>Отчет по релизам</h1>
            <div class="meta">
                Период: <strong>{html.escape(period['label'])}</strong><br>
                Фильтр: <strong>{html.escape(self._get_kind_title(filters.get('kind', 'installed')))}</strong>
                {system_filter_html}
            </div>
        </section>

        <section class="summary-grid">
            {self._render_summary_card("Всего строк", stats['total'], "summary", "all")}
            {self._render_summary_card("В основной таблице", stats['visible_total'], "summary", "visible")}
            {self._render_summary_card("Скрыто по умолчанию", stats['hidden_total'], "summary", "hidden")}
            {self._render_summary_card("Установлен на ПРОМ", stats['installed'], "summary", "installed")}
            {self._render_summary_card("Перераскатки", stats['rerolls'], "summary", "reroll")}
            {self._render_summary_card("Хотфиксы", stats['hotfixes'], "summary", "hotfix")}
            {self._render_summary_card("Отменено", stats['cancelled'], "summary", "cancelled")}
        </section>

        <section class="detail-grid">
            {render_counter_list("По системам", stats["systems"], "system")}
            {render_counter_list("По дежурным", stats["duty_owners"], "duty_owner")}
            {render_counter_list("По ответственным", stats["responsibles"], "responsible")}
        </section>

        <section class="table-card">
            <h3>Список релизов</h3>
            <p class="hint">Основная таблица повторяет правила видимости блока релизов: верхняя ненумеруемая группа скрыта по умолчанию.</p>
            <div class="report-toolbar">
                <div class="report-filter-state">Фильтр по людям: <strong id="activeFilterLabel">не выбран</strong></div>
                <button type="button" class="clear-filter-btn" id="clearReportFilter" hidden>Сбросить фильтр</button>
            </div>
            {self._build_table(visible_rows if visible_rows else '<tr><td colspan="12">За выбранный период записи не найдены.</td></tr>')}
            {hidden_section}
        </section>

        <div class="footer">Отчет сформирован: {html.escape(report_data['generated_at'])}</div>
    </div>
    <script>
        (function () {{
            const filterButtons = Array.from(document.querySelectorAll('.counter-filter'));
            const summaryButtons = Array.from(document.querySelectorAll('.summary-card-button'));
            const clearButton = document.getElementById('clearReportFilter');
            const labelNode = document.getElementById('activeFilterLabel');
            let currentType = '';
            let currentValue = '';

            function normalize(value) {{
                return String(value || '').trim().toLowerCase();
            }}

            function applyFilter() {{
                const rows = Array.from(document.querySelectorAll('tbody tr[data-duty-owner], tbody tr[data-responsibles]'));
                rows.forEach((row) => {{
                    if (!currentType || !currentValue) {{
                        row.hidden = false;
                        return;
                    }}
                    const system = normalize(row.dataset.system);
                    const dutyOwner = normalize(row.dataset.dutyOwner);
                    const responsibles = normalize(row.dataset.responsibles).split('|').filter(Boolean);
                    const isUnnumbered = row.dataset.unnumbered === '1';
                    const isFinal = row.dataset.final === '1';
                    const isCancelled = row.dataset.cancelled === '1';
                    const isReroll = row.dataset.reroll === '1';
                    const isHotfix = row.dataset.hotfix === '1';
                    let matched = false;
                    if (currentType === 'summary') {{
                        if (currentValue === 'all') {{
                            matched = true;
                        }} else if (currentValue === 'visible') {{
                            matched = !isUnnumbered;
                        }} else if (currentValue === 'hidden') {{
                            matched = isUnnumbered;
                        }} else if (currentValue === 'installed') {{
                            matched = isFinal;
                        }} else if (currentValue === 'cancelled') {{
                            matched = isCancelled;
                        }} else if (currentValue === 'reroll') {{
                            matched = isReroll;
                        }} else if (currentValue === 'hotfix') {{
                            matched = isHotfix;
                        }}
                    }} else if (currentType === 'system') {{
                        matched = system === normalize(currentValue);
                    }} else if (currentType === 'duty_owner') {{
                        matched = dutyOwner === normalize(currentValue);
                    }} else if (currentType === 'responsible') {{
                        matched = responsibles.includes(normalize(currentValue));
                    }}
                    row.hidden = !matched;
                }});
                if (!currentType || !currentValue) {{
                    labelNode.textContent = 'не выбран';
                    clearButton.hidden = true;
                }} else {{
                    let suffix = '';
                    if (currentType === 'summary') {{
                        const summaryLabels = {{
                            all: ' (все строки)',
                            visible: ' (основная таблица)',
                            hidden: ' (скрытые по умолчанию)',
                            installed: ' (установлен на ПРОМ)',
                            cancelled: ' (отменено)',
                            reroll: ' (перераскатки)',
                            hotfix: ' (хотфиксы)',
                        }};
                        suffix = summaryLabels[currentValue] || '';
                    }} else if (currentType === 'system') {{
                        suffix = ' (система)';
                    }} else if (currentType === 'duty_owner') {{
                        suffix = ' (дежурный)';
                    }} else if (currentType === 'responsible') {{
                        suffix = ' (ответственный)';
                    }}
                    labelNode.textContent = currentValue + suffix;
                    clearButton.hidden = false;
                }}
                const hiddenDetails = document.querySelector('.hidden-table-card');
                if (hiddenDetails) {{
                    if (currentType === 'summary' && currentValue === 'hidden') {{
                        hiddenDetails.open = true;
                    }}
                }}
            }}

            filterButtons.forEach((button) => {{
                button.addEventListener('click', () => {{
                    currentType = button.dataset.filterType || '';
                    currentValue = button.dataset.filterValue || '';
                    applyFilter();
                }});
            }});

            summaryButtons.forEach((button) => {{
                button.addEventListener('click', () => {{
                    currentType = button.dataset.filterType || '';
                    currentValue = button.dataset.filterValue || '';
                    applyFilter();
                }});
            }});

            clearButton.addEventListener('click', () => {{
                currentType = '';
                currentValue = '';
                applyFilter();
            }});
        }})();
    </script>
</body>
</html>"""

    def generate_current_week_plan_html(self, report_data: Dict[str, Any]) -> str:
        period = report_data["period"]
        stats = report_data["statistics"]
        items = report_data.get("items", [])
        rows_html = self._render_week_rows(items)

        def render_counter_list(title: str, data: Dict[str, int], filter_type: str = "") -> str:
            if not data:
                return f'<div class="mini-card"><h4>{html.escape(title)}</h4><p>Нет данных</p></div>'
            entries_parts = []
            for name, count in list(data.items())[:8]:
                label = html.escape(name)
                if filter_type:
                    entries_parts.append(
                        f'<li><button type="button" class="counter-filter" '
                        f'data-filter-type="{html.escape(filter_type)}" '
                        f'data-filter-value="{label}"><span class="counter-filter-label">{label}</span><strong>{count}</strong></button></li>'
                    )
                else:
                    entries_parts.append(f"<li><span>{label}</span><strong>{count}</strong></li>")
            entries = "".join(entries_parts)
            return f"""
            <div class="mini-card">
                <h4>{html.escape(title)}</h4>
                <ul class="counter-list">{entries}</ul>
            </div>
            """

        return f"""<!doctype html>
<html lang="ru">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Релизы текущей недели — {html.escape(period['label'])}</title>
    <style>
        * {{
            box-sizing: border-box;
        }}
        body {{
            margin: 0;
            font-family: "Segoe UI", Tahoma, sans-serif;
            background: linear-gradient(180deg, #eef3ff 0%, #f8fafc 100%);
            color: #18212f;
            padding: 24px;
        }}
        .container {{
            max-width: 1320px;
            margin: 0 auto;
        }}
        .hero {{
            background: #ffffff;
            border-radius: 22px;
            padding: 28px 32px;
            box-shadow: 0 18px 44px rgba(15, 23, 42, 0.08);
            margin-bottom: 22px;
        }}
        .hero h1 {{
            margin: 0 0 10px;
            font-size: 32px;
        }}
        .hero .meta {{
            color: #526071;
            font-size: 15px;
            line-height: 1.6;
        }}
        .summary-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
            gap: 16px;
            margin-bottom: 22px;
        }}
        .summary-card {{
            background: #ffffff;
            border-radius: 18px;
            padding: 22px;
            box-shadow: 0 16px 36px rgba(15, 23, 42, 0.07);
        }}
        .summary-card-button {{
            width: 100%;
            border: 0;
            background: transparent;
            padding: 0;
            text-align: left;
            font: inherit;
            color: inherit;
            cursor: pointer;
        }}
        .summary-card-button:hover h3,
        .summary-card-button:hover .value {{
            color: #0d6efd;
        }}
        .summary-card h3 {{
            margin: 0 0 10px;
            color: #5b6878;
            font-size: 13px;
            text-transform: uppercase;
            letter-spacing: 0.08em;
        }}
        .summary-card .value {{
            font-size: 38px;
            font-weight: 800;
        }}
        .detail-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
            gap: 16px;
            margin-bottom: 22px;
        }}
        .mini-card {{
            background: #ffffff;
            border-radius: 18px;
            padding: 20px 22px;
            box-shadow: 0 14px 34px rgba(15, 23, 42, 0.07);
        }}
        .mini-card h4 {{
            margin: 0 0 14px;
            font-size: 16px;
        }}
        .counter-list {{
            list-style: none;
            padding: 0;
            margin: 0;
            display: grid;
            gap: 10px;
        }}
        .counter-filter {{
            width: 100%;
            border: 0;
            background: transparent;
            padding: 0;
            display: flex;
            justify-content: space-between;
            gap: 12px;
            color: #394657;
            font: inherit;
            text-align: left;
            cursor: pointer;
        }}
        .counter-filter-label {{
            flex: 1 1 auto;
            padding-right: 18px;
        }}
        .counter-filter:hover span {{
            color: #0d6efd;
            text-decoration: underline;
        }}
        .counter-list strong {{
            color: #0d6efd;
        }}
        .table-card {{
            background: #ffffff;
            border-radius: 22px;
            padding: 22px;
            box-shadow: 0 18px 44px rgba(15, 23, 42, 0.08);
            overflow: hidden;
        }}
        .table-card h3 {{
            margin: 0 0 8px;
            font-size: 22px;
        }}
        .hint {{
            margin: 0 0 16px;
            color: #607083;
            font-size: 14px;
        }}
        .report-toolbar {{
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 12px;
            margin-bottom: 16px;
            flex-wrap: wrap;
        }}
        .report-filter-state {{
            color: #607083;
            font-size: 14px;
        }}
        .report-filter-state strong {{
            color: #1d2a3a;
        }}
        .clear-filter-btn {{
            border: 1px solid #d7e1ef;
            border-radius: 999px;
            background: #ffffff;
            color: #1d2a3a;
            padding: 8px 14px;
            font: inherit;
            cursor: pointer;
        }}
        .clear-filter-btn[hidden] {{
            display: none;
        }}
        table {{
            width: 100%;
            border-collapse: collapse;
            table-layout: fixed;
        }}
        th, td {{
            padding: 12px 10px;
            text-align: left;
            border-bottom: 1px solid #e6ebf3;
            vertical-align: top;
            white-space: normal;
            word-break: break-word;
            overflow-wrap: anywhere;
        }}
        th {{
            background: #f5f8fe;
            color: #546274;
            font-size: 12px;
            text-transform: uppercase;
            letter-spacing: 0.06em;
        }}
        th:nth-child(1), td:nth-child(1) {{ width: 54px; }}
        th:nth-child(2), td:nth-child(2) {{ width: 28%; }}
        th:nth-child(3), td:nth-child(3), th:nth-child(4), td:nth-child(4) {{ width: 10%; }}
        th:nth-child(5), td:nth-child(5) {{ width: 10%; }}
        th:nth-child(6), td:nth-child(6) {{ width: 9%; }}
        th:nth-child(7), td:nth-child(7), th:nth-child(8), td:nth-child(8) {{ width: 9%; }}
        th:nth-child(9), td:nth-child(9) {{ width: 8%; }}
        tr.state-overdue {{ background: rgba(224, 49, 49, 0.07); }}
        tr.state-today {{ background: rgba(245, 159, 0, 0.08); }}
        .footer {{
            color: #6b7785;
            text-align: center;
            padding: 14px 0 4px;
            font-size: 14px;
        }}
    </style>
</head>
<body>
    <div class="container">
        <section class="hero">
            <h1>Предстоящие релизы текущей недели</h1>
            <div class="meta">
                Период: <strong>{html.escape(period['label'])}</strong><br>
                В отчет включены только видимые релизы текущей недели, включая уже установленные на ПРОМ строки.
            </div>
        </section>

        <section class="summary-grid">
            {self._render_summary_card("Всего релизов недели", stats['total'], "summary", "all")}
            {self._render_summary_card("Установлен на ПРОМ", stats['installed'], "summary", "installed")}
            {self._render_summary_card("Перераскатки", stats['rerolls'], "summary", "reroll")}
            {self._render_summary_card("Хотфиксы", stats['hotfixes'], "summary", "hotfix")}
        </section>

        <section class="detail-grid">
            {render_counter_list("По системам", stats["systems"], "system")}
            {render_counter_list("По статусам", stats["statuses"], "status")}
        </section>

        <section class="table-card">
            <h3>Список релизов недели</h3>
            <p class="hint">Скрытые по умолчанию релизы в отчет не включаются. Установленные на ПРОМ строки текущей недели учитываются.</p>
            <div class="report-toolbar">
                <div class="report-filter-state">Фильтр: <strong id="activeFilterLabel">не выбран</strong></div>
                <button type="button" class="clear-filter-btn" id="clearReportFilter" hidden>Сбросить фильтр</button>
            </div>
            <table>
                <thead>
                    <tr>
                        <th>№</th>
                        <th>Название</th>
                        <th>ID релиза</th>
                        <th>ID РОВ</th>
                        <th>Сборка</th>
                        <th>Тип</th>
                        <th>Дата начала</th>
                        <th>Дата окончания</th>
                        <th>Система</th>
                    </tr>
                </thead>
                <tbody>
                    {rows_html if rows_html else '<tr><td colspan="9">На текущую неделю предстоящие релизы не найдены.</td></tr>'}
                </tbody>
            </table>
        </section>

        <div class="footer">Отчет сформирован: {html.escape(report_data['generated_at'])}</div>
    </div>
    <script>
        (function () {{
            const filterButtons = Array.from(document.querySelectorAll('.counter-filter'));
            const summaryButtons = Array.from(document.querySelectorAll('.summary-card-button'));
            const clearButton = document.getElementById('clearReportFilter');
            const labelNode = document.getElementById('activeFilterLabel');
            let currentType = '';
            let currentValue = '';

            function normalize(value) {{
                return String(value || '').trim().toLowerCase();
            }}

            function applyFilter() {{
                const rows = Array.from(document.querySelectorAll('tbody tr[data-system], tbody tr[data-status]'));
                rows.forEach((row) => {{
                    if (!currentType || !currentValue) {{
                        row.hidden = false;
                        return;
                    }}
                    const system = normalize(row.dataset.system);
                    const status = normalize(row.dataset.status);
                    const isReroll = row.dataset.reroll === '1';
                    const isHotfix = row.dataset.hotfix === '1';
                    let matched = false;

                    if (currentType === 'summary') {{
                        if (currentValue === 'all') {{
                            matched = true;
                        }} else if (currentValue === 'installed') {{
                            matched = row.dataset.final === '1';
                        }} else if (currentValue === 'reroll') {{
                            matched = isReroll;
                        }} else if (currentValue === 'hotfix') {{
                            matched = isHotfix;
                        }}
                    }} else if (currentType === 'system') {{
                        matched = system === normalize(currentValue);
                    }} else if (currentType === 'status') {{
                        matched = status === normalize(currentValue);
                    }}

                    row.hidden = !matched;
                }});

                if (!currentType || !currentValue) {{
                    labelNode.textContent = 'не выбран';
                    clearButton.hidden = true;
                }} else {{
                    let suffix = '';
                    if (currentType === 'summary') {{
                        const summaryLabels = {{
                            all: ' (все релизы недели)',
                            installed: ' (установлен на ПРОМ)',
                            reroll: ' (перераскатки)',
                            hotfix: ' (хотфиксы)',
                        }};
                        suffix = summaryLabels[currentValue] || '';
                    }} else if (currentType === 'system') {{
                        suffix = ' (система)';
                    }} else if (currentType === 'status') {{
                        suffix = ' (статус)';
                    }}
                    labelNode.textContent = currentValue + suffix;
                    clearButton.hidden = false;
                }}
            }}

            filterButtons.forEach((button) => {{
                button.addEventListener('click', () => {{
                    currentType = button.dataset.filterType || '';
                    currentValue = button.dataset.filterValue || '';
                    applyFilter();
                }});
            }});

            summaryButtons.forEach((button) => {{
                button.addEventListener('click', () => {{
                    currentType = button.dataset.filterType || '';
                    currentValue = button.dataset.filterValue || '';
                    applyFilter();
                }});
            }});

            clearButton.addEventListener('click', () => {{
                currentType = '';
                currentValue = '';
                applyFilter();
            }});
        }})();
    </script>
</body>
</html>"""

    def _build_table(self, rows_html: str) -> str:
        return f"""
        <table>
            <thead>
                <tr>
                    <th>№</th>
                    <th>Название</th>
                    <th>ID релиза</th>
                    <th>ID РОВ</th>
                    <th>Сборка</th>
                    <th>Тип</th>
                    <th>Статус</th>
                    <th>Дата начала</th>
                    <th>Дата окончания</th>
                    <th>Система</th>
                    <th>Дежурный</th>
                    <th>Ответственные</th>
                </tr>
            </thead>
            <tbody>
                {rows_html}
            </tbody>
        </table>
        """

    def _render_rows(self, rows_source: List[Dict[str, Any]]) -> str:
        rows = []
        for item in rows_source:
            row_kind = self._get_item_kind_label(item)
            responsibles = ", ".join(item.get("psi_responsibles") or []) or EM_DASH
            duty_owner = str(item.get("psi_owner") or "").strip() or EM_DASH
            row_state = "final" if item.get("is_final") else "cancelled" if item.get("is_cancelled") else "active"
            row_title = " / ".join(
                [part for part in (item.get("release_name_lines") or [])[:2] if str(part or "").strip()]
            ) or str(item.get("release_summary") or "")
            system_name = self._normalize_system_name(item.get("system_name"), item.get("source_prefix"))
            responsibles_attr = "|".join(
                str(value or "").strip().lower()
                for value in (item.get("psi_responsibles") or [])
                if str(value or "").strip()
            )
            rows.append(
                f"""
                <tr class="state-{row_state}"
                    data-system="{html.escape(system_name.lower())}"
                    data-duty-owner="{html.escape(str(duty_owner).lower())}"
                    data-responsibles="{html.escape(responsibles_attr)}"
                    data-unnumbered="{'1' if bool(item.get('is_unnumbered')) else '0'}"
                    data-final="{'1' if bool(item.get('is_final')) else '0'}"
                    data-cancelled="{'1' if bool(item.get('is_cancelled')) else '0'}"
                    data-reroll="{'1' if bool(item.get('is_reroll')) else '0'}"
                    data-hotfix="{'1' if self._is_hotfix(item) else '0'}">
                    <td>{html.escape(str(item.get('release_number') or EM_DASH))}</td>
                    <td>{html.escape(row_title)}</td>
                    <td>{html.escape(str(item.get('release_key') or EM_DASH))}</td>
                    <td>{html.escape(str(item.get('rov_key') or EM_DASH))}</td>
                    <td>{html.escape(str(item.get('release_version') or EM_DASH))}</td>
                    <td>{html.escape(row_kind)}</td>
                    <td>{html.escape(str(item.get('release_status') or EM_DASH))}</td>
                    <td>{html.escape(str(item.get('deployment_start') or EM_DASH))}</td>
                    <td>{html.escape(str(item.get('deployment_end') or EM_DASH))}</td>
                    <td>{html.escape(system_name)}</td>
                    <td>{html.escape(duty_owner)}</td>
                    <td>{html.escape(responsibles)}</td>
                </tr>
                """
            )
        return "".join(rows)

    def _render_week_rows(self, rows_source: List[Dict[str, Any]]) -> str:
        rows = []
        for index, item in enumerate(rows_source, start=1):
            row_kind = self._get_item_kind_label(item)
            row_state = "overdue" if item.get("is_overdue") else "today" if item.get("is_today") else "active"
            row_title = " / ".join(
                [part for part in (item.get("release_name_lines") or [])[:2] if str(part or "").strip()]
            ) or str(item.get("release_summary") or "")
            system_name = self._normalize_system_name(item.get("system_name"), item.get("source_prefix"))
            status_name = str(item.get("release_status") or "Не указан").strip() or "Не указан"
            rows.append(
                f"""
                <tr class="state-{row_state}"
                    data-system="{html.escape(system_name.lower())}"
                    data-status="{html.escape(status_name.lower())}"
                    data-final="{'1' if bool(item.get('is_final')) else '0'}"
                    data-reroll="{'1' if bool(item.get('is_reroll')) else '0'}"
                    data-hotfix="{'1' if self._is_hotfix(item) else '0'}">
                    <td>{index}</td>
                    <td>{html.escape(row_title)}</td>
                    <td>{html.escape(str(item.get('release_key') or EM_DASH))}</td>
                    <td>{html.escape(str(item.get('rov_key') or EM_DASH))}</td>
                    <td>{html.escape(str(item.get('release_version') or EM_DASH))}</td>
                    <td>{html.escape(row_kind)}</td>
                    <td>{html.escape(str(item.get('deployment_start') or EM_DASH))}</td>
                    <td>{html.escape(str(item.get('deployment_end') or EM_DASH))}</td>
                    <td>{html.escape(system_name)}</td>
                </tr>
                """
            )
        return "".join(rows)

    def _resolve_period(
        self,
        *,
        quarter: Optional[int],
        year: Optional[int],
        days: Optional[int],
        message_lower: str,
    ) -> Dict[str, Any]:
        current_year = datetime.now().year
        explicit_year = self._extract_year(message_lower)

        if quarter:
            period_year = year or explicit_year or current_year
            start, end = self._get_quarter_dates(quarter, period_year)
            return {
                "mode": "quarter",
                "quarter": quarter,
                "year": period_year,
                "start": start,
                "end": end,
                "label": f"{quarter} квартал {period_year}",
            }

        if explicit_year and ("\u0433\u043e\u0434" in message_lower or re.search(r"\b20\d{2}\b", message_lower)):
            start = datetime(explicit_year, 1, 1)
            end = datetime(explicit_year, 12, 31, 23, 59, 59)
            return {
                "mode": "year",
                "year": explicit_year,
                "start": start,
                "end": end,
                "label": f"{explicit_year} год",
            }

        if "\u0433\u043e\u0434" in message_lower:
            start = datetime(current_year, 1, 1)
            end = datetime(current_year, 12, 31, 23, 59, 59)
            return {
                "mode": "year",
                "year": current_year,
                "start": start,
                "end": end,
                "label": f"{current_year} год",
            }

        if days:
            end = datetime.now()
            start = end - timedelta(days=days)
            return {
                "mode": "days",
                "days": days,
                "start": start,
                "end": end,
                "label": f"Последние {days} дней",
            }

        default_quarter = (datetime.now().month - 1) // 3 + 1
        start, end = self._get_quarter_dates(default_quarter, current_year)
        return {
            "mode": "quarter",
            "quarter": default_quarter,
            "year": current_year,
            "start": start,
            "end": end,
            "label": f"{default_quarter} квартал {current_year}",
        }

    def _get_quarter_dates(self, quarter: int, year: int) -> Tuple[datetime, datetime]:
        quarter_months = {
            1: (1, 3),
            2: (4, 6),
            3: (7, 9),
            4: (10, 12),
        }
        start_month, end_month = quarter_months[quarter]
        start = datetime(year, start_month, 1)
        if end_month == 12:
            end = datetime(year, 12, 31, 23, 59, 59)
        else:
            end = datetime(year, end_month + 1, 1) - timedelta(seconds=1)
        return start, end

    def _detect_report_kind(self, message_lower: str) -> str:
        message_lower = (message_lower or "").strip().lower()

        if "\u043f\u0435\u0440\u0435\u0440\u0430\u0441\u043a\u0430\u0442" in message_lower:
            return "reroll"
        if "\u0445\u043e\u0442\u0444\u0438\u043a\u0441" in message_lower:
            return "hotfix"
        if "\u043e\u0442\u043c\u0435\u043d" in message_lower:
            return "cancelled"
        if (
            "\u0443\u0441\u0442\u0430\u043d\u043e\u0432" in message_lower
            or "\u043f\u0440\u043e\u043c" in message_lower
            or "\u0444\u0438\u043d\u0430\u043b\u044c\u043d" in message_lower
        ):
            return "installed"
        if "\u0441\u043a\u043e\u043b\u044c\u043a\u043e" in message_lower and "\u0440\u0435\u043b\u0438\u0437" in message_lower:
            return "installed"
        return "all"

    def _detect_system_filter(self, message_lower: str) -> str:
        markers = {
            "clm": "CLM",
            "\u0444\u043e\u043a\u0443\u0441": "\u0424\u043e\u043a\u0443\u0441",
            "focus": "\u0424\u043e\u043a\u0443\u0441",
            "\u0430\u0438\u0441\u0442": "\u0410\u0418\u0421\u0422",
            "aigas": "AIGAS",
            "helperai": "HELPERAI",
            "emrm": "EMRM",
            "smecsc": "SMECSC",
            "smeclm": "SMECLM",
        }
        for marker, normalized in markers.items():
            if marker in message_lower:
                return normalized
        return ""

    def _extract_year(self, message_lower: str) -> Optional[int]:
        match = re.search(r"\b(20\d{2})\b", message_lower)
        if match:
            return int(match.group(1))
        return None

    def _matches_system(self, item: Dict[str, Any], system_filter: str) -> bool:
        system_name = self._normalize_system_name(item.get("system_name"), item.get("source_prefix")).lower()
        prefix = str(item.get("source_prefix") or "").strip().lower()
        release_key = str(item.get("release_key") or "").strip().lower()
        target = self._normalize_system_name(system_filter, "").lower()
        return target in {system_name, prefix} or target in release_key

    def _matches_kind(self, item: Dict[str, Any], report_kind: str) -> bool:
        if report_kind == "all":
            return True
        if report_kind == "installed":
            return bool(item.get("is_final"))
        if report_kind == "cancelled":
            return bool(item.get("is_cancelled"))
        if report_kind == "reroll":
            return bool(item.get("is_reroll"))
        if report_kind == "hotfix":
            return self._is_hotfix(item)
        return True

    def _is_hotfix(self, item: Dict[str, Any]) -> bool:
        if item.get("is_reroll"):
            return False
        version = str(item.get("release_version") or "").strip().upper()
        return version.startswith("P-")

    def _get_item_event_datetime(self, item: Dict[str, Any]) -> Optional[datetime]:
        for key in ("deployment_end_iso", "deployment_start_iso", "sort_date", "created_sort_date"):
            raw_value = str(item.get(key) or "").strip()
            if not raw_value:
                continue
            try:
                return datetime.fromisoformat(raw_value)
            except ValueError:
                continue
        return None

    def _get_item_week_datetime(self, item: Dict[str, Any]) -> Optional[datetime]:
        for key in ("deployment_start_iso", "deployment_end_iso", "sort_date", "created_sort_date"):
            raw_value = str(item.get(key) or "").strip()
            if not raw_value:
                continue
            try:
                return datetime.fromisoformat(raw_value)
            except ValueError:
                continue
        return None

    def _get_item_kind_label(self, item: Dict[str, Any]) -> str:
        if item.get("is_reroll"):
            return "Перераскатка"
        if self._is_hotfix(item):
            return "Хотфикс"
        if item.get("is_cancelled"):
            return "Отменено"
        return "Релиз"

    def _get_kind_title(self, kind: str) -> str:
        return {
            "all": "Все релизы периода",
            "installed": "Установленные на ПРОМ",
            "cancelled": "Отмененные релизы",
            "reroll": "Перераскатки",
            "hotfix": "Хотфиксы",
        }.get(kind, "Все релизы периода")

    def _normalize_system_name(self, raw_value: Any, prefix_value: Any) -> str:
        raw = str(raw_value or "").strip()
        prefix = str(prefix_value or "").strip().upper()

        mojibake_map = {
            "Р¤РѕРєСѓСЃ": "Фокус",
            "Р¤РѕРєСѓС": "Фокус",
            "РђРРЎРў": "АИСТ",
            "РђРРЎРў ": "АИСТ",
        }
        normalized = mojibake_map.get(raw, raw)
        upper_normalized = normalized.upper()

        if prefix == "SMECSC":
            return "АИСТ"
        if prefix in {"AIGAS", "HELPERAI"}:
            return "AI-Агенты"
        if prefix == "EMRM":
            return "EMRM"
        if prefix in {"SMECLM", "CLM"}:
            return "CLM"
        if "ФОКУС" in upper_normalized or "FOCUS" in upper_normalized:
            return "EMRM"
        if "AI-" in upper_normalized or "AI " in upper_normalized or "АГЕНТ" in upper_normalized:
            return "AI-Агенты"
        if "АИСТ" in upper_normalized:
            return "АИСТ"
        if upper_normalized == "EMRM":
            return "EMRM"
        if "CLM" in upper_normalized:
            return "CLM"
        return normalized or "Не указано"

    def _get_current_week_period(self) -> Dict[str, Any]:
        now = datetime.now()
        week_start = datetime(now.year, now.month, now.day) - timedelta(days=now.weekday())
        week_end = week_start + timedelta(days=6, hours=23, minutes=59, seconds=59)
        return {
            "start": week_start,
            "end": week_end,
            "label": f"{week_start.strftime('%d.%m.%Y')} - {week_end.strftime('%d.%m.%Y')}",
        }

    def _render_summary_card(self, title: str, value: int, filter_type: str, filter_value: str) -> str:
        return f"""
        <article class="summary-card">
            <button type="button" class="summary-card-button" data-filter-type="{html.escape(filter_type)}" data-filter-value="{html.escape(filter_value)}">
                <h3>{html.escape(title)}</h3>
                <div class="value">{value}</div>
            </button>
        </article>
        """


_release_report_service: Optional[ReleaseReportService] = None


def get_release_report_service() -> ReleaseReportService:
    global _release_report_service
    if _release_report_service is None:
        _release_report_service = ReleaseReportService()
    return _release_report_service
