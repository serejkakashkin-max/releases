from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatchcase

from flask import current_app, request

from services.oplot_ui_service import safe_public_url_for


@dataclass(frozen=True)
class ReleaseNavigationItem:
    id: str
    label: str
    endpoint: str
    icon: str
    target: str = "_self"
    feature_flag: str | None = None
    active_patterns: tuple[str, ...] = ()


_RELEASE_NAVIGATION = (
    ReleaseNavigationItem(
        "duty-schedule",
        "График дежурств",
        "dashboard.release_monitor_duty_schedule_page",
        "calendar",
        active_patterns=("dashboard.release_monitor_duty_schedule_page",),
    ),
    ReleaseNavigationItem(
        "assignment-center",
        "Центр назначений",
        "dashboard.release_monitor_assignment_center_page",
        "users",
        target="_blank",
        active_patterns=("dashboard.release_monitor_assignment_center_page",),
    ),
    ReleaseNavigationItem(
        "current-week",
        "Релизы текущей недели",
        "dashboard.current_week_release_monitor_page",
        "calendar",
        target="_blank",
        active_patterns=("dashboard.current_week_release_monitor_page",),
    ),
    ReleaseNavigationItem(
        "document-templates",
        "Центр шаблонов",
        "document_templates.index",
        "template",
        feature_flag="DOCUMENT_TEMPLATE_CENTER_ENABLED",
        active_patterns=("document_templates.*",),
    ),
)


def build_release_navigation(current_endpoint: str | None = None) -> list[dict]:
    endpoint = str(current_endpoint if current_endpoint is not None else request.endpoint or "")
    navigation: list[dict] = []
    for item in _RELEASE_NAVIGATION:
        if item.feature_flag and not current_app.config.get(item.feature_flag, False):
            continue
        url = safe_public_url_for(item.endpoint)
        if url is None:
            continue
        navigation.append({
            "id": item.id,
            "label": item.label,
            "url": url,
            "icon": item.icon,
            "target": item.target,
            "active": any(fnmatchcase(endpoint, pattern) for pattern in item.active_patterns),
        })
    return navigation


def build_release_ui_config() -> dict:
    root_url = safe_public_url_for("main.index") or "/"
    public_base = root_url[:-1] if root_url.endswith("/") else root_url
    return {"public_base": public_base}
