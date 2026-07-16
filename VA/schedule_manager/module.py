from __future__ import annotations

import logging

from flask import Blueprint, abort, request

from services.sup_admin_auth_service import (
    csrf_protect_request,
    get_sup_admin_csrf_token,
    require_sup_admin_request,
)

from .config import APP_VERSION, ensure_runtime_dirs
from .routes.api import api_bp
from .routes.integration import integration_bp
from .routes.settings import settings_bp
from .routes.web import web_bp
from .url_helpers import public_url_for


logger = logging.getLogger(__name__)


def create_schedule_manager_blueprint() -> Blueprint:
    ensure_runtime_dirs()

    parent_bp = Blueprint(
        "va_schedule_manager",
        __name__,
        template_folder="templates",
        static_folder="static",
        static_url_path="/static",
    )

    @parent_bp.before_request
    def _protect_schedule_manager():
        if request.endpoint and request.endpoint.endswith(".static"):
            return None

        auth_response = require_sup_admin_request()
        if auth_response is not None:
            return auth_response

        csrf_response = csrf_protect_request()
        if csrf_response is not None:
            return csrf_response

        return None

    @parent_bp.context_processor
    def _inject_schedule_manager_context():
        return {
            "va_admin_csrf_token": get_sup_admin_csrf_token(),
            "va_admin_panel_url": public_url_for("sup_parameters.sup_parameters_page"),
            "va_url_for": public_url_for,
            "va_schedule_manager_version": APP_VERSION,
        }

    parent_bp.register_blueprint(web_bp)
    parent_bp.register_blueprint(settings_bp)
    parent_bp.register_blueprint(api_bp, url_prefix="/api")
    parent_bp.register_blueprint(integration_bp)

    return parent_bp


def disabled_sample_endpoint():
    abort(404)
