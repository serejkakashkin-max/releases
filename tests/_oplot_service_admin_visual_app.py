from __future__ import annotations

from flask import Blueprint, Flask, jsonify, render_template

from tests._support import PROJECT_ROOT
from services.oplot_ui_service import register_oplot_ui
from services.sup_ui_service import build_sup_admin_ui_config


def create_visual_app() -> Flask:
    app = Flask(
        __name__,
        template_folder=str(PROJECT_ROOT / "templates"),
        static_folder=str(PROJECT_ROOT / "static"),
    )
    app.config.update(TESTING=True, SECRET_KEY="sup-admin-visual")
    app.add_url_rule("/", endpoint="main.index", view_func=lambda: "home")
    app.add_url_rule(
        "/release-monitor",
        endpoint="dashboard.release_monitor_page",
        view_func=lambda: "release monitor",
    )

    session = Blueprint("sup_admin_session", __name__, url_prefix="/admin/session")
    session.add_url_rule("/login", endpoint="login", view_func=lambda: jsonify(success=True), methods=["POST"])
    session.add_url_rule("/logout", endpoint="logout", view_func=lambda: jsonify(success=True), methods=["POST"])
    session.add_url_rule("/status", endpoint="status", view_func=lambda: jsonify(success=True))
    app.register_blueprint(session)

    sup = Blueprint("sup_parameters", __name__, url_prefix="/admin")

    @sup.get("/sup-parameters")
    def sup_parameters_page():
        return render_template(
            "sup_parameters.html",
            token_configured=True,
            sup_admin_ui_config=build_sup_admin_ui_config(
                schedule_manager_metadata={"status": "disabled", "loaded": False}
            ),
        )

    endpoints = (
        ("/sup-parameters/data", "sup_parameters_data", ["GET"]),
        ("/sup-parameters/save", "sup_parameters_save", ["POST"]),
        ("/sup-parameters/release-monitor-refresh", "release_monitor_refresh_data", ["GET"]),
        ("/sup-parameters/release-monitor-refresh/start", "release_monitor_refresh_start", ["POST"]),
        ("/sup-parameters/employee-directory", "employee_directory_data", ["GET"]),
        ("/sup-parameters/employee-directory/save", "employee_directory_save", ["POST"]),
        ("/sup-parameters/va-schedule-manager", "va_schedule_manager_admin_data", ["GET"]),
        ("/sup-parameters/va-schedule-manager/employees/<employee_id>/settings", "va_employee_settings_save", ["POST", "PUT"]),
        ("/sup-parameters/va-schedule-manager/competencies", "va_competency_add", ["POST"]),
        ("/sup-parameters/va-schedule-manager/competencies/<code>", "va_competency_update", ["PATCH"]),
    )
    for path, endpoint, methods in endpoints:
        sup.add_url_rule(path, endpoint=endpoint, view_func=lambda **_values: jsonify(success=False), methods=methods)
    app.register_blueprint(sup)
    register_oplot_ui(app)
    return app


if __name__ == "__main__":
    create_visual_app().run(host="127.0.0.1", port=5098, debug=False, use_reloader=False)
