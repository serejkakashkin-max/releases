from __future__ import annotations

import hashlib
import hmac
import ipaddress
import os
import re
import secrets
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlsplit

from flask import current_app, request, session

from config import TOKENS
from services.cross_process_file_lock import CrossProcessFileLock, FileLockTimeoutError
from services.document_template_runtime_service import RuntimeStateError, atomic_write_json, read_json
from services.public_url_service import public_base_path, public_url_for
from services.runtime_paths import runtime_path


SESSION_PREFIX = "document_template_editor_"
AUTHENTICATED = SESSION_PREFIX + "authenticated"
ACTOR = SESSION_PREFIX + "actor"
LOGIN_AT = SESSION_PREFIX + "login_at"
LAST_SEEN = SESSION_PREFIX + "last_seen"
TOKEN_FINGERPRINT = SESSION_PREFIX + "token_fingerprint"
CSRF_NONCE = SESSION_PREFIX + "csrf_nonce"
ABSOLUTE_SECONDS = 4 * 60 * 60
IDLE_SECONDS = 30 * 60
PAIR_LIMIT = 5
SOURCE_LIMIT = 50
WINDOW_SECONDS = 10 * 60
BLOCK_SECONDS = 5 * 60
MAX_BUCKETS = 10_000
SAFE_CATALOG_QUERY = {"q", "category", "ke", "variant", "page"}
DENIED_SECRETS = {
    "super_secret_key", "secret", "changeme", "change_me", "default", "test",
    "development", "dev", "password", "your-secret-key", "replace-me",
}


class UnsafeSessionConfiguration(RuntimeError):
    pass


class RateLimitStorageError(RuntimeError):
    pass


@dataclass(frozen=True)
class LoginRateStatus:
    allowed: bool
    code: str = ""


def _coerce_bool(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def strong_session_secret() -> bytes:
    value = current_app.secret_key
    if isinstance(value, bytes):
        raw = value
        text = value.decode("utf-8", "ignore")
    else:
        text = str(value or "")
        raw = text.encode("utf-8")
    normalized = text.strip().casefold()
    if len(raw) < 32 or normalized in DENIED_SECRETS:
        raise UnsafeSessionConfiguration("Editor session is not safely configured.")
    compact = re.sub(r"[^a-z0-9]", "", normalized)
    if compact and len(set(compact)) <= 2:
        raise UnsafeSessionConfiguration("Editor session is not safely configured.")
    if re.fullmatch(r"(.{1,8})\1{3,}", normalized):
        raise UnsafeSessionConfiguration("Editor session is not safely configured.")
    return raw


def editor_token() -> str:
    override = current_app.config.get("DOCUMENT_TEMPLATE_EDITOR_TOKEN")
    return str(override or os.environ.get("DOCUMENT_TEMPLATE_EDITOR_TOKEN") or TOKENS.get("document_template_editor_token") or "").strip()


def normalize_display_name(value: str) -> str:
    result = unicodedata.normalize("NFC", str(value or "")).strip()
    if not 2 <= len(result) <= 100 or any(unicodedata.category(char).startswith("C") for char in result):
        raise ValueError("Введите имя длиной от 2 до 100 символов.")
    return result


def _fingerprint(secret: bytes, value: str, domain: bytes) -> str:
    return hmac.new(secret, domain + b"\0" + value.encode("utf-8"), hashlib.sha256).hexdigest()


def _token_fingerprint(secret: bytes, token: str) -> str:
    return _fingerprint(secret, token, b"document-template-editor-token-v1")


def client_source() -> str:
    fallback = str(request.remote_addr or "unknown")
    if not _coerce_bool(current_app.config.get("TRUST_PROXY_HEADERS", os.environ.get("TRUST_PROXY_HEADERS"))):
        return fallback
    for candidate in str(request.headers.get("X-Forwarded-For") or "").split(","):
        try:
            return ipaddress.ip_address(candidate.strip()).compressed
        except ValueError:
            continue
    return fallback


def _rate_paths() -> tuple[Path, Path]:
    override = current_app.config.get("DOCUMENT_TEMPLATE_CENTER_RUNTIME_ROOT")
    root = Path(override) if override else runtime_path()
    base = root / "cache" / "document_template_center" / "auth"
    return base / "rate_limit.json", base / "rate_limit.lock"


def _rate_key(secret: bytes) -> bytes:
    return hmac.new(secret, b"document-template-rate-limit-key-v1", hashlib.sha256).digest()


def _bucket_id(master: bytes, domain: str, value: str) -> str:
    return hmac.new(master, f"{domain}\0{value}".encode("utf-8"), hashlib.sha256).hexdigest()


def _load_rate_state(path: Path) -> dict[str, Any]:
    state = read_json(path, missing={"version": 1, "buckets": {}})
    if not isinstance(state, dict) or state.get("version") != 1 or not isinstance(state.get("buckets"), dict):
        raise RateLimitStorageError("Login protection storage is unavailable.")
    return state


def _clean_buckets(buckets: dict[str, Any], now: float) -> None:
    for key, bucket in list(buckets.items()):
        if not isinstance(bucket, dict):
            raise RateLimitStorageError("Login protection storage is unavailable.")
        attempts = [float(item) for item in bucket.get("attempts", []) if now - float(item) <= WINDOW_SECONDS]
        blocked_until = float(bucket.get("blocked_until", 0))
        if not attempts and blocked_until <= now:
            del buckets[key]
        else:
            bucket["attempts"] = attempts
            bucket["blocked_until"] = blocked_until


def check_or_update_rate_limit(display_name: str, *, failure: bool = False, success: bool = False, now: float | None = None) -> LoginRateStatus:
    secret = strong_session_secret()
    master = _rate_key(secret)
    source = client_source()
    normalized_name = unicodedata.normalize("NFC", display_name).casefold()
    source_id = _bucket_id(master, "source", source)
    name_id = _bucket_id(master, "display", normalized_name)
    pair_id = _bucket_id(master, "pair", source_id + name_id)
    state_path, lock_path = _rate_paths()
    timestamp = float(time.time() if now is None else now)
    try:
        with CrossProcessFileLock(lock_path, timeout=5):
            state = _load_rate_state(state_path)
            buckets = state["buckets"]
            _clean_buckets(buckets, timestamp)
            for key in (pair_id, source_id):
                bucket = buckets.get(key, {})
                if float(bucket.get("blocked_until", 0)) > timestamp:
                    return LoginRateStatus(False, "rate_limited")
            if success:
                buckets.pop(pair_id, None)
                atomic_write_json(state_path, state)
                return LoginRateStatus(True)
            if failure:
                missing = sum(1 for key in (pair_id, source_id) if key not in buckets)
                if len(buckets) + missing > MAX_BUCKETS:
                    return LoginRateStatus(False, "rate_limit_capacity")
                for key, limit in ((pair_id, PAIR_LIMIT), (source_id, SOURCE_LIMIT)):
                    bucket = buckets.setdefault(key, {"attempts": [], "blocked_until": 0})
                    bucket["attempts"].append(timestamp)
                    if len(bucket["attempts"]) >= limit:
                        bucket["blocked_until"] = timestamp + BLOCK_SECONDS
                atomic_write_json(state_path, state)
                blocked = any(float(buckets[key]["blocked_until"]) > timestamp for key in (pair_id, source_id))
                return LoginRateStatus(not blocked, "rate_limited" if blocked else "")
            atomic_write_json(state_path, state)
            return LoginRateStatus(True)
    except (RuntimeStateError, FileLockTimeoutError, OSError, ValueError, TypeError) as exc:
        raise RateLimitStorageError("Login protection storage is unavailable.") from exc


def login_editor(display_name: str, supplied_token: str, *, now: int | None = None) -> tuple[bool, str]:
    secret = strong_session_secret()
    actor = normalize_display_name(display_name)
    expected = editor_token()
    if not expected:
        return False, "editor_token_missing"
    status = check_or_update_rate_limit(actor)
    if not status.allowed:
        return False, status.code
    supplied = str(supplied_token or "")
    if not supplied or not hmac.compare_digest(supplied, expected):
        status = check_or_update_rate_limit(actor, failure=True)
        return False, status.code or "invalid_credentials"
    check_or_update_rate_limit(actor, success=True)
    timestamp = int(time.time() if now is None else now)
    session[AUTHENTICATED] = True
    session[ACTOR] = actor
    session[LOGIN_AT] = timestamp
    session[LAST_SEEN] = timestamp
    session[TOKEN_FINGERPRINT] = _token_fingerprint(secret, expected)
    session[CSRF_NONCE] = secrets.token_urlsafe(32)
    session.modified = True
    return True, ""


def editor_session_actor(*, now: int | None = None, touch: bool = True) -> str | None:
    secret = strong_session_secret()
    if not session.get(AUTHENTICATED):
        return None
    timestamp = int(time.time() if now is None else now)
    try:
        login_at = int(session.get(LOGIN_AT, 0))
        last_seen = int(session.get(LAST_SEEN, 0))
    except (TypeError, ValueError):
        clear_editor_session()
        return None
    expected = editor_token()
    actual_fingerprint = str(session.get(TOKEN_FINGERPRINT) or "")
    if not expected or not hmac.compare_digest(actual_fingerprint, _token_fingerprint(secret, expected)):
        clear_editor_session()
        return None
    if timestamp - login_at > ABSOLUTE_SECONDS or timestamp - last_seen > IDLE_SECONDS:
        clear_editor_session()
        return None
    actor = str(session.get(ACTOR) or "")
    try:
        actor = normalize_display_name(actor)
    except ValueError:
        clear_editor_session()
        return None
    if touch and timestamp - last_seen >= 60:
        session[LAST_SEEN] = timestamp
        session.modified = True
    return actor


def clear_editor_session() -> None:
    for key in list(session.keys()):
        if str(key).startswith(SESSION_PREFIX):
            session.pop(key, None)


def csrf_token() -> str:
    return str(session.get(CSRF_NONCE) or "") if editor_session_actor(touch=False) else ""


def csrf_is_valid() -> bool:
    expected = csrf_token()
    supplied = str(request.headers.get("X-CSRF-Token") or request.form.get("_csrf_token") or "")
    return bool(expected and supplied and hmac.compare_digest(expected, supplied))


def _bounded_decode(value: str) -> str:
    decoded = str(value or "")[:4096]
    for _ in range(3):
        next_value = unquote(decoded)
        if next_value == decoded:
            break
        decoded = next_value
    return decoded


def safe_next(value: str | None) -> str:
    fallback = public_url_for("document_templates.index")
    decoded = _bounded_decode(value or "")
    if not decoded or "\\" in decoded or any(ord(char) < 32 or ord(char) == 127 for char in decoded):
        return fallback
    parsed = urlsplit(decoded)
    if parsed.scheme or parsed.netloc or parsed.fragment or decoded.startswith("//"):
        return fallback
    path = parsed.path
    base = public_base_path()
    if base and (path == base or path.startswith(base + "/")):
        path = path[len(base):] or "/"
    center = "/admin/document-templates"
    if not (path == center or path == center + "/" or path.startswith(center + "/")):
        return fallback
    segments = [item for item in path.split("/") if item]
    if any(item in {".", ".."} for item in segments):
        return fallback
    query = parse_qsl(parsed.query, keep_blank_values=True)
    if any(key in {"token", "_csrf_token", "next"} for key, _ in query):
        return fallback
    if query and path.rstrip("/") != center:
        return fallback
    if any(key not in SAFE_CATALOG_QUERY for key, _ in query):
        return fallback
    normalized = base + path if base else path
    return normalized + (("?" + urlencode(query)) if query else "")


def login_url(next_url: str | None = None) -> str:
    url = public_url_for("document_templates.login_page")
    destination = safe_next(next_url) if next_url else ""
    return url + (("?" + urlencode({"next": destination})) if destination else "")
