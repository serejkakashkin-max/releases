from __future__ import annotations

import json
import os
from typing import Any, Dict, Optional
from uuid import uuid4

from services.cross_process_file_lock import (
    CrossProcessFileLock,
    FileLockTimeoutError,
)
from services.runtime_paths import runtime_path


REFRESH_LOCK_FILE = runtime_path("cache", "release_monitor_refresh.lock")
REFRESH_STATUS_FILE = runtime_path("cache", "release_monitor_refresh_status.json")
STATUS_SCHEMA_VERSION = 1

_ALLOWED_STATES = {
    "idle",
    "refreshing",
    "completed",
    "rejected",
    "failed",
    "interrupted",
    "skipped",
}
_ALLOWED_MODES = {"quick", "full", "reliable_full", "auto_incremental"}
_ALLOWED_TRIGGERS = {"manual", "auto"}
_TEXT_FIELDS = (
    "message",
    "started_at",
    "finished_at",
    "confirmed_snapshot_at",
    "error_code",
)
_COUNT_FIELDS = ("previous_total", "candidate_total")


def default_refresh_status() -> Dict[str, Any]:
    return {
        "schema_version": STATUS_SCHEMA_VERSION,
        "state": "idle",
        "mode": "",
        "trigger": "",
        "message": "",
        "started_at": "",
        "finished_at": "",
        "confirmed_snapshot_at": "",
        "previous_total": None,
        "candidate_total": None,
        "error_code": "",
    }


def sanitize_refresh_status(value: Any) -> Dict[str, Any]:
    source = value if isinstance(value, dict) else {}
    status = default_refresh_status()
    state = str(source.get("state") or "idle").strip().lower()
    mode = str(source.get("mode") or "").strip().lower()
    trigger = str(source.get("trigger") or "").strip().lower()
    status["state"] = state if state in _ALLOWED_STATES else "failed"
    status["mode"] = mode if mode in _ALLOWED_MODES else ""
    status["trigger"] = trigger if trigger in _ALLOWED_TRIGGERS else ""
    for field in _TEXT_FIELDS:
        status[field] = str(source.get(field) or "").strip()[:500]
    for field in _COUNT_FIELDS:
        raw = source.get(field)
        status[field] = max(0, int(raw)) if isinstance(raw, (int, float)) else None
    return status


def read_persisted_refresh_status() -> Dict[str, Any]:
    try:
        payload = json.loads(REFRESH_STATUS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return default_refresh_status()
    return sanitize_refresh_status(payload)


def write_persisted_refresh_status(value: Any) -> Dict[str, Any]:
    status = sanitize_refresh_status(value)
    REFRESH_STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
    temp_path = REFRESH_STATUS_FILE.with_name(
        f".{REFRESH_STATUS_FILE.name}.{uuid4().hex}.tmp"
    )
    serialized = json.dumps(
        status,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    try:
        with temp_path.open("wb") as handle:
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, REFRESH_STATUS_FILE)
    finally:
        if temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass
    return status


def try_acquire_refresh_lock() -> Optional[CrossProcessFileLock]:
    lock = CrossProcessFileLock(REFRESH_LOCK_FILE, timeout=0)
    try:
        lock.acquire()
    except FileLockTimeoutError:
        return None
    return lock


def is_refresh_lock_held() -> bool:
    lock = try_acquire_refresh_lock()
    if lock is None:
        return True
    lock.release()
    return False


def get_shared_refresh_status() -> Dict[str, Any]:
    status = read_persisted_refresh_status()
    lock_held = is_refresh_lock_held()
    if status["state"] == "refreshing" and not lock_held:
        return {
            **status,
            "state": "interrupted",
            "message": "Предыдущее обновление было прервано. Можно запустить новое.",
            "error_code": "refresh_interrupted",
        }
    if lock_held and status["state"] != "refreshing":
        return {
            **status,
            "state": "refreshing",
            "message": "Обновление таблицы Блока релизов завершается.",
            "finished_at": "",
            "error_code": "",
        }
    return status
