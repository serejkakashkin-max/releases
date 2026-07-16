import os
from pathlib import Path


def _normalized_base_path(value: str) -> str:
    cleaned = value.strip().strip("/")
    if not cleaned:
        return ""
    return f"/{cleaned}"


MODULE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = MODULE_DIR.parents[1]
BASE_DIR = MODULE_DIR

APP_NAME = "va-schedule-manager"
APP_TITLE = "Графики дежурств"
APP_VERSION = os.environ.get("VA_SCHEDULE_MANAGER_VERSION", "0.1.0-integrated")
BASE_PATH = _normalized_base_path(os.environ.get("VA_SCHEDULE_MANAGER_BASE_PATH", ""))
PUBLIC_BASE_URL = os.environ.get("VA_SCHEDULE_MANAGER_PUBLIC_BASE_URL", "").rstrip("/")

RUNTIME_ROOT = Path(
    os.environ.get(
        "VA_SCHEDULE_MANAGER_RUNTIME_ROOT",
        PROJECT_ROOT / "cache" / "va_schedule_manager",
    )
)
DATA_DIR = RUNTIME_ROOT / "data"
UPLOAD_DIR = RUNTIME_ROOT / "uploads"
EXPORT_DIR = RUNTIME_ROOT / "exports"
STATE_DIR = RUNTIME_ROOT / "state"
LOCK_DIR = STATE_DIR / "locks"
BACKUP_DIR = STATE_DIR / "backups"
MIGRATION_REPORT_DIR = STATE_DIR / "migration_reports"

DOCS_DIR = MODULE_DIR / "docs"
SAMPLE_DATA_DIR = MODULE_DIR / "sample_data"

SCHEDULE_DATA_FILE = DATA_DIR / "schedule_data.json"
EMPLOYEES_DATA_FILE = DATA_DIR / "employees.json"
COMPETENCIES_DATA_FILE = DATA_DIR / "competencies.json"
SHIFTS_DATA_FILE = DATA_DIR / "shifts.json"
SCHEDULE_EDITS_FILE = DATA_DIR / "schedule_edits.json"
INTEGRATION_SETTINGS_FILE = DATA_DIR / "integration_settings.json"

MAX_UPLOAD_SIZE_BYTES = 20 * 1024 * 1024
ALLOWED_EXTENSIONS = {".xlsx", ".xls"}
EMPLOYEE_SHEET_NAME = "Справочник"

ENABLE_SAMPLE_ENDPOINTS = (
    os.environ.get("VA_SCHEDULE_MANAGER_ENABLE_SAMPLE_ENDPOINTS", "")
    .strip()
    .lower()
    in {"1", "true", "yes", "on"}
)


def ensure_runtime_dirs() -> None:
    for path in (
        DATA_DIR,
        UPLOAD_DIR,
        EXPORT_DIR,
        STATE_DIR,
        LOCK_DIR,
        BACKUP_DIR,
        MIGRATION_REPORT_DIR,
    ):
        path.mkdir(parents=True, exist_ok=True)
