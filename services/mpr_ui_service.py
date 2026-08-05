from __future__ import annotations

from services.public_url_service import public_url_for


def build_mpr_ui_config(*, templates) -> dict:
    """Build presentation-only, prefix-safe configuration for the MPR page."""

    available_templates = list(templates or [])
    initial_template_code = ""
    if available_templates:
        initial_template_code = str(available_templates[0].get("code") or "")

    return {
        "urls": {
            "preview": public_url_for("mpr.mpr_preview"),
            "generate": public_url_for("mpr.mpr_generate"),
        },
        "initial_template_code": initial_template_code,
    }
