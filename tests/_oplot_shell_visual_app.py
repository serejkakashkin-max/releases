from __future__ import annotations

import json
import sys
from pathlib import Path

from flask import Flask, render_template
from jinja2 import ChoiceLoader, FileSystemLoader
from werkzeug.serving import make_server


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from services.oplot_ui_service import register_oplot_ui


def build_app() -> Flask:
    app = Flask("oplot-shell-visual", template_folder=str(PROJECT_ROOT / "templates"), static_folder=str(PROJECT_ROOT / "static"))
    app.config.update(TESTING=False, SECRET_KEY="visual-shell-secret")
    app.jinja_loader = ChoiceLoader([
        app.jinja_loader,
        FileSystemLoader(str(PROJECT_ROOT / "tests" / "fixtures" / "templates")),
    ])
    routes = (
        ("/", "main.index"),
        ("/help", "main.help_page"),
        ("/dashboard", "dashboard.dashboard"),
        ("/release-monitor", "dashboard.release_monitor_page"),
        ("/mpr", "mpr.mpr_page"),
        ("/dashboard/release-monitor/assignment-center", "dashboard.release_monitor_assignment_center_page"),
        ("/dashboard/release-monitor/duty-schedule", "dashboard.release_monitor_duty_schedule_page"),
        ("/admin/sup-parameters", "sup_parameters.sup_parameters_page"),
    )
    for index, (path, endpoint) in enumerate(routes):
        app.add_url_rule(
            path,
            endpoint=endpoint,
            view_func=lambda value=index: render_template("oplot_shell_fixture.html", fixture_route=value),
        )

    @app.get("/shell", endpoint="document_templates.index")
    def fixture():
        return render_template("oplot_shell_fixture.html")

    register_oplot_ui(app)
    return app


def main() -> int:
    app = build_app()
    server = make_server("127.0.0.1", 0, app)
    print(json.dumps({"url": f"http://127.0.0.1:{server.server_port}/shell"}), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
