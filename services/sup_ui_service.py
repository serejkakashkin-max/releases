from __future__ import annotations

from typing import Any, Mapping

from flask import current_app
from werkzeug.routing import BuildError

from services.public_url_service import public_url_for


DEFAULT_TAB = "employees"
DEFAULT_VIEW = "employees"
TAB_ALIASES = {
    "overview": "employees",
    "automation": "mail",
}

_EMPLOYEE_PLACEHOLDER = "__OPLOT_EMPLOYEE_ID__"
_COMPETENCY_PLACEHOLDER = "__OPLOT_COMPETENCY_CODE__"


def _optional_public_url(endpoint: str, **values: str) -> str:
    if endpoint not in current_app.view_functions:
        return ""
    try:
        return public_url_for(endpoint, **values)
    except (BuildError, KeyError, RuntimeError, TypeError, ValueError):
        return ""


def _schedule_manager_presentation(metadata: Mapping[str, Any] | None) -> dict[str, Any]:
    source = metadata if isinstance(metadata, Mapping) else {}
    return {
        "configured": bool(source.get("configured")),
        "package_present": bool(source.get("package_present")),
        "enabled": bool(source.get("enabled")),
        "loaded": bool(source.get("loaded")),
        "status": str(source.get("status") or "not_registered"),
        "error": str(source.get("error") or ""),
        "version": str(source.get("version") or ""),
        "url": _optional_public_url("va_schedule_manager.web.index"),
    }


def build_sup_admin_ui_config(
    *,
    schedule_manager_metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build presentation-only, prefix-safe configuration for the SUP page."""

    return {
        "urls": {
            "data": public_url_for("sup_parameters.sup_parameters_data"),
            "save": public_url_for("sup_parameters.sup_parameters_save"),
            "admin_session_login": public_url_for("sup_admin_session.login"),
            "admin_session_logout": public_url_for("sup_admin_session.logout"),
            "admin_session_status": public_url_for("sup_admin_session.status"),
            "release_refresh_status": public_url_for(
                "sup_parameters.release_monitor_refresh_data"
            ),
            "release_refresh_start": public_url_for(
                "sup_parameters.release_monitor_refresh_start"
            ),
            "employee_directory": public_url_for(
                "sup_parameters.employee_directory_data"
            ),
            "employee_directory_save": public_url_for(
                "sup_parameters.employee_directory_save"
            ),
            "va_admin": public_url_for(
                "sup_parameters.va_schedule_manager_admin_data"
            ),
            "va_employee_settings": public_url_for(
                "sup_parameters.va_employee_settings_save",
                employee_id=_EMPLOYEE_PLACEHOLDER,
            ),
            "va_competencies": public_url_for(
                "sup_parameters.va_competency_add"
            ),
            "va_competency": public_url_for(
                "sup_parameters.va_competency_update",
                code=_COMPETENCY_PLACEHOLDER,
            ),
            "release_monitor": public_url_for(
                "dashboard.release_monitor_page"
            ),
        },
        "url_templates": {
            "va_employee_settings": {
                "value": public_url_for(
                    "sup_parameters.va_employee_settings_save",
                    employee_id=_EMPLOYEE_PLACEHOLDER,
                ),
                "placeholder": _EMPLOYEE_PLACEHOLDER,
            },
            "va_competency": {
                "value": public_url_for(
                    "sup_parameters.va_competency_update",
                    code=_COMPETENCY_PLACEHOLDER,
                ),
                "placeholder": _COMPETENCY_PLACEHOLDER,
            },
        },
        "default_tab": DEFAULT_TAB,
        "default_view": DEFAULT_VIEW,
        "tab_aliases": dict(TAB_ALIASES),
        "schedule_manager": _schedule_manager_presentation(
            schedule_manager_metadata
        ),
    }
