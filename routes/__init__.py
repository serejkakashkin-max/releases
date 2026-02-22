from flask import request, after_this_request

def register_routes(app):
    from routes.main_routes import main_bp
    from routes.release_routes import release_bp
    from routes.sms_routes import sms_bp
    from routes.dashboard_routes import dashboard_bp  # НОВОЕ: импорт дашборда
    
    app.register_blueprint(main_bp)
    app.register_blueprint(release_bp)
    app.register_blueprint(sms_bp)
    app.register_blueprint(dashboard_bp)  # НОВОЕ: регистрация blueprint дашборда
    
    @app.after_request
    def add_header(response):
        if not request.path.startswith('/static'):
            response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
        return response