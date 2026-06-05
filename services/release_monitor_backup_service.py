import hashlib
import json
import logging
import os
import re
import socket
import time
import uuid
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

from services.release_monitor_service import (
    ATTEMPTS_FILE,
    DATE_OVERRIDES_FILE,
    DUTY_SCHEDULE_FILE,
    MANUAL_OVERRIDES_FILE,
    MANUAL_RELEASES_FILE,
    ORDER_FILE,
    REVIEWERS_FILE,
    REVISION_FILE,
    SNAPSHOT_FILE,
    WORK_MARKS_FILE,
    ZNI_FILE,
)


REPORTS_DIR = Path(__file__).resolve().parent.parent / "reports" / "release_monitor_backups"
BACKUP_MAX_AGE_HOURS = int(os.getenv("RELEASE_MONITOR_BACKUP_MAX_AGE_HOURS", "1"))

BACKUP_FILES = (
    SNAPSHOT_FILE,
    MANUAL_RELEASES_FILE,
    MANUAL_OVERRIDES_FILE,
    REVIEWERS_FILE,
    ORDER_FILE,
    DUTY_SCHEDULE_FILE,
    DATE_OVERRIDES_FILE,
    ZNI_FILE,
    WORK_MARKS_FILE,
    ATTEMPTS_FILE,
    REVISION_FILE,
)


def _ensure_reports_dir() -> None:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _file_manifest(path: Path) -> Dict[str, Any]:
    entry: Dict[str, Any] = {
        "name": path.name,
        "archive_path": f"cache/{path.name}",
        "exists": path.exists(),
    }
    if not path.exists():
        return entry

    stat = path.stat()
    entry.update(
        {
            "size_bytes": stat.st_size,
            "mtime": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
            "sha256": _sha256(path),
        }
    )

    if path == SNAPSHOT_FILE:
        try:
            with path.open("r", encoding="utf-8-sig") as handle:
                payload = json.load(handle)
            items = payload.get("items") if isinstance(payload, dict) else None
            entry["snapshot_items_count"] = len(items) if isinstance(items, list) else 0
        except Exception as exc:
            entry["snapshot_read_error"] = str(exc)

    return entry


def cleanup_old_release_monitor_cache_backups(max_age_hours: int = BACKUP_MAX_AGE_HOURS) -> int:
    if not REPORTS_DIR.exists():
        return 0

    now = time.time()
    removed = 0
    for path in REPORTS_DIR.glob("*.zip"):
        try:
            age_hours = (now - path.stat().st_mtime) / 3600
            if age_hours > max_age_hours:
                path.unlink()
                removed += 1
                logging.info("Release monitor backup: removed old ZIP %s", path.name)
        except Exception as exc:
            logging.warning("Release monitor backup: failed to remove %s: %s", path, exc)

    if removed:
        logging.info("Release monitor backup: cleaned up %s old ZIP files", removed)
    return removed


def create_release_monitor_cache_backup(reason: str = "manual_chat_download") -> Dict[str, Any]:
    _ensure_reports_dir()
    cleanup_old_release_monitor_cache_backups()

    backup_id = f"rmcache_{datetime.now():%Y%m%d%H%M%S}_{uuid.uuid4().hex[:8]}"
    filename = f"release_monitor_cache_{backup_id}.zip"
    path = REPORTS_DIR / filename

    file_entries: List[Dict[str, Any]] = [_file_manifest(file_path) for file_path in BACKUP_FILES]
    manifest = {
        "backup_id": backup_id,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "reason": reason,
        "hostname": socket.gethostname(),
        "files": file_entries,
    }

    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "manifest.json",
            json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"),
        )
        for file_path, entry in zip(BACKUP_FILES, file_entries):
            if entry.get("exists"):
                archive.write(file_path, entry["archive_path"])

    logging.info("Release monitor backup: created %s", path)
    return {
        "backup_id": backup_id,
        "path": str(path),
        "filename": filename,
        "files_count": sum(1 for entry in file_entries if entry.get("exists")),
        "missing_count": sum(1 for entry in file_entries if not entry.get("exists")),
    }


def get_release_monitor_cache_backup_path(backup_id: str) -> str:
    safe_id = re.sub(r"[^a-zA-Z0-9_-]", "", str(backup_id or ""))
    if not safe_id:
        return ""

    root = REPORTS_DIR.resolve()
    path = (REPORTS_DIR / f"release_monitor_cache_{safe_id}.zip").resolve()
    if root not in path.parents and path != root:
        return ""
    if not path.exists():
        return ""
    return str(path)
