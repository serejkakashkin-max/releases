from __future__ import annotations

import importlib.util
import logging
from pathlib import Path
from typing import Any, Dict

from flask import Flask

from services.feature_flags_service import is_module_enabled


logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
VA_PACKAGE_DIR = PROJECT_ROOT / "VA" / "schedule_manager"
VA_URL_PREFIX = "/admin/va/schedule-manager"

_metadata: Dict[str, Any] = {
    "configured": False,
    "package_present": False,
    "enabled": False,
    "loaded": False,
    "status": "not_registered",
    "error": "",
    "exception_type": "",
    "url": None,
    "version": "",
    "runtime": {},
}


def register_va_schedule_manager(app: Flask) -> None:
    global _metadata

    configured = True
    package_present = VA_PACKAGE_DIR.exists()
    enabled = is_module_enabled("va_schedule_manager", default=False)
    _metadata = _base_metadata(
        configured=configured,
        package_present=package_present,
        enabled=enabled,
    )

    if not package_present:
        logger.warning("VA Schedule Manager package is not present; optional module skipped.")
        _metadata["status"] = "missing"
        return

    if not enabled:
        logger.info("VA Schedule Manager module is disabled by SUP configuration.")
        _metadata["status"] = "disabled"
        return

    try:
        _ensure_va_dependencies()
        from VA.schedule_manager.config import (
            APP_VERSION,
            DATA_DIR,
            EXPORT_DIR,
            STATE_DIR,
            UPLOAD_DIR,
            ensure_runtime_dirs,
        )
        from VA.schedule_manager.module import create_schedule_manager_blueprint

        ensure_runtime_dirs()
        app.register_blueprint(
            create_schedule_manager_blueprint(),
            url_prefix=VA_URL_PREFIX,
        )
        _metadata.update(
            {
                "loaded": True,
                "status": "loaded",
                "url": f"{VA_URL_PREFIX}/",
                "version": APP_VERSION,
                "runtime": {
                    "data": _safe_runtime_status(DATA_DIR),
                    "uploads": _safe_runtime_status(UPLOAD_DIR),
                    "exports": _safe_runtime_status(EXPORT_DIR),
                    "state": _safe_runtime_status(STATE_DIR),
                },
            }
        )
    except Exception as exc:
        logger.exception("VA Schedule Manager failed to load.")
        _metadata.update(
            {
                "loaded": False,
                "status": "import_error",
                "error": "Не удалось загрузить модуль Schedule Manager. Подробности записаны в журнал приложения.",
                "exception_type": type(exc).__name__,
                "url": None,
            }
        )


def get_va_schedule_manager_metadata() -> Dict[str, Any]:
    return dict(_metadata)


def _base_metadata(*, configured: bool, package_present: bool, enabled: bool) -> Dict[str, Any]:
    return {
        "configured": configured,
        "package_present": package_present,
        "enabled": enabled,
        "loaded": False,
        "status": "not_loaded",
        "error": "",
        "exception_type": "",
        "url": None,
        "version": "",
        "runtime": {},
    }


def _ensure_va_dependencies() -> None:
    missing = [
        module_name
        for module_name in ("xlrd", "portalocker")
        if importlib.util.find_spec(module_name) is None
    ]
    if missing:
        raise ModuleNotFoundError(", ".join(missing))


def _safe_runtime_status(path: Path) -> Dict[str, Any]:
    try:
        exists = path.exists()
        items = sum(1 for _ in path.iterdir()) if exists and path.is_dir() else 0
        return {"exists": exists, "items": items}
    except OSError:
        return {"exists": False, "items": 0}
