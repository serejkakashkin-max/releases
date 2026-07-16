from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import threading
import time
from datetime import timedelta
from typing import Any, Dict, Optional

from flask import jsonify, request, session

from config import TOKENS


ADMIN_SESSION_LIFETIME_SECONDS = 8 * 60 * 60
ADMIN_FAILURE_WINDOW_SECONDS = 10 * 60
ADMIN_FAILURE_LIMIT = 5
ADMIN_BLOCK_SECONDS = 5 * 60
ADMIN_MUTATION_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

_rate_limit_lock = threading.Lock()
_rate_limit_state: Dict[str, Dict[str, Any]] = {}


def configured_sup_admin_token() -> str:
    return str(TOKENS.get("sup_admin_token") or "").strip()


def configured_sup_admin_session_secret() -> str:
    return (
        str(os.environ.get("SUP_ADMIN_SESSION_SECRET") or "").strip()
        or str(TOKENS.get("sup_admin_session_secret") or "").strip()
    )


def is_admin_session_secret_configured() -> bool:
    secret = configured_sup_admin_session_secret()
    return len(secret) >= 32


def configure_sup_admin_session(app) -> None:
    secret = configured_sup_admin_session_secret()
    if is_admin_session_secret_configured():
        app.secret_key = secret
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = str(
        TOKENS.get("session_cookie_samesite") or "Lax"
    )
    app.config["SESSION_COOKIE_SECURE"] = _coerce_bool(
        os.environ.get("SESSION_COOKIE_SECURE", TOKENS.get("session_cookie_secure")),
        default=False,
    )
    app.permanent_session_lifetime = timedelta(seconds=ADMIN_SESSION_LIFETIME_SECONDS)


def is_sup_admin_authenticated() -> bool:
    secret = configured_sup_admin_session_secret()
    if not is_admin_session_secret_configured():
        return False
    if not session.get("sup_admin_authenticated"):
        return False
    if not session.get("sup_admin_login_at"):
        return False
    return hmac.compare_digest(
        str(session.get("sup_admin_secret_fingerprint") or ""),
        _secret_fingerprint(secret),
    )


def get_sup_admin_csrf_token() -> str:
    if not is_sup_admin_authenticated():
        return ""
    return str(session.get("sup_admin_csrf_token") or "")


def login_sup_admin(token: str) -> tuple[dict, int]:
    if not configured_sup_admin_token():
        return _auth_error_payload(), 403
    if not is_admin_session_secret_configured():
        return {
            "success": False,
            "error": "Административная session не настроена. Задайте SUP_ADMIN_SESSION_SECRET или sup_admin_session_secret.",
        }, 403

    client_key = _client_key()
    if _is_blocked(client_key):
        return _auth_error_payload(), 403

    expected = configured_sup_admin_token()
    supplied = str(token or "").strip()
    if not supplied or not hmac.compare_digest(supplied, expected):
        _record_failed_attempt(client_key)
        return _auth_error_payload(), 403

    _clear_rate_limit(client_key)
    session.clear()
    session.permanent = True
    secret = configured_sup_admin_session_secret()
    session["sup_admin_authenticated"] = True
    session["sup_admin_login_at"] = int(time.time())
    session["sup_admin_secret_fingerprint"] = _secret_fingerprint(secret)
    session["sup_admin_csrf_token"] = secrets.token_urlsafe(32)
    return {
        "success": True,
        "csrf_token": session["sup_admin_csrf_token"],
        "expires_in_seconds": ADMIN_SESSION_LIFETIME_SECONDS,
    }, 200


def logout_sup_admin() -> tuple[dict, int]:
    if not is_sup_admin_authenticated():
        return {"success": False, "error": "Административная session не активна."}, 403
    csrf_response = csrf_protect_request()
    if csrf_response is not None:
        response, status = csrf_response
        return response.get_json(silent=True) or {"success": False}, status
    session.clear()
    return {"success": True}, 200


def require_sup_admin_request():
    if is_sup_admin_authenticated():
        return None
    if _wants_json_response():
        return jsonify(
            {
                "success": False,
                "error": "Требуется административный вход.",
            }
        ), 403
    return (
        "<!doctype html><meta charset='utf-8'>"
        "<title>Доступ закрыт</title>"
        "<body style='font-family: sans-serif; padding: 32px;'>"
        "<h1>Доступ закрыт</h1>"
        "<p>Для открытия инструмента требуется административный вход через СУП-параметры.</p>"
        "</body>",
        403,
    )


def csrf_protect_request():
    if request.method not in ADMIN_MUTATION_METHODS:
        return None
    expected = str(session.get("sup_admin_csrf_token") or "")
    supplied = str(request.headers.get("X-CSRF-Token") or "").strip()
    if expected and supplied and hmac.compare_digest(supplied, expected):
        return None
    return jsonify({"success": False, "error": "CSRF token missing or invalid."}), 403


def _auth_error_payload() -> dict:
    return {"success": False, "error": "Административный вход не выполнен."}


def _coerce_bool(value: Any, *, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    normalized = str(value or "").strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default


def _secret_fingerprint(secret: str) -> str:
    return hmac.new(
        secret.encode("utf-8"),
        b"sup-admin-session-v1",
        hashlib.sha256,
    ).hexdigest()


def _client_key() -> str:
    forwarded = str(request.headers.get("X-Forwarded-For") or "").split(",", 1)[0].strip()
    return forwarded or str(request.remote_addr or "unknown")


def _is_blocked(client_key: str) -> bool:
    now = time.time()
    with _rate_limit_lock:
        state = _rate_limit_state.get(client_key)
        if not state:
            return False
        blocked_until = float(state.get("blocked_until") or 0)
        if blocked_until > now:
            return True
        if blocked_until:
            _rate_limit_state.pop(client_key, None)
        return False


def _record_failed_attempt(client_key: str) -> None:
    now = time.time()
    with _rate_limit_lock:
        state = _rate_limit_state.setdefault(
            client_key,
            {"attempts": [], "blocked_until": 0},
        )
        attempts = [
            timestamp
            for timestamp in state.get("attempts", [])
            if now - float(timestamp) <= ADMIN_FAILURE_WINDOW_SECONDS
        ]
        attempts.append(now)
        state["attempts"] = attempts
        if len(attempts) >= ADMIN_FAILURE_LIMIT:
            state["blocked_until"] = now + ADMIN_BLOCK_SECONDS


def _clear_rate_limit(client_key: str) -> None:
    with _rate_limit_lock:
        _rate_limit_state.pop(client_key, None)


def _wants_json_response() -> bool:
    if request.path.startswith("/admin/va/schedule-manager/api"):
        return True
    if request.headers.get("Accept", "").lower().find("application/json") >= 0:
        return True
    if request.is_json:
        return True
    return False
