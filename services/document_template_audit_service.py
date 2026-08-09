from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from flask import current_app

from services.cross_process_file_lock import CrossProcessFileLock
from services.document_template_storage_service import data_root


ALLOWED_FIELDS = {
    "actor", "action", "document_id", "relative_target", "candidate_uuid",
    "version_uuid", "comment", "old_sha", "new_sha", "result", "error_code",
    "operation_uuid", "sha256", "source_filename",
}


def append_audit_event(**values: Any) -> dict[str, Any]:
    event = {
        "event_uuid": str(uuid.uuid4()),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    for key in ALLOWED_FIELDS:
        if key in values and values[key] not in (None, ""):
            event[key] = str(values[key])
    audit_root = data_root() / "audit"
    audit_root.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    with CrossProcessFileLock(audit_root / "events.lock", timeout=5):
        with (audit_root / "events.jsonl").open("ab") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    return event
