from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatchcase
from typing import Callable

from flask import Flask, current_app, request
from werkzeug.routing import BuildError

from services.public_url_service import public_url_for


AvailabilityPredicate = Callable[[], bool]


@dataclass(frozen=True)
class NavigationItem:
    id: str
    label: str
    endpoint: str
    icon: str
    group: str
    active_patterns: tuple[str, ...]
    feature_flag: str | None = None
    availability: AvailabilityPredicate | None = None


_GROUPS = (
    ("main", "Основное"),
    ("documents", "Документы"),
    ("management", "Управление"),
    ("support", "Поддержка"),
)

_NAVIGATION = (
    NavigationItem("home", "Главная", "main.index", "home", "main", ("main.index",)),
    NavigationItem(
        "release-monitor", "Блок релизов", "dashboard.release_monitor_page", "rocket", "main",
        ("dashboard.release_monitor_page",),
    ),
    NavigationItem(
        "duty-dashboard", "Рабочий стол дежурного", "dashboard.dashboard", "dashboard", "main",
        ("dashboard.dashboard",),
    ),
    NavigationItem("mpr", "МПР", "mpr.mpr_page", "document", "documents", ("mpr.mpr_page",)),
    NavigationItem(
        "document-templates", "Центр шаблонов", "document_templates.index", "template", "documents",
        ("document_templates.*",), feature_flag="DOCUMENT_TEMPLATE_CENTER_ENABLED",
    ),
    NavigationItem(
        "assignment-center", "Центр назначений", "dashboard.release_monitor_assignment_center_page",
        "users", "management", ("dashboard.release_monitor_assignment_center_page",),
    ),
    NavigationItem(
        "duty-schedule", "График дежурств", "dashboard.release_monitor_duty_schedule_page",
        "calendar", "management", ("dashboard.release_monitor_duty_schedule_page",),
    ),
    NavigationItem(
        "sup-parameters", "СУП-параметры", "sup_parameters.sup_parameters_page", "settings", "management",
        ("sup_parameters.*",),
    ),
    NavigationItem(
        "schedule-manager", "Schedule Manager", "va_schedule_manager.web.index", "schedule", "management",
        ("va_schedule_manager.web.*", "va_schedule_manager.settings.*"),
    ),
    NavigationItem("help", "Помощь", "main.help_page", "help", "support", ("main.help_page",)),
)


def safe_public_url_for(endpoint: str, **values) -> str | None:
    if not endpoint or endpoint not in current_app.view_functions:
        return None
    try:
        url = public_url_for(endpoint, **values)
    except (BuildError, KeyError, RuntimeError, TypeError, ValueError):
        return None
    if not url or url.startswith(("http://", "https://", "//")):
        return None
    return url


def _is_available(item: NavigationItem) -> bool:
    if item.feature_flag and not current_app.config.get(item.feature_flag, False):
        return False
    if item.endpoint not in current_app.view_functions:
        return False
    if item.availability is not None:
        try:
            if not item.availability():
                return False
        except Exception:
            return False
    return True


def build_oplot_navigation(current_endpoint: str | None = None) -> list[dict]:
    endpoint = str(current_endpoint or "")
    active_claimed = False
    grouped: dict[str, list[dict]] = {group_id: [] for group_id, _label in _GROUPS}
    for item in _NAVIGATION:
        if not _is_available(item):
            continue
        url = safe_public_url_for(item.endpoint)
        if url is None:
            continue
        matches = any(fnmatchcase(endpoint, pattern) for pattern in item.active_patterns)
        active = bool(matches and not active_claimed)
        active_claimed = active_claimed or active
        grouped[item.group].append({
            "id": item.id,
            "label": item.label,
            "url": url,
            "icon": item.icon,
            "active": active,
        })
    return [
        {"id": group_id, "label": label, "items": grouped[group_id]}
        for group_id, label in _GROUPS if grouped[group_id]
    ]


def register_oplot_ui(app: Flask) -> None:
    if app.extensions.get("oplot_ui_registered"):
        return
    app.extensions["oplot_ui_registered"] = True

    @app.context_processor
    def _oplot_ui_context():
        endpoint = request.endpoint or ""
        return {
            "oplot_navigation": build_oplot_navigation(endpoint),
            "oplot_current_endpoint": endpoint,
            "public_url_for": public_url_for,
            "safe_public_url_for": safe_public_url_for,
        }
