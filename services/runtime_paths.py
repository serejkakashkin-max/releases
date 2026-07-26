from __future__ import annotations

import os
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
RUNTIME_ENV = "RELEASE_WEB_RUNTIME_DIR"


def get_runtime_root() -> Path:
    configured = str(os.environ.get(RUNTIME_ENV) or "").strip()
    return Path(configured).expanduser().resolve() if configured else PROJECT_ROOT


def runtime_path(*parts: str) -> Path:
    return get_runtime_root().joinpath(*parts)


def get_runtime_location_id() -> str:
    return "configured" if str(os.environ.get(RUNTIME_ENV) or "").strip() else "local_default"


def get_va_runtime_root() -> Path:
    root = runtime_path("cache", "va_schedule_manager")
    legacy = str(os.environ.get("VA_SCHEDULE_MANAGER_RUNTIME_ROOT") or "").strip()
    if legacy and str(os.environ.get(RUNTIME_ENV) or "").strip():
        if Path(legacy).expanduser().resolve() != root:
            raise RuntimeError("VA runtime root conflicts with RELEASE_WEB_RUNTIME_DIR.")
    return Path(legacy).expanduser().resolve() if legacy else root


def get_coordination_lock_path() -> Path:
    return runtime_path(
        "cache",
        "employee_directory_backups",
        "employee_directory_va_coordination.lock",
    )
