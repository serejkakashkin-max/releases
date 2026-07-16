from VA.schedule_manager.config import APP_NAME, APP_TITLE, APP_VERSION, BASE_PATH, PUBLIC_BASE_URL


def prefixed_path(path: str) -> str:
    normalized = path if path.startswith("/") else f"/{path}"
    return f"{BASE_PATH}{normalized}" if BASE_PATH else normalized


def build_integration_manifest() -> dict:
    return {
        "module": {
            "name": APP_NAME,
            "title": APP_TITLE,
            "version": APP_VERSION,
            "base_path": BASE_PATH,
            "public_base_url": PUBLIC_BASE_URL,
        },
        "ui": {
            "entrypoint": prefixed_path("/"),
            "settings": prefixed_path("/settings/employees"),
            "docs": prefixed_path("/docs"),
        },
        "health": {
            "readiness": prefixed_path("/integration/health"),
            "status": prefixed_path("/api/status"),
            "today": prefixed_path("/api/today"),
        },
        "api": {
            "prefix": prefixed_path("/api"),
            "contract": "ok/data/error/meta",
            "docs": prefixed_path("/docs/file/api.md"),
        },
        "storage": {
            "source_of_truth": "data/schedule_data.json",
            "excel_after_import": "not_used",
        },
        "embedding": {
            "recommended": "mount_wsgi_app(create_app(), base_path) или reverse proxy с тем же префиксом",
            "standalone_env": "SCHEDULE_MANAGER_BASE_PATH",
        },
    }
