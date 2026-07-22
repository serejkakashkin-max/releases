from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict

from services.duty_schedule_provider_registry import get_duty_schedule_provider_revision
from services.release_template_catalog_service import get_template_catalog_signature


VIEW_CONTRACT = 1
_FINGERPRINT_CACHE = {}
_FINGERPRINT_CACHE_LOCK = threading.RLock()


def get_file_signature(path) -> Dict[str, object]:
    path = Path(path)
    try:
        stat = path.stat()
    except OSError:
        return {"exists": False, "mtime_ns": 0, "size": 0}
    if not path.is_file():
        return {"exists": False, "mtime_ns": 0, "size": 0}
    return {
        "exists": True,
        "mtime_ns": int(stat.st_mtime_ns),
        "size": int(stat.st_size),
    }


def _content_fingerprint(path: Path, signature: Dict[str, object]) -> str:
    if not signature["exists"]:
        return ""
    try:
        change_ns = int(path.stat().st_ctime_ns)
    except OSError:
        return ""
    cache_key = str(path.resolve())
    source_key = (
        bool(signature["exists"]),
        int(signature["mtime_ns"]),
        int(signature["size"]),
        change_ns,
    )
    with _FINGERPRINT_CACHE_LOCK:
        cached = _FINGERPRINT_CACHE.get(cache_key)
        if cached and cached["source_key"] == source_key:
            return str(cached["fingerprint"])

    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        return ""
    fingerprint = "sha256:" + digest.hexdigest()
    with _FINGERPRINT_CACHE_LOCK:
        _FINGERPRINT_CACHE[cache_key] = {
            "source_key": source_key,
            "fingerprint": fingerprint,
        }
    return fingerprint


def get_file_revision_component(path) -> Dict[str, object]:
    path = Path(path)
    signature = get_file_signature(path)
    return {
        **signature,
        "fingerprint": _content_fingerprint(path, signature),
    }


def _format_updated_at(mtime_ns: int) -> str:
    if not mtime_ns:
        return ""
    return datetime.fromtimestamp(mtime_ns / 1_000_000_000, tz=timezone.utc).isoformat()


def get_release_monitor_view_state(*, force_template_check: bool = False) -> Dict[str, object]:
    from services import release_monitor_service as release_monitor

    runtime_paths = {
        "snapshot": release_monitor.SNAPSHOT_FILE,
        "manual_releases": release_monitor.MANUAL_RELEASES_FILE,
        "manual_overrides": release_monitor.MANUAL_OVERRIDES_FILE,
        "reviewers": release_monitor.REVIEWERS_FILE,
        "order": release_monitor.ORDER_FILE,
        "dates": release_monitor.DATE_OVERRIDES_FILE,
        "zni": release_monitor.ZNI_FILE,
        "work_marks": release_monitor.WORK_MARKS_FILE,
        "attempts": release_monitor.ATTEMPTS_FILE,
        "base_revision_file": release_monitor.REVISION_FILE,
    }
    source_components = {
        path_id: get_file_revision_component(path)
        for path_id, path in runtime_paths.items()
    }
    template_component = get_template_catalog_signature(force=force_template_check)
    va_component = get_duty_schedule_provider_revision()
    revision_payload = {
        "view_contract": VIEW_CONTRACT,
        "base_revision": release_monitor.get_release_monitor_base_revision(),
        "sources": source_components,
        "va": va_component,
        "template_catalog": template_component,
    }
    serialized = json.dumps(
        revision_payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    va_mtimes = [
        int(signature.get("mtime_ns") or 0)
        for signature in (va_component.get("source_signatures") or {}).values()
        if isinstance(signature, dict)
    ]
    max_mtime_ns = max(
        [int(component["mtime_ns"]) for component in source_components.values()]
        + [int(template_component.get("max_mtime_ns") or 0)]
        + va_mtimes,
        default=0,
    )
    return {
        "view_revision": "sha256:" + hashlib.sha256(serialized).hexdigest(),
        "updated_at": _format_updated_at(max_mtime_ns),
        "va_status": str(va_component.get("status") or "missing_provider"),
    }
