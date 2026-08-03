from __future__ import annotations

from io import BytesIO
from pathlib import Path

from flask import Blueprint, abort, current_app, make_response, render_template, request, send_file

from config import DOC_TEMPLATES_ROOT
from services.document_template_read_service import (
    DocumentTemplateRootUnavailable,
    build_catalog_page,
    resolve_document,
)
from services.document_template_vendor_service import verify_vendor_assets
from services.public_url_service import public_url_for


DOCX_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

document_template_bp = Blueprint(
    "document_templates",
    __name__,
    url_prefix="/admin/document-templates",
)


def _require_enabled() -> None:
    if not current_app.config.get("DOCUMENT_TEMPLATE_CENTER_ENABLED", False):
        abort(404)


def _template_root() -> Path:
    # The override is intentionally an application-config seam for isolated tests only.
    return Path(current_app.config.get("DOCUMENT_TEMPLATE_CENTER_ROOT", DOC_TEMPLATES_ROOT))


def _page_number() -> int:
    try:
        return max(1, int(request.args.get("page", "1")))
    except (TypeError, ValueError):
        return 1


def _navigation_links():
    return [
        {"label": "Главная", "url": public_url_for("main.index")},
        {"label": "Блок релизов", "url": public_url_for("dashboard.release_monitor_page")},
        {"label": "Дашборд", "url": public_url_for("dashboard.dashboard")},
        {"label": "МПР", "url": public_url_for("mpr.mpr_page")},
        {"label": "Помощь", "url": public_url_for("main.help_page")},
    ]


def _vendor_status():
    return verify_vendor_assets(Path(current_app.static_folder))


def _is_htmx_request() -> bool:
    return request.headers.get("HX-Request", "").lower() == "true"


@document_template_bp.get("")
@document_template_bp.get("/")
def index():
    _require_enabled()
    vendor_status = _vendor_status()
    if not vendor_status["ok"]:
        if _is_htmx_request():
            response = make_response(render_template(
                "document_templates/_catalog_error.html",
                catalog_error="Отсутствуют или повреждены локальные vendor assets: "
                + ", ".join(vendor_status["problems"]),
            ), 503)
        else:
            response = make_response(render_template(
                "document_templates/vendor_error.html",
                problems=vendor_status["problems"],
                navigation_links=_navigation_links(),
                public_url_for=public_url_for,
            ), 503)
        response.headers.add("Vary", "HX-Request")
        return response

    filters = {
        "query": request.args.get("q", "").strip(),
        "category": request.args.get("category", "").strip(),
        "ke": request.args.get("ke", "").strip(),
        "variant": request.args.get("variant", "").strip(),
        "page": _page_number(),
    }
    try:
        catalog = build_catalog_page(_template_root(), **filters)
        template_name = (
            "document_templates/_catalog.html"
            if _is_htmx_request()
            else "document_templates/index.html"
        )
        response = make_response(render_template(
            template_name,
            catalog=catalog,
            navigation_links=_navigation_links(),
            public_url_for=public_url_for,
        ))
    except DocumentTemplateRootUnavailable:
        template_name = (
            "document_templates/_catalog_error.html"
            if _is_htmx_request()
            else "document_templates/index.html"
        )
        response = make_response(render_template(
            template_name,
            catalog=None,
            catalog_error="Каталог шаблонов временно недоступен.",
            navigation_links=_navigation_links(),
            public_url_for=public_url_for,
        ), 503)
    response.headers.add("Vary", "HX-Request")
    return response


def _document_response(document_id: str, *, as_attachment: bool):
    _require_enabled()
    document = resolve_document(document_id, _template_root())
    if document is None:
        abort(404)
    try:
        payload = document.path.read_bytes()
    except OSError:
        abort(404)
    response = send_file(
        BytesIO(payload),
        mimetype=DOCX_CONTENT_TYPE,
        as_attachment=as_attachment,
        download_name=document.filename,
        conditional=False,
        etag=False,
        max_age=0,
    )
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@document_template_bp.get("/documents/<document_id>/preview")
def preview_document(document_id: str):
    return _document_response(document_id, as_attachment=False)


@document_template_bp.get("/documents/<document_id>/download")
def download_document(document_id: str):
    return _document_response(document_id, as_attachment=True)
