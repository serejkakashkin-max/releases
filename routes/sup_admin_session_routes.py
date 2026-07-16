from flask import Blueprint, jsonify, request

from services.sup_admin_auth_service import (
    get_sup_admin_csrf_token,
    is_admin_session_secret_configured,
    is_sup_admin_authenticated,
    login_sup_admin,
    logout_sup_admin,
)


sup_admin_session_bp = Blueprint("sup_admin_session", __name__, url_prefix="/admin/session")


@sup_admin_session_bp.post("/login")
def login():
    payload = request.get_json(silent=True) or {}
    data, status = login_sup_admin(str(payload.get("token") or ""))
    return jsonify(data), status


@sup_admin_session_bp.post("/logout")
def logout():
    data, status = logout_sup_admin()
    return jsonify(data), status


@sup_admin_session_bp.get("/status")
def status():
    authenticated = is_sup_admin_authenticated()
    return jsonify(
        {
            "success": True,
            "authenticated": authenticated,
            "session_secret_configured": is_admin_session_secret_configured(),
            "csrf_token": get_sup_admin_csrf_token() if authenticated else "",
        }
    )
