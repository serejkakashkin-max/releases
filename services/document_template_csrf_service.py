from __future__ import annotations

import hmac
import re
import secrets
from urllib.parse import urlsplit

from flask import current_app, g, request

from services.public_url_service import public_url_for


DOCUMENT_TEMPLATE_ACTOR = "Пользователь Oplot"
CSRF_COOKIE_NAME = "oplot_dtc_csrf"
_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{32,128}\Z")
_G_TOKEN = "document_template_csrf_token"
_G_SET_COOKIE = "document_template_csrf_set_cookie"


def _valid_token(value: object) -> str:
    token = str(value or "")
    return token if _TOKEN_PATTERN.fullmatch(token) else ""


def _secure_cookie_enabled() -> bool:
    value = current_app.config.get("SESSION_COOKIE_SECURE", False)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def csrf_cookie_path() -> str:
    path = urlsplit(public_url_for("document_templates.index")).path or "/"
    return path.rstrip("/") + "/"


def csrf_token() -> str:
    existing = getattr(g, _G_TOKEN, "")
    if existing:
        return existing
    token = _valid_token(request.cookies.get(CSRF_COOKIE_NAME))
    if not token:
        token = secrets.token_urlsafe(32)
        setattr(g, _G_SET_COOKIE, True)
    setattr(g, _G_TOKEN, token)
    return token


def csrf_is_valid() -> bool:
    expected = _valid_token(request.cookies.get(CSRF_COOKIE_NAME))
    supplied = _valid_token(
        request.headers.get("X-CSRF-Token") or request.form.get("_csrf_token")
    )
    return bool(expected and supplied and hmac.compare_digest(expected, supplied))


def apply_csrf_cookie(response):
    token = getattr(g, _G_TOKEN, "")
    if token and getattr(g, _G_SET_COOKIE, False):
        response.set_cookie(
            CSRF_COOKIE_NAME,
            token,
            max_age=None,
            secure=_secure_cookie_enabled(),
            httponly=True,
            samesite="Lax",
            path=csrf_cookie_path(),
        )
    return response
