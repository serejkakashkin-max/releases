from __future__ import annotations

from services.public_url_service import public_url_for


def build_duty_dashboard_ui_config(
    *,
    hidden_tasks=None,
    sup_tasks=None,
    logi_tasks=None,
    vnedrenie_prom_tasks=None,
    vnedrenie_psi_tasks=None,
    release_monitor=None,
    release_monitor_summary=None,
    release_monitor_meta=None,
    assignee_stats=None,
) -> dict:
    """Builds presentation-only configuration for the duty dashboard."""
    return {
        "urls": {
            "refresh": public_url_for("dashboard.refresh_dashboard"),
            "approval_check": public_url_for("dashboard.check_approvals"),
            "approval_cache_clear": public_url_for("dashboard.clear_approval_cache_route"),
            "hidden_tasks": public_url_for("dashboard.hide_task_api"),
            "hidden_task_restore": public_url_for("dashboard.show_task_api"),
            "hidden_tasks_restore_all": public_url_for("dashboard.restore_all_tasks_api"),
        },
        "data": {
            "hidden_tasks": hidden_tasks if isinstance(hidden_tasks, dict) else {},
            "dashboard": {
                "sup_tasks": sup_tasks if isinstance(sup_tasks, list) else [],
                "logi_tasks": logi_tasks if isinstance(logi_tasks, list) else [],
                "vnedrenie_prom_tasks": vnedrenie_prom_tasks if isinstance(vnedrenie_prom_tasks, list) else [],
                "vnedrenie_psi_tasks": vnedrenie_psi_tasks if isinstance(vnedrenie_psi_tasks, list) else [],
                "release_monitor": release_monitor if isinstance(release_monitor, list) else [],
                "release_monitor_summary": release_monitor_summary if isinstance(release_monitor_summary, dict) else {},
                "release_monitor_meta": release_monitor_meta if isinstance(release_monitor_meta, dict) else {},
                "assignee_stats": assignee_stats if isinstance(assignee_stats, dict) else {},
            },
        },
        "settings": {
            "page_reload_ms": 3_600_000,
            "approval_delay_ms": 1_000,
        },
    }


def build_assignment_center_ui_config(
    *, poll_interval_ms: int = 15_000, gigachat_enabled: bool = True
) -> dict:
    """Builds endpoint-backed configuration without moving assignment logic."""
    return {
        "urls": {
            "data": public_url_for("dashboard.release_monitor_assignment_center_data"),
            "reviewer": public_url_for("dashboard.update_release_monitor_reviewer"),
            "recommend": public_url_for("dashboard.release_monitor_week_control_recommend"),
        },
        "settings": {
            "poll_interval_ms": int(poll_interval_ms),
            "gigachat_enabled": bool(gigachat_enabled),
        },
    }
