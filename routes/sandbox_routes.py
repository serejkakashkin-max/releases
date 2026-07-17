from flask import Blueprint, render_template

from services.public_url_service import public_url_for
from services.sandbox_registry import get_sandbox_modules


sandbox_bp = Blueprint("sandbox", __name__)


@sandbox_bp.route("/sandbox")
def sandbox_index():
    modules = get_sandbox_modules(loaded_only=True)
    return render_template(
        "sandbox.html",
        modules=modules,
        public_url_for=public_url_for,
    )
