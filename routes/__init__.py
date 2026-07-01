from flask import request


def register_routes(app):
    from routes.main_routes import main_bp
    from routes.release_routes import release_bp
    from routes.mpr_routes import mpr_bp
    from routes.sms_routes import sms_bp
    from routes.dashboard_routes import dashboard_bp
    from routes.chatbot_routes import chatbot_bp
    from routes.sup_parameters_routes import sup_parameters_bp

    app.register_blueprint(main_bp)
    app.register_blueprint(release_bp)
    app.register_blueprint(mpr_bp)
    app.register_blueprint(sms_bp)
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(chatbot_bp)
    app.register_blueprint(sup_parameters_bp)

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
