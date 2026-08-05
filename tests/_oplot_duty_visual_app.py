from __future__ import annotations

from unittest import mock

from flask import Flask

from tests._support import PROJECT_ROOT, prepare_config_import

prepare_config_import()

from routes.dashboard_routes import dashboard_bp
from routes.main_routes import main_bp
from services.oplot_ui_service import register_oplot_ui


def create_app() -> Flask:
    app = Flask(__name__, template_folder=str(PROJECT_ROOT / "templates"), static_folder=str(PROJECT_ROOT / "static"))
    app.config.update(TESTING=False, SECRET_KEY="stage6-visual-fixture")
    app.add_url_rule("/release/monitor-init", endpoint="release.release_monitor_init", view_func=lambda: "ok", methods=["POST"])
    app.add_url_rule("/release/monitor-generate", endpoint="release.release_monitor_generate", view_func=lambda: "ok", methods=["POST"])
    app.add_url_rule("/sms/release-monitor/generate", endpoint="sms.generate_release_monitor_sms", view_func=lambda: "ok", methods=["POST"])
    app.add_url_rule("/sms/templates", endpoint="sms.get_sms_templates", view_func=lambda: "ok")
    app.add_url_rule("/sms/templates/<profile>", endpoint="sms.save_sms_template", view_func=lambda profile: profile, methods=["POST"])
    app.register_blueprint(main_bp)
    app.register_blueprint(dashboard_bp)
    register_oplot_ui(app)

    task_base = {
        "url": "#", "status": "В работе", "assignee_name": "Дежурный Oplot",
        "priority": "Высокий", "created": "2026-08-01", "updated": "2026-08-04",
        "days_in_progress": 3, "has_sup_tag": False, "has_logi_tag": False,
        "has_db_tag": False, "has_infra_tag": False, "has_role_tag": False,
        "has_vnedrenie_tag": False, "sup_detected_by": "", "logi_detected_by": "",
        "psi_detected_by": "",
    }
    dashboard_data = {
        "sup_tasks": [{**task_base, "key": "SUP-101", "summary": "Проверить обращение промышленного контура", "has_sup_tag": True}],
        "logi_tasks": [{**task_base, "key": "LOGI-202", "summary": "Разобрать диагностические журналы", "has_logi_tag": True}],
        "vnedrenie_prom_tasks": [{**task_base, "key": "REL-303", "summary": "Контроль внедрения релиза на ПРОМ", "has_vnedrenie_tag": True}],
        "vnedrenie_psi_tasks": [],
        "assignee_stats": {}, "dashboard_assignees": [], "release_monitor": [],
        "release_monitor_summary": {}, "release_monitor_meta": {},
    }
    schedule_status = {"status": "authoritative", "authoritative": True, "updated_at": "сейчас", "warnings": []}
    schedule_grid = {
        "label": "Август 2026", "warnings": [],
        "days": [{"day": 4, "weekday": "вт", "date": "2026-08-04"}, {"day": 5, "weekday": "ср", "date": "2026-08-05"}],
        "shifts": [{"code": "D", "short_name": "Д", "name": "Дневная смена", "color": "#206bc4", "text_color": "#ffffff"}],
        "employees": [{"employee_name": "Иванов Иван Иванович", "warning": False, "assignments": {"4": "D", "5": "D"}}],
    }
    assignment_control = {
        "period": {"label": "3–9 августа 2026"},
        "meta": {"snapshot_at": "04.08.2026 18:30", "week_key": "2026-W32", "view_revision": "fixture"},
        "statistics": {"missing_responsible": 1, "available_candidates": 1, "reserve_candidates": 1, "excluded_candidates": 1},
        "availability_authoritative": True,
        "missing_responsible": [{"row_key": "fixture-release", "release_key": "EMRM-100", "rov_key": "EMRM-101", "release_summary": "Synthetic release for visual review", "deployment_start": "06.08.2026", "system_name": "Oplot", "ke_id": "14290659", "release_version": "v10", "release_status": "Планируется", "duty_owner": "Дежурный Oplot", "candidate_availability": {}}],
        "candidates": {
            "available": [{"name": "Иванов И.И.", "availability": "available", "metrics": {"week": 1, "active": 2, "quarter": 8, "year": 20}, "reasons": []}],
            "reserve": [{"name": "Петров П.П.", "availability": "reserve", "metrics": {"week": 2, "active": 1, "quarter": 7, "year": 18}, "reasons": ["Резерв по графику"]}],
            "excluded": [{"name": "Сидоров С.С.", "availability": "excluded", "metrics": {"week": 0, "active": 0, "quarter": 4, "year": 12}, "reasons": ["Отпуск"]}],
        },
    }
    patches = (
        mock.patch("routes.main_routes.get_stats", return_value={"total_combined": 0, "release": {"total": 0, "last_30_days": 0, "percentage": 0}, "sms": {"total": 0, "last_30_days": 0, "percentage": 0}}),
        mock.patch("routes.main_routes.is_maintenance_enabled", return_value=False),
        mock.patch("routes.dashboard_routes.get_dashboard_data", return_value=dashboard_data),
        mock.patch("routes.dashboard_routes.get_hidden_tasks", return_value={}),
        mock.patch("routes.dashboard_routes.prune_hidden_tasks"),
        mock.patch("routes.dashboard_routes.is_maintenance_enabled", return_value=False),
        mock.patch("routes.dashboard_routes.check_multiple_approvals", return_value={}),
        mock.patch("routes.dashboard_routes.get_release_monitor_assignment_center_data", return_value=assignment_control),
        mock.patch("routes.dashboard_routes.get_release_monitor_week_responsible_recommendations", return_value={"items": []}),
        mock.patch("routes.dashboard_routes.get_release_monitor_snapshot", return_value={"items": [], "summary": {}, "meta": {}}),
        mock.patch("routes.dashboard_routes._build_consistent_release_monitor_model", return_value=({"snapshot": {"items": [], "summary": {}, "meta": {}}, "template_hints": {}, "reviewer_options": [], "sms_profile_availability": {}}, {"view_revision": "fixture", "updated_at": "сейчас"})),
        mock.patch("routes.dashboard_routes.get_duty_schedule_provider_status", return_value=schedule_status),
        mock.patch("routes.dashboard_routes.get_duty_schedule_months", return_value={"months": ["2026-08"]}),
        mock.patch("routes.dashboard_routes.get_duty_schedule_month", return_value=schedule_grid),
    )
    for patcher in patches:
        patcher.start()
    app.extensions["stage6_visual_patches"] = patches
    return app


app = create_app()
