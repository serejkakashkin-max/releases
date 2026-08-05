from __future__ import annotations

from flask import Blueprint, Flask, jsonify, render_template

from tests._support import PROJECT_ROOT
from services.mpr_ui_service import build_mpr_ui_config
from services.oplot_ui_service import register_oplot_ui


def create_visual_app() -> Flask:
    app = Flask(
        __name__,
        template_folder=str(PROJECT_ROOT / "templates"),
        static_folder=str(PROJECT_ROOT / "static"),
    )
    app.config.update(TESTING=True, SECRET_KEY="mpr-visual-fixture")
    app.add_url_rule("/", endpoint="main.index", view_func=lambda: "home")
    blueprint = Blueprint("mpr", __name__)
    templates = [{"code": "synthetic", "name": "Тестовый шаблон", "filename": "synthetic/template.docx"}]

    @blueprint.get("/mpr")
    def mpr_page():
        return render_template(
            "mpr.html",
            templates=templates,
            mpr_ui_config=build_mpr_ui_config(templates=templates),
        )

    @blueprint.post("/mpr/preview")
    def mpr_preview():
        return jsonify({
            "success": True,
            "rows_count": 12,
            "packages": [
                {"code": "mcod", "label": "МЦОД", "datacenters": ["МегаЦОД"], "rows_count": 5, "available": True},
                {"code": "scod_vavilova", "label": "СЦОД и Вавилова", "datacenters": ["Сколково", "Вавилова"], "rows_count": 7, "available": True},
            ],
            "unmapped": [],
        })

    @blueprint.post("/mpr/generate")
    def mpr_generate():
        return b"synthetic"

    app.register_blueprint(blueprint)
    register_oplot_ui(app)
    return app


if __name__ == "__main__":
    create_visual_app().run(host="127.0.0.1", port=5097, debug=False, use_reloader=False)
