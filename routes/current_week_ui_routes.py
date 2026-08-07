"""Presentation adapter for the live Current Week page.

The saved Current Week HTML report remains an export artifact. The live route
is rebound to a Jinja/Oplot-shell view without changing its URL, report data
contract, filtering rules, or saved-report endpoints.
"""

from __future__ import annotations

import logging

from flask import make_response, render_template

from services.release_monitor_service import get_release_monitor_snapshot
from services.release_report_service import get_release_report_service


CURRENT_WEEK_ENDPOINT = "dashboard.current_week_release_monitor_page"


def current_week_release_monitor_page():
    """Render the live Current Week monitor inside the shared Oplot shell."""
    try:
        snapshot = get_release_monitor_snapshot() or {}
        items = snapshot.get("items", []) if isinstance(snapshot, dict) else []
        report_service = get_release_report_service()
        report_data = report_service.generate_current_week_plan_report(items)
        rows_html = report_service._render_week_rows(report_data.get("items", []))
        html_content = render_template(
            "release_monitor_current_week.html",
            report_data=report_data,
            rows_html=rows_html,
        )
        response = make_response(html_content)
        response.headers["Content-Type"] = "text/html; charset=utf-8"
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        return response
    except Exception:
        logging.exception("Ошибка открытия мониторинга релизов текущей недели")
        html_content = render_template(
            "release_monitor_current_week.html",
            report_data=None,
            rows_html="",
            current_week_error="Ошибка открытия мониторинга релизов текущей недели. Попробуйте обновить страницу позже.",
        )
        return make_response(html_content, 500)


def install_current_week_ui(app) -> None:
    """Replace only the live endpoint handler after dashboard blueprint registration."""
    if CURRENT_WEEK_ENDPOINT not in app.view_functions:
        raise RuntimeError(f"Current Week endpoint is not registered: {CURRENT_WEEK_ENDPOINT}")
    app.view_functions[CURRENT_WEEK_ENDPOINT] = current_week_release_monitor_page
