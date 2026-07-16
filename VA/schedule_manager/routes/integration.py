from flask import Blueprint

from VA.schedule_manager.integration_manifest import build_integration_manifest
from VA.schedule_manager.routes.api_responses import api_success


integration_bp = Blueprint("integration", __name__, url_prefix="/integration")


@integration_bp.get("/health")
def health():
    return api_success(
        {
            "status": "ok",
            "module": build_integration_manifest()["module"],
        }
    )


@integration_bp.get("/manifest")
def manifest():
    return api_success(build_integration_manifest())
