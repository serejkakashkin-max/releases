from __future__ import annotations

import hashlib
import os
import stat
from io import BytesIO
from pathlib import Path

from flask import (
    Blueprint, abort, current_app, make_response, redirect, render_template,
    request, send_file,
)

from config import DOC_TEMPLATES_ROOT
from services.cross_process_file_lock import FileLockTimeoutError
from services.document_template_auth_service import (
    RateLimitStorageError, UnsafeSessionConfiguration, clear_editor_session,
    csrf_is_valid, csrf_token, editor_session_actor, login_editor, login_url,
    safe_next, strong_session_secret,
)
from services.document_template_candidate_service import validate_staged_candidate
from services.document_template_publish_service import (
    DocumentConflict, DocumentMutationBlocked, publish_candidate, recover_stale_operations, rollback_version,
)
from services.document_template_runtime_service import RuntimeStateError
from services.document_template_read_service import (
    DocumentTemplateRootUnavailable, build_catalog_page, resolve_document,
)
from services.document_template_storage_service import (
    MAX_UPLOAD_BYTES, UPLOAD_REQUEST_OVERHEAD, CandidateNotFound,
    CandidateStateConflict, CandidateUploadTooLarge, cancel_candidate, claim_maintenance_window, cleanup_candidates,
    candidate_file, get_candidate, history_file, list_candidates, list_history,
    write_uploaded_candidate,
)
from services.document_template_vendor_service import verify_vendor_assets
from services.public_url_service import public_url_for


DOCX_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
MUTATION_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "connect-src 'self'; img-src 'self' data: blob:; font-src 'self' data:; "
    "object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'self'"
)

document_template_bp = Blueprint("document_templates", __name__, url_prefix="/admin/document-templates")


def _is_htmx_request() -> bool:
    return request.headers.get("HX-Request", "").lower() == "true"


def _require_enabled() -> None:
    if not current_app.config.get("DOCUMENT_TEMPLATE_CENTER_ENABLED", False):
        abort(404)


def _template_root() -> Path:
    return Path(current_app.config.get("DOCUMENT_TEMPLATE_CENTER_ROOT", DOC_TEMPLATES_ROOT))


def _navigation_links():
    return [
        {"label": "Главная", "url": public_url_for("main.index")},
        {"label": "Блок релизов", "url": public_url_for("dashboard.release_monitor_page")},
        {"label": "Дашборд", "url": public_url_for("dashboard.dashboard")},
        {"label": "МПР", "url": public_url_for("mpr.mpr_page")},
        {"label": "Помощь", "url": public_url_for("main.help_page")},
    ]


def _safe_configuration_response():
    if _is_htmx_request():
        response = make_response("", 503)
        response.headers["HX-Redirect"] = public_url_for("document_templates.login_page")
        return response
    return make_response(render_template(
        "document_templates/configuration_error.html",
        navigation_links=_navigation_links(), public_url_for=public_url_for,
    ), 503)


def _current_internal_get() -> str:
    value = request.path
    if request.query_string:
        value += "?" + request.query_string.decode("utf-8", "ignore")
    return safe_next(value)


def _auth_guard(*, login_allowed: bool = False):
    _require_enabled()
    try:
        strong_session_secret()
    except UnsafeSessionConfiguration:
        return _safe_configuration_response()
    if login_allowed:
        return None
    actor = editor_session_actor()
    if actor:
        if request.method in MUTATION_METHODS and not csrf_is_valid():
            return make_response("Действие отклонено: обновите страницу и повторите попытку.", 403)
        return None
    target = login_url(_current_internal_get() if request.method == "GET" else None)
    if _is_htmx_request():
        response = make_response("", 403)
        response.headers["HX-Redirect"] = target
        return response
    if request.method == "GET":
        return redirect(target, code=302)
    return make_response("Требуется вход в Центр шаблонов.", 403)


@document_template_bp.after_request
def _security_headers(response):
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Content-Security-Policy"] = CSP
    return response


@document_template_bp.errorhandler(RuntimeStateError)
def _runtime_state_error(_error):
    return make_response("Служебные данные Центра шаблонов временно недоступны.", 503)


@document_template_bp.get("/login")
def login_page():
    guard = _auth_guard(login_allowed=True)
    if guard is not None:
        return guard
    if editor_session_actor(touch=False):
        return redirect(safe_next(request.args.get("next")), code=302)
    return render_template(
        "document_templates/login.html", error="", next_url=safe_next(request.args.get("next")),
        navigation_links=_navigation_links(), public_url_for=public_url_for,
    )


@document_template_bp.post("/session/login")
def login_session():
    guard = _auth_guard(login_allowed=True)
    if guard is not None:
        return guard
    destination = safe_next(request.form.get("next"))
    try:
        ok, code = login_editor(request.form.get("display_name", ""), request.form.get("token", ""))
    except ValueError as exc:
        return render_template("document_templates/login.html", error=str(exc), next_url=destination, navigation_links=_navigation_links(), public_url_for=public_url_for), 400
    except RateLimitStorageError:
        return render_template("document_templates/login.html", error="Защита входа временно недоступна. Обратитесь к администратору.", next_url=destination, navigation_links=_navigation_links(), public_url_for=public_url_for), 503
    if not ok:
        if code in {"rate_limited", "rate_limit_capacity"}:
            status, message = 429, "Слишком много попыток. Подождите несколько минут."
        elif code == "editor_token_missing":
            status, message = 503, "Вход редактора пока не настроен."
        else:
            status, message = 403, "Имя или общий токен не приняты."
        return render_template("document_templates/login.html", error=message, next_url=destination, navigation_links=_navigation_links(), public_url_for=public_url_for), status
    if _is_htmx_request():
        response = make_response("", 204); response.headers["HX-Redirect"] = destination; return response
    return redirect(destination, code=303)


@document_template_bp.post("/session/logout")
def logout_session():
    guard = _auth_guard()
    if guard is not None:
        return guard
    clear_editor_session()
    return redirect(public_url_for("document_templates.login_page"), code=303)


def _page_number() -> int:
    try:
        return max(1, int(request.args.get("page", "1")))
    except (TypeError, ValueError):
        return 1


def _enrich_catalog(catalog: dict) -> None:
    for kit in catalog.get("kits", []):
        for document in kit.get("documents", []):
            candidates = list_candidates(document["document_id"])
            document["candidates"] = candidates[:3]
            document["candidate_count"] = len(candidates)
            document["status"] = candidates[0]["state"] if candidates else "active"


@document_template_bp.get("")
@document_template_bp.get("/")
def index():
    guard = _auth_guard()
    if guard is not None:
        return guard
    try:
        if claim_maintenance_window():
            cleanup_candidates()
            recover_stale_operations(document_resolver=lambda value: resolve_document(value, _template_root()))
    except (OSError, FileLockTimeoutError, RuntimeError, ValueError):
        pass
    vendor_status = verify_vendor_assets(Path(current_app.static_folder))
    if not vendor_status["ok"]:
        template = "document_templates/_catalog_error.html" if _is_htmx_request() else "document_templates/vendor_error.html"
        response = make_response(render_template(template, catalog_error="Локальные компоненты интерфейса недоступны: " + ", ".join(vendor_status["problems"]), problems=vendor_status["problems"], navigation_links=_navigation_links(), public_url_for=public_url_for), 503)
        response.headers.add("Vary", "HX-Request"); return response
    filters = {"query": request.args.get("q", "").strip(), "category": request.args.get("category", "").strip(), "ke": request.args.get("ke", "").strip(), "variant": request.args.get("variant", "").strip(), "page": _page_number()}
    try:
        catalog = build_catalog_page(_template_root(), **filters); _enrich_catalog(catalog)
        template = "document_templates/_catalog.html" if _is_htmx_request() else "document_templates/index.html"
        response = make_response(render_template(template, catalog=catalog, actor=editor_session_actor(touch=False), csrf_token=csrf_token(), navigation_links=_navigation_links(), public_url_for=public_url_for))
    except DocumentTemplateRootUnavailable:
        template = "document_templates/_catalog_error.html" if _is_htmx_request() else "document_templates/index.html"
        response = make_response(render_template(template, catalog=None, catalog_error="Каталог шаблонов временно недоступен.", actor=editor_session_actor(touch=False), csrf_token=csrf_token(), navigation_links=_navigation_links(), public_url_for=public_url_for), 503)
    response.headers.add("Vary", "HX-Request"); return response


def _resolved_or_404(document_id: str):
    document = resolve_document(document_id, _template_root())
    if document is None:
        abort(404)
    return document


def _docx_response(path: Path, filename: str, *, attachment: bool):
    try:
        payload = path.read_bytes()
    except OSError:
        abort(404)
    return send_file(BytesIO(payload), mimetype=DOCX_CONTENT_TYPE, as_attachment=attachment, download_name=filename, conditional=False, etag=False, max_age=0)


def _active_docx_response(document_id: str, *, attachment: bool):
    document = _resolved_or_404(document_id)
    try:
        with document.path.open("rb") as handle:
            opened = os.fstat(handle.fileno())
            if not stat.S_ISREG(opened.st_mode):
                abort(404)
            payload = handle.read()
    except OSError:
        abort(404)
    payload_sha = hashlib.sha256(payload).hexdigest()
    confirmed = resolve_document(document_id, _template_root())
    if confirmed is None or confirmed.sha256 != payload_sha or confirmed.filename != document.filename:
        abort(404)
    return send_file(BytesIO(payload), mimetype=DOCX_CONTENT_TYPE, as_attachment=attachment, download_name=document.filename, conditional=False, etag=False, max_age=0)


@document_template_bp.get("/documents/<document_id>/preview")
def preview_document(document_id: str):
    guard = _auth_guard()
    if guard is not None: return guard
    return _active_docx_response(document_id, attachment=False)


@document_template_bp.get("/documents/<document_id>/download")
def download_document(document_id: str):
    guard = _auth_guard()
    if guard is not None: return guard
    return _active_docx_response(document_id, attachment=True)


@document_template_bp.post("/documents/<document_id>/candidates")
def upload_candidate(document_id: str):
    guard = _auth_guard()
    if guard is not None: return guard
    document = _resolved_or_404(document_id)
    if request.content_length is not None and request.content_length > MAX_UPLOAD_BYTES + UPLOAD_REQUEST_OVERHEAD:
        return make_response("Файл превышает допустимый размер 10 МиБ.", 413)
    upload = request.files.get("file")
    comment = str(request.form.get("comment") or "").strip()
    upload_name = Path(upload.filename).name if upload is not None and upload.filename else ""
    if upload is None or not upload_name or len(upload_name) > 255 or any(ord(char) < 32 for char in upload_name) or not upload_name.casefold().endswith(".docx") or not 3 <= len(comment) <= 500 or any(ord(char) < 32 and char not in "\r\n\t" for char in comment):
        return make_response("Выберите DOCX и добавьте комментарий от 3 до 500 символов.", 400)
    if upload.content_length is not None and upload.content_length > MAX_UPLOAD_BYTES:
        return make_response("Файл превышает допустимый размер 10 МиБ.", 413)
    try:
        metadata = write_uploaded_candidate(upload.stream, document_id=document_id, source_filename=upload_name, active_filename=document.filename, active_sha=document.sha256, uploaded_by=editor_session_actor(touch=False) or "", comment=comment)
    except CandidateUploadTooLarge:
        return make_response("Файл превышает допустимый размер 10 МиБ.", 413)
    except ValueError as exc:
        return make_response(str(exc), 400)
    target = public_url_for("document_templates.candidate_detail", document_id=document_id, candidate_uuid=metadata["candidate_uuid"])
    if _is_htmx_request():
        response = make_response("", 201); response.headers["HX-Redirect"] = target; return response
    return redirect(target, code=303)


def _candidate_context(document_id: str, candidate_uuid: str):
    document = _resolved_or_404(document_id)
    try: candidate = get_candidate(candidate_uuid, document_id=document_id)
    except ValueError: abort(400)
    except CandidateNotFound: abort(404)
    return document, candidate


@document_template_bp.get("/documents/<document_id>/candidates/<candidate_uuid>")
def candidate_detail(document_id: str, candidate_uuid: str):
    guard = _auth_guard()
    if guard is not None: return guard
    document, candidate = _candidate_context(document_id, candidate_uuid)
    template = "document_templates/_candidate_panel.html" if _is_htmx_request() else "document_templates/candidate.html"
    return render_template(template, document=document.as_view_model(), candidate=candidate, actor=editor_session_actor(touch=False), csrf_token=csrf_token(), navigation_links=_navigation_links(), public_url_for=public_url_for)


@document_template_bp.post("/documents/<document_id>/candidates/<candidate_uuid>/validate")
def validate_candidate_route(document_id: str, candidate_uuid: str):
    guard = _auth_guard()
    if guard is not None: return guard
    document, _ = _candidate_context(document_id, candidate_uuid)
    try: candidate = validate_staged_candidate(document_id, candidate_uuid, document.path)
    except ValueError: abort(400)
    except CandidateNotFound: abort(404)
    except CandidateStateConflict: return make_response("Кандидат находится в несовместимом состоянии.", 409)
    status = 200 if candidate["state"] == "valid" else 422
    return render_template("document_templates/_candidate_panel.html", document=document.as_view_model(), candidate=candidate, actor=editor_session_actor(touch=False), csrf_token=csrf_token(), navigation_links=_navigation_links(), public_url_for=public_url_for), status


def _candidate_doc_response(document_id: str, candidate_uuid: str, name: str, attachment: bool):
    document, candidate = _candidate_context(document_id, candidate_uuid)
    try: path = candidate_file(candidate_uuid, name)
    except CandidateNotFound: abort(404)
    filename = candidate["source_filename"] if name == "candidate.docx" else f"test-{candidate['source_filename']}"
    return _docx_response(path, filename, attachment=attachment)


@document_template_bp.get("/documents/<document_id>/candidates/<candidate_uuid>/preview")
def preview_candidate(document_id, candidate_uuid):
    guard = _auth_guard();
    if guard is not None: return guard
    return _candidate_doc_response(document_id, candidate_uuid, "candidate.docx", False)


@document_template_bp.get("/documents/<document_id>/candidates/<candidate_uuid>/test-document/preview")
def preview_test_document(document_id, candidate_uuid):
    guard = _auth_guard();
    if guard is not None: return guard
    return _candidate_doc_response(document_id, candidate_uuid, "test.docx", False)


@document_template_bp.get("/documents/<document_id>/candidates/<candidate_uuid>/test-document/download")
def download_test_document(document_id, candidate_uuid):
    guard = _auth_guard();
    if guard is not None: return guard
    return _candidate_doc_response(document_id, candidate_uuid, "test.docx", True)


@document_template_bp.post("/documents/<document_id>/candidates/<candidate_uuid>/publish")
def publish_candidate_route(document_id, candidate_uuid):
    guard = _auth_guard();
    if guard is not None: return guard
    document, _ = _candidate_context(document_id, candidate_uuid)
    try: publish_candidate(document, candidate_uuid, editor_session_actor(touch=False) or "")
    except DocumentConflict: return make_response("Действующий шаблон изменился. Проверьте конфликт и загрузите новую версию.", 409)
    except CandidateStateConflict: return make_response("Кандидат не готов к публикации.", 422)
    except (DocumentMutationBlocked, FileLockTimeoutError, OSError): return make_response("Документ временно заблокирован безопасным восстановлением.", 423)
    return redirect(public_url_for("document_templates.index"), code=303)


@document_template_bp.post("/documents/<document_id>/candidates/<candidate_uuid>/cancel")
def cancel_candidate_route(document_id, candidate_uuid):
    guard = _auth_guard();
    if guard is not None: return guard
    _resolved_or_404(document_id)
    try: cancel_candidate(candidate_uuid, document_id)
    except ValueError: abort(400)
    except CandidateNotFound: abort(404)
    except CandidateStateConflict: return make_response("Кандидат уже нельзя отменить.", 409)
    return redirect(public_url_for("document_templates.index"), code=303)


@document_template_bp.get("/documents/<document_id>/history")
def document_history(document_id):
    guard = _auth_guard();
    if guard is not None: return guard
    document = _resolved_or_404(document_id)
    return render_template("document_templates/history.html", document=document.as_view_model(), versions=list_history(document_id), csrf_token=csrf_token(), actor=editor_session_actor(touch=False), navigation_links=_navigation_links(), public_url_for=public_url_for)


def _history_response(document_id, version_uuid, attachment):
    document = _resolved_or_404(document_id)
    try: path = history_file(document_id, version_uuid)
    except ValueError: abort(400)
    except CandidateNotFound: abort(404)
    return _docx_response(path, document.filename, attachment=attachment)


@document_template_bp.get("/documents/<document_id>/history/<version_uuid>/preview")
def preview_history(document_id, version_uuid):
    guard = _auth_guard();
    if guard is not None: return guard
    return _history_response(document_id, version_uuid, False)


@document_template_bp.get("/documents/<document_id>/history/<version_uuid>/download")
def download_history(document_id, version_uuid):
    guard = _auth_guard();
    if guard is not None: return guard
    return _history_response(document_id, version_uuid, True)


@document_template_bp.post("/documents/<document_id>/history/<version_uuid>/rollback")
def rollback_history(document_id, version_uuid):
    guard = _auth_guard();
    if guard is not None: return guard
    document = _resolved_or_404(document_id)
    reason = str(request.form.get("reason") or "").strip(); expected = str(request.form.get("expected_active_sha") or "")
    if not 3 <= len(reason) <= 500 or len(expected) != 64: return make_response("Укажите причину отката и актуальную SHA.", 400)
    try: rollback_version(document, version_uuid, actor=editor_session_actor(touch=False) or "", reason=reason, expected_active_sha=expected)
    except ValueError: abort(400)
    except CandidateNotFound: abort(404)
    except DocumentConflict: return make_response("Действующий шаблон изменился. Обновите историю.", 409)
    except (DocumentMutationBlocked, FileLockTimeoutError, OSError): return make_response("Откат временно заблокирован безопасным восстановлением.", 423)
    return redirect(public_url_for("document_templates.document_history", document_id=document_id), code=303)
