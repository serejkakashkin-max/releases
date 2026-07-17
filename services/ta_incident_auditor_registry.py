from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict

from flask import Flask

from services.feature_flags_service import is_module_enabled
from services.sandbox_registry import register_sandbox_module


logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TA_PACKAGE_DIR = PROJECT_ROOT / "TA" / "incident_auditor"
TA_URL_PREFIX = "/sandbox/ta/incident-auditor"

_metadata: Dict[str, Any] = {
    "configured": False,
    "package_present": False,
    "enabled": False,
    "loaded": False,
    "status": "not_registered",
    "error": "",
    "exception_type": "",
}


def register_ta_incident_auditor(app: Flask) -> None:
    global _metadata

    package_present = TA_PACKAGE_DIR.exists()
    enabled = is_module_enabled("ta_incident_auditor", default=False)
    _metadata = {
        "configured": True,
        "package_present": package_present,
        "enabled": enabled,
        "loaded": False,
        "status": "not_loaded",
        "error": "",
        "exception_type": "",
    }

    if not package_present:
        logger.warning("TA Incident Auditor package is not present; optional module skipped.")
        _metadata["status"] = "missing"
        return

    if not enabled:
        logger.info("TA Incident Auditor module is disabled by SUP configuration.")
        _metadata["status"] = "disabled"
        return

    try:
        from TA.incident_auditor.config import APP_VERSION, ensure_runtime_dirs
        from TA.incident_auditor.module import create_incident_auditor_blueprint

        ensure_runtime_dirs()
        app.register_blueprint(
            create_incident_auditor_blueprint(),
            url_prefix=TA_URL_PREFIX,
        )
        register_sandbox_module(
            {
                "id": "ta_incident_auditor",
                "owner_code": "TA",
                "owner_name": "Тутов Артём",
                "title": "Аудитор инцидентов",
                "description": "Экспериментальный инструмент анализа инцидентов",
                "version": APP_VERSION,
                "status": "experimental",
                "enabled": True,
                "loaded": True,
                "endpoint": "ta_incident_auditor.index",
                "url": None,
            }
        )
        _metadata.update({"loaded": True, "status": "loaded"})
    except Exception as exc:
        logger.exception("TA Incident Auditor failed to load.")
        _metadata.update(
            {
                "loaded": False,
                "status": "import_error",
                "error": "Не удалось загрузить модуль TA «Аудитор инцидентов». Подробности записаны в журнал приложения.",
                "exception_type": type(exc).__name__,
            }
        )


def get_ta_incident_auditor_metadata() -> Dict[str, Any]:
    return dict(_metadata)
