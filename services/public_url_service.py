from __future__ import annotations

import os
from urllib.parse import urlparse

from flask import request, url_for

try:
    from config import TOKENS
except Exception:  # pragma: no cover - config import failures are handled by startup
    TOKENS = {}


_ROOT_MARKERS = (
    "/admin/",
    "/dashboard/",
    "/release-monitor",
    "/release/",
    "/mpr",
    "/help",
    "/sandbox",
)


def _normalize_prefix(value: str | None) -> str:
    prefix = str(value or "").strip()
    if not prefix or prefix == "/":
        return ""
    if "://" in prefix:
        prefix = urlparse(prefix).path
    prefix = "/" + prefix.strip("/")
    return "" if prefix == "/" else prefix


def _is_local_request() -> bool:
    host = (request.host or "").split(":", 1)[0].lower()
    return host in {"127.0.0.1", "localhost", "::1"}


def _prefix_from_public_url() -> str:
    public_url = str(TOKENS.get("release_monitor_public_base_url") or "").strip()
    if not public_url:
        return ""

    path = urlparse(public_url).path
    for marker in _ROOT_MARKERS:
        index = path.find(marker)
        if index > 0:
            return _normalize_prefix(path[:index])
    return ""


def public_base_path() -> str:
    forwarded_prefix = (
        request.headers.get("X-Forwarded-Prefix")
        or request.headers.get("X-Script-Name")
        or request.environ.get("HTTP_X_FORWARDED_PREFIX")
        or request.environ.get("SCRIPT_NAME")
        or request.script_root
    )
    prefix = _normalize_prefix(forwarded_prefix)
    if prefix:
        return prefix

    env_prefix = (
        os.environ.get("BASE_PATH")
        or os.environ.get("PUBLIC_BASE_PATH")
        or os.environ.get("APP_BASE_PATH")
        or os.environ.get("APPLICATION_ROOT")
    )
    prefix = _normalize_prefix(env_prefix)
    if prefix:
        return prefix

    if not _is_local_request():
        return _prefix_from_public_url() or "/releases"

    return ""


def with_public_base(url: str) -> str:
    if not url or url.startswith(("http://", "https://", "//")):
        return url
    prefix = public_base_path()
    if not prefix:
        return url
    if url == prefix or url.startswith(prefix + "/"):
        return url
    if not url.startswith("/"):
        url = "/" + url
    return prefix + url


def public_url_for(endpoint: str, **values) -> str:
    return with_public_base(url_for(endpoint, **values))
