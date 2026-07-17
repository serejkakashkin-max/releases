from flask import request


def register_routes(app):
    from routes.main_routes import main_bp
    from routes.release_routes import release_bp
    from routes.mpr_routes import mpr_bp
    from routes.sms_routes import sms_bp
    from routes.dashboard_routes import dashboard_bp
    from routes.chatbot_routes import chatbot_bp
    from routes.sup_admin_session_routes import sup_admin_session_bp
    from routes.sup_parameters_routes import sup_parameters_bp
    from routes.sandbox_routes import sandbox_bp

    app.register_blueprint(main_bp)
    app.register_blueprint(release_bp)
    app.register_blueprint(mpr_bp)
    app.register_blueprint(sms_bp)
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(chatbot_bp)
    app.register_blueprint(sup_admin_session_bp)
    app.register_blueprint(sup_parameters_bp)
    app.register_blueprint(sandbox_bp)

    try:
        from services.va_schedule_manager_registry import register_va_schedule_manager

        register_va_schedule_manager(app)
    except Exception:
        app.logger.exception("VA Schedule Manager optional registration failed.")

    try:
        from services.ta_incident_auditor_registry import register_ta_incident_auditor

        register_ta_incident_auditor(app)
    except Exception:
        app.logger.exception("TA Incident Auditor optional registration failed.")

    try:
        from services.email_to_sbertrack_service import (
            ensure_email_to_sbertrack_worker_started,
        )

        ensure_email_to_sbertrack_worker_started()
    except Exception as exc:
        app.logger.warning("Email to SberTrack worker was not started: %s", exc)

    @app.after_request
    def add_header(response):
        if not request.path.startswith("/static"):
            response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
        return response
