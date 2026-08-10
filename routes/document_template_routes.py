from __future__ import annotations

import re
import shutil
from io import BytesIO
from pathlib import Path

from flask import (
    Blueprint, abort, current_app, make_response, redirect, render_template,
    jsonify, request, send_file,
)

from config import DOC_TEMPLATES_ROOT
from services.cross_process_file_lock import FileLockTimeoutError
from services.document_template_csrf_service import (
    DOCUMENT_TEMPLATE_ACTOR, apply_csrf_cookie, csrf_form_is_valid, csrf_is_valid, csrf_token,
)
from services.document_template_audit_service import append_audit_event
from services.document_template_candidate_service import (
    CandidatePreviewDenied, candidate_preview_payload, validate_staged_candidate,
)
from services.document_template_publish_service import (
    DocumentConflict, DocumentMutationBlocked, publish_candidate, recover_stale_operations, rollback_version,
)
from services.document_template_runtime_service import RuntimeStateError
from services.document_template_read_service import (
    DocumentTemplateRootUnavailable, build_catalog_page, read_document_payload, resolve_document,
)
from services.document_template_storage_service import (
    MAX_UPLOAD_BYTES, UPLOAD_REQUEST_OVERHEAD, CandidateNotFound,
    CandidateStateConflict, CandidateUploadTooLarge, cancel_candidate, claim_maintenance_window, cleanup_candidates,
    HistoryPreviewDenied, candidate_directory, committed_history_payload, delete_history_version,
    document_lock, get_candidate, get_history_version, list_candidates, list_history,
    update_candidate, write_uploaded_candidate,
)
from services.document_template_vendor_service import verify_vendor_assets
from services.oplot_ui_service import safe_public_url_for
from services.public_url_service import public_url_for
from services.sup_admin_auth_service import (
    csrf_protect_request, is_admin_session_secret_configured, is_sup_admin_authenticated,
)


DOCX_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "connect-src 'self'; img-src 'self' data: blob:; font-src 'self' data:; "
    "object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'self'"
)

document_template_bp = Blueprint(
    "document_templates",
    __name__,
    url_prefix="/dashboard/release-monitor/document-templates",
)


def _is_htmx_request() -> bool:
    return request.headers.get("HX-Request", "").lower() == "true"


def _template_root() -> Path:
    return Path(current_app.config.get("DOCUMENT_TEMPLATE_CENTER_ROOT", DOC_TEMPLATES_ROOT))


def _safe_configuration_response():
    if _is_htmx_request():
        return make_response(
            render_template(
                "document_templates/_catalog_error.html",
                catalog_error="Центр шаблонов временно недоступен.",
            ),
            503,
        )
    return make_response(render_template(
        "document_templates/configuration_error.html",
    ), 503)


def _csrf_guard():
    if csrf_is_valid():
        return None
    return make_response(
        "Действие отклонено: обновите страницу и повторите попытку.", 403
    )


@document_template_bp.after_request
def _security_headers(response):
    apply_csrf_cookie(response)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Content-Security-Policy"] = CSP
    return response


@document_template_bp.errorhandler(RuntimeStateError)
def _runtime_state_error(_error):
    if request.method == "GET":
        return _safe_configuration_response()
    return make_response("Служебные данные Центра шаблонов временно недоступны.", 503)


def _page_number() -> int:
    try:
        return max(1, int(request.args.get("page", "1")))
    except (TypeError, ValueError):
        return 1


def _enrich_catalog(catalog: dict) -> None:
    for kit in catalog.get("kits", []):
        for document in kit.get("documents", []):
            candidates = [
                item for item in list_candidates(document["document_id"])
                if item.get("state") not in {"published", "cancelled", "expired"}
            ]
            document["candidate_count"] = len(candidates)
            document["status"] = candidates[0]["state"] if candidates else "active"


def _enrich_history_versions(document_id: str, versions: list[dict]) -> None:
    for version in versions:
        if version.get("replacement_source_filename") or not version.get("candidate_uuid"):
            continue
        try:
            candidate = get_candidate(
                version["candidate_uuid"],
                document_id=document_id,
                allow_expired=True,
            )
        except (CandidateNotFound, ValueError, OSError):
            continue
        version["replacement_source_filename"] = candidate.get("source_filename", "")


def _navigation_response(target: str, *, htmx_status: int = 200):
    if _is_htmx_request():
        response = make_response("", htmx_status)
        response.headers["HX-Redirect"] = target
        return response
    return redirect(target, code=303)


def _candidate_result_url(document_id: str, candidate_uuid: str) -> str:
    return public_url_for(
        "document_templates.candidate_detail",
        document_id=document_id,
        candidate_uuid=candidate_uuid,
    )


def _complete_replacement(document, candidate_uuid: str):
    """Run the existing validation and atomic publish pipeline as one action."""
    candidate = validate_staged_candidate(
        document.document_id,
        candidate_uuid,
        document.path,
    )
    if candidate.get("state") != "valid":
        return candidate, False
    publish_candidate(document, candidate_uuid, DOCUMENT_TEMPLATE_ACTOR)
    return get_candidate(candidate_uuid, document_id=document.document_id), True


@document_template_bp.get("")
@document_template_bp.get("/")
def index():
    try:
        if claim_maintenance_window():
            cleanup_candidates()
            recover_stale_operations(document_resolver=lambda value: resolve_document(value, _template_root()))
    except (OSError, FileLockTimeoutError, RuntimeError, ValueError):
        pass
    vendor_status = verify_vendor_assets(Path(current_app.static_folder))
    if not vendor_status["ok"]:
        template = "document_templates/_catalog_error.html" if _is_htmx_request() else "document_templates/vendor_error.html"
        response = make_response(render_template(template, catalog_error="Локальные компоненты интерфейса недоступны: " + ", ".join(vendor_status["problems"]), problems=vendor_status["problems"]), 503)
        response.headers.add("Vary", "HX-Request"); return response
    filters = {"query": request.args.get("q", "").strip(), "category": request.args.get("category", "").strip(), "variant": request.args.get("variant", "").strip(), "page": _page_number()}
    try:
        catalog = build_catalog_page(_template_root(), **filters); _enrich_catalog(catalog)
        catalog["replacement_success"] = request.args.get("replacement") == "success"
        template = "document_templates/_catalog.html" if _is_htmx_request() else "document_templates/index.html"
        response = make_response(render_template(template, catalog=catalog, csrf_token=csrf_token()))
    except DocumentTemplateRootUnavailable:
        if _is_htmx_request():
            response = make_response(render_template(
                "document_templates/_catalog_error.html",
                catalog_error="Каталог шаблонов временно недоступен.",
            ), 503)
        else:
            response = _safe_configuration_response()
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
    resolved = read_document_payload(document_id, _template_root())
    if resolved is None:
        abort(404)
    document, payload = resolved
    return send_file(BytesIO(payload), mimetype=DOCX_CONTENT_TYPE, as_attachment=attachment, download_name=document.filename, conditional=False, etag=False, max_age=0)


@document_template_bp.get("/documents/<document_id>/preview")
def preview_document(document_id: str):
    return _active_docx_response(document_id, attachment=False)


@document_template_bp.get("/documents/<document_id>/download")
def download_document(document_id: str):
    return _active_docx_response(document_id, attachment=True)


@document_template_bp.post("/documents/<document_id>/candidates")
def upload_candidate(document_id: str):
    guard = _csrf_guard()
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
        metadata = write_uploaded_candidate(upload.stream, document_id=document_id, source_filename=upload_name, active_filename=document.filename, active_sha=document.sha256, uploaded_by=DOCUMENT_TEMPLATE_ACTOR, comment=comment)
    except CandidateUploadTooLarge:
        return make_response("Файл превышает допустимый размер 10 МиБ.", 413)
    except ValueError as exc:
        return make_response(str(exc), 400)
    try:
        append_audit_event(actor=metadata.get("uploaded_by"), action="upload", document_id=document_id, relative_target=document.relative_path, candidate_uuid=metadata["candidate_uuid"], comment=comment, old_sha=document.sha256, new_sha=metadata.get("candidate_sha"), result="uploaded")
    except Exception:
        shutil.rmtree(candidate_directory(metadata["candidate_uuid"]), ignore_errors=True)
        return make_response("Журнал операций временно недоступен. Загрузка не сохранена.", 503)
    candidate_uuid = metadata["candidate_uuid"]
    try:
        _candidate, published = _complete_replacement(document, candidate_uuid)
    except ValueError:
        abort(400)
    except CandidateNotFound:
        abort(404)
    except (DocumentConflict, CandidateStateConflict):
        published = False
    except (DocumentMutationBlocked, FileLockTimeoutError, OSError):
        return make_response(
            "Безопасная замена временно заблокирована. Новая версия сохранена для диагностики.",
            423,
        )
    target = (
        public_url_for("document_templates.index", replacement="success")
        if published
        else _candidate_result_url(document_id, candidate_uuid)
    )
    return _navigation_response(target, htmx_status=201)


def _candidate_context(document_id: str, candidate_uuid: str):
    document = _resolved_or_404(document_id)
    try: candidate = get_candidate(candidate_uuid, document_id=document_id)
    except ValueError: abort(400)
    except CandidateNotFound: abort(404)
    return document, candidate


@document_template_bp.get("/documents/<document_id>/candidates/<candidate_uuid>")
def candidate_detail(document_id: str, candidate_uuid: str):
    document, candidate = _candidate_context(document_id, candidate_uuid)
    template = "document_templates/_candidate_panel.html" if _is_htmx_request() else "document_templates/candidate.html"
    return render_template(template, document=document.as_view_model(), candidate=candidate, csrf_token=csrf_token())


@document_template_bp.post("/documents/<document_id>/candidates/<candidate_uuid>/validate")
def validate_candidate_route(document_id: str, candidate_uuid: str):
    guard = _csrf_guard()
    if guard is not None: return guard
    document, _ = _candidate_context(document_id, candidate_uuid)
    try: candidate, published = _complete_replacement(document, candidate_uuid)
    except ValueError: abort(400)
    except CandidateNotFound: abort(404)
    except DocumentConflict:
        return _navigation_response(_candidate_result_url(document_id, candidate_uuid))
    except CandidateStateConflict: return make_response("Новая версия находится в несовместимом состоянии.", 409)
    except (DocumentMutationBlocked, FileLockTimeoutError, OSError):
        return make_response("Безопасная замена временно заблокирована.", 423)
    if published:
        return _navigation_response(
            public_url_for("document_templates.index", replacement="success")
        )
    status = 200 if candidate["state"] == "valid" else 422
    return render_template("document_templates/_candidate_panel.html", document=document.as_view_model(), candidate=candidate, csrf_token=csrf_token()), status


def _candidate_doc_response(document_id: str, candidate_uuid: str, name: str, attachment: bool):
    document = _resolved_or_404(document_id)
    try: payload, candidate = candidate_preview_payload(document_id, candidate_uuid, test_document=name == "test.docx")
    except ValueError: abort(400)
    except CandidateNotFound: abort(404)
    except CandidatePreviewDenied: return make_response("Документ ещё не прошёл необходимую безопасную проверку.", 409)
    filename = candidate["source_filename"] if name == "candidate.docx" else f"test-{candidate['source_filename']}"
    return send_file(BytesIO(payload), mimetype=DOCX_CONTENT_TYPE, as_attachment=attachment, download_name=filename, conditional=False, etag=False, max_age=0)


@document_template_bp.get("/documents/<document_id>/candidates/<candidate_uuid>/preview")
def preview_candidate(document_id, candidate_uuid):
    return _candidate_doc_response(document_id, candidate_uuid, "candidate.docx", False)


@document_template_bp.get("/documents/<document_id>/candidates/<candidate_uuid>/test-document/preview")
def preview_test_document(document_id, candidate_uuid):
    return _candidate_doc_response(document_id, candidate_uuid, "test.docx", False)


@document_template_bp.get("/documents/<document_id>/candidates/<candidate_uuid>/test-document/download")
def download_test_document(document_id, candidate_uuid):
    return _candidate_doc_response(document_id, candidate_uuid, "test.docx", True)


@document_template_bp.post("/documents/<document_id>/candidates/<candidate_uuid>/publish")
def publish_candidate_route(document_id, candidate_uuid):
    guard = _csrf_guard()
    if guard is not None: return guard
    document, _ = _candidate_context(document_id, candidate_uuid)
    try: publish_candidate(document, candidate_uuid, DOCUMENT_TEMPLATE_ACTOR)
    except DocumentConflict: return make_response("Действующий шаблон изменился. Проверьте конфликт и загрузите новую версию.", 409)
    except CandidateStateConflict: return make_response("Кандидат не готов к публикации.", 422)
    except (DocumentMutationBlocked, FileLockTimeoutError, OSError): return make_response("Документ временно заблокирован безопасным восстановлением.", 423)
    return redirect(public_url_for("document_templates.index"), code=303)


@document_template_bp.post("/documents/<document_id>/candidates/<candidate_uuid>/cancel")
def cancel_candidate_route(document_id, candidate_uuid):
    guard = _csrf_guard()
    if guard is not None: return guard
    document, candidate = _candidate_context(document_id, candidate_uuid)
    try: cancel_candidate(candidate_uuid, document_id)
    except ValueError: abort(400)
    except CandidateNotFound: abort(404)
    except CandidateStateConflict: return make_response("Кандидат уже нельзя отменить.", 409)
    event = dict(actor=DOCUMENT_TEMPLATE_ACTOR, action="cancel", document_id=document_id, relative_target=document.relative_path, candidate_uuid=candidate_uuid, comment=candidate.get("comment"), old_sha=candidate.get("active_sha_at_upload"), new_sha=candidate.get("candidate_sha"), result="cancelled")
    try:
        append_audit_event(**event)
    except Exception:
        update_candidate(candidate_uuid, {"audit_pending": event}, document_id=document_id)
    return redirect(public_url_for("document_templates.index"), code=303)


@document_template_bp.get("/documents/<document_id>/history")
def document_history(document_id):
    document = _resolved_or_404(document_id)
    versions = list_history(document_id)
    _enrich_history_versions(document_id, versions)
    return render_template(
        "document_templates/history.html",
        document=document.as_view_model(),
        versions=versions,
        csrf_token=csrf_token(),
        admin_session_configured=is_admin_session_secret_configured(),
        admin_session_login_url=safe_public_url_for("sup_admin_session.login") or "",
        admin_session_status_url=safe_public_url_for("sup_admin_session.status") or "",
    )


def _history_response(document_id, version_uuid, attachment):
    document = _resolved_or_404(document_id)
    try: payload, _ = committed_history_payload(document_id, version_uuid)
    except ValueError: abort(400)
    except CandidateNotFound: abort(404)
    except HistoryPreviewDenied: return make_response("Историческая версия недоступна или не прошла безопасную проверку.", 409)
    return send_file(BytesIO(payload), mimetype=DOCX_CONTENT_TYPE, as_attachment=attachment, download_name=document.filename, conditional=False, etag=False, max_age=0)


@document_template_bp.get("/documents/<document_id>/history/<version_uuid>/preview")
def preview_history(document_id, version_uuid):
    return _history_response(document_id, version_uuid, False)


@document_template_bp.get("/documents/<document_id>/history/<version_uuid>/download")
def download_history(document_id, version_uuid):
    return _history_response(document_id, version_uuid, True)


@document_template_bp.post("/documents/<document_id>/history/<version_uuid>/delete")
def delete_history(document_id, version_uuid):
    if not csrf_form_is_valid():
        return make_response(
            "Действие отклонено: обновите страницу и повторите попытку.", 403
        )
    if not is_sup_admin_authenticated():
        return jsonify({
            "success": False,
            "requires_admin_login": True,
            "error": "Требуется административный вход.",
        }), 403
    csrf_error = csrf_protect_request()
    if csrf_error is not None:
        return csrf_error
    document = _resolved_or_404(document_id)
    try:
        with document_lock(document.document_id):
            version = get_history_version(document.document_id, version_uuid)
            append_audit_event(
                actor=DOCUMENT_TEMPLATE_ACTOR,
                action="history_delete",
                document_id=document.document_id,
                relative_target=document.relative_path,
                version_uuid=version_uuid,
                sha256=version.get("sha256"),
                source_filename=version.get("source_filename") or document.filename,
                result="deleted",
            )
            delete_history_version(document.document_id, version_uuid)
    except ValueError:
        abort(400)
    except CandidateNotFound:
        abort(404)
    except HistoryPreviewDenied:
        return make_response("Историческую версию нельзя удалить безопасно.", 423)
    except (FileLockTimeoutError, OSError):
        return make_response("Историческая версия временно заблокирована безопасным восстановлением.", 423)
    return jsonify({
        "success": True,
        "redirect": public_url_for("document_templates.document_history", document_id=document_id),
    })


@document_template_bp.post("/documents/<document_id>/history/<version_uuid>/rollback")
def rollback_history(document_id, version_uuid):
    guard = _csrf_guard()
    if guard is not None: return guard
    document = _resolved_or_404(document_id)
    reason = str(request.form.get("reason") or "").strip(); expected = str(request.form.get("expected_active_sha") or "")
    if not 3 <= len(reason) <= 500 or re.fullmatch(r"[0-9a-f]{64}", expected) is None: return make_response("Укажите причину отката и актуальную SHA.", 400)
    try: rollback_version(document, version_uuid, actor=DOCUMENT_TEMPLATE_ACTOR, reason=reason, expected_active_sha=expected)
    except ValueError: abort(400)
    except CandidateNotFound: abort(404)
    except DocumentConflict: return make_response("Действующий шаблон изменился. Обновите историю.", 409)
    except (DocumentMutationBlocked, FileLockTimeoutError, OSError): return make_response("Откат временно заблокирован безопасным восстановлением.", 423)
    return redirect(public_url_for("document_templates.document_history", document_id=document_id), code=303)
