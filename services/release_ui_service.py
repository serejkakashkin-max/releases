from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatchcase

from flask import current_app, request

from services.oplot_ui_service import safe_public_url_for
from services.public_url_service import public_url_for


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


def build_release_ui_config(
    *,
    release_monitor=None,
    release_monitor_summary=None,
    release_monitor_meta=None,
    reviewer_options=None,
    sms_profile_availability=None,
    template_hints=None,
    document_playbooks=None,
    operational_day_start_hour=7,
    maintenance_enabled=False,
    maintenance_scope="release_monitor",
) -> dict:
    urls = {
        "status": public_url_for("dashboard.release_monitor_status"),
        "reviewer": public_url_for("dashboard.update_release_monitor_reviewer"),
        "zni": public_url_for("dashboard.create_release_monitor_zni_issue"),
        "date_override": public_url_for("dashboard.update_release_monitor_date_override"),
        "manual_release_lookup": public_url_for("dashboard.lookup_release_monitor_manual_release_row"),
        "manual_release": public_url_for("dashboard.create_release_monitor_manual_release_row"),
        "manual_override_fields": public_url_for("dashboard.update_release_monitor_manual_override_fields"),
        "manual_override_reset": public_url_for("dashboard.reset_release_monitor_manual_override_row"),
        "manual_distribution": public_url_for("dashboard.update_release_monitor_manual_distribution"),
        "monitor_init": public_url_for("release.release_monitor_init"),
        "monitor_generate": public_url_for("release.release_monitor_generate"),
        "work_mark": public_url_for("dashboard.update_release_monitor_work_mark"),
        "rollout_notes": public_url_for("dashboard.update_release_monitor_rollout_notes"),
        "order": public_url_for("dashboard.save_release_monitor_order"),
        "confluence_sync": public_url_for("dashboard.sync_release_monitor_confluence"),
        "sms_generate": public_url_for("sms.generate_release_monitor_sms"),
        "sms_templates": public_url_for("sms.get_sms_templates"),
        "current_week": public_url_for("dashboard.current_week_release_monitor_page"),
        "assignment_center": public_url_for("dashboard.release_monitor_assignment_center_page"),
    }
    url_templates = {
        "sms_template_profile": public_url_for(
            "sms.save_sms_template",
            profile="__OPLOT_PROFILE__",
        ),
    }
    try:
        start_hour = int(operational_day_start_hour)
    except (TypeError, ValueError):
        start_hour = 7
    scope = str(maintenance_scope or "release_monitor").strip() or "release_monitor"
    return {
        "urls": urls,
        "url_templates": url_templates,
        "data": {
            "release_monitor": release_monitor if isinstance(release_monitor, list) else [],
            "release_monitor_summary": release_monitor_summary if isinstance(release_monitor_summary, dict) else {},
            "release_monitor_meta": release_monitor_meta if isinstance(release_monitor_meta, dict) else {},
            "reviewer_options": reviewer_options if isinstance(reviewer_options, list) else [],
            "sms_profile_availability": sms_profile_availability if isinstance(sms_profile_availability, dict) else {},
            "template_hints": template_hints if isinstance(template_hints, dict) else {},
            "document_playbooks": document_playbooks if isinstance(document_playbooks, list) else [],
        },
        "settings": {
            "operational_day_start_hour": start_hour,
            "maintenance_enabled": bool(maintenance_enabled),
            "maintenance_scope": scope,
        },
    }
