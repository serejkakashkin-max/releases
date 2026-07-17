from __future__ import annotations

from pathlib import Path


APP_VERSION = "0.1"
PROJECT_ROOT = Path(__file__).resolve().parents[2]
RUNTIME_ROOT = PROJECT_ROOT / "cache" / "ta_incident_auditor"
UPLOAD_DIR = RUNTIME_ROOT / "uploads"
EXPORT_DIR = RUNTIME_ROOT / "exports"
STATE_DIR = RUNTIME_ROOT / "state"


def ensure_runtime_dirs() -> None:
    for path in (UPLOAD_DIR, EXPORT_DIR, STATE_DIR):
        path.mkdir(parents=True, exist_ok=True)
