import logging

from flask import Blueprint, jsonify, render_template, request

from config import TOKENS
from services.sup_parameters_service import (
    SupParametersConflictError,
    SupParametersValidationError,
    get_sup_parameters_data,
    save_sup_parameters,
)
from services.employee_directory_repository import (
    EmployeeDirectoryConflictError,
    EmployeeDirectoryStateError,
    EmployeeDirectoryValidationError,
    get_employee_directory_admin_data,
    save_employee_directory,
)
from services.employee_directory_service import (
    get_consumer_health,
    load_employee_directory_context,
)
from services.feature_flags_service import get_feature_flags
from services.release_monitor_service import (
    get_release_monitor_refresh_admin_status,
    start_release_monitor_refresh,
)
from services.employee_directory_operational_validator import (
    validate_employee_directory_operations,
)
from services.sup_admin_auth_service import csrf_protect_request, require_sup_admin_request
from services.va_schedule_manager_admin_service import (
    build_va_schedule_manager_admin_data,
)
from services.sup_ui_service import build_sup_admin_ui_config
from services.va_schedule_manager_registry import get_va_schedule_manager_metadata
from VA.schedule_manager.repositories.competency_repository import (
    CompetencyRepository,
)
from VA.schedule_manager.repositories.employee_settings_repository import (
    EmployeeSettingsConflictError,
    EmployeeSettingsRepository,
    EmployeeSettingsValidationError,
)
from VA.schedule_manager.repositories.managed_employee_repository import (
    ManagedEmployeeRepository,
)
from VA.schedule_manager.services.competency_service import (
    CompetencyConflictError,
    CompetencyInUseError,
    CompetencyService,
    CompetencyValidationError,
)


sup_parameters_bp = Blueprint("sup_parameters", __name__, url_prefix="/admin")


def _configured_token() -> str:
    return str(TOKENS.get("sup_admin_token") or "").strip()


def _request_token() -> str:
    return (
        str(request.headers.get("X-SUP-Admin-Token") or "").strip()
        or str(request.args.get("token") or "").strip()
    )


def _token_error():
    if not _configured_token():
        return jsonify(
            {
                "success": False,
                "error": "В config.json не задан sup_admin_token. Доступ к СУП-параметрам закрыт.",
            }
        ), 403
    return jsonify({"success": False, "error": "Неверный token СУП-параметров."}), 403


def _require_token():
    configured = _configured_token()
    return bool(configured and _request_token() == configured)


def _validate_directory_operational(directory_payload):
    try:
        from VA.schedule_manager.repositories.employee_settings_repository import (
            EmployeeSettingsRepository,
        )
        settings_snapshot = EmployeeSettingsRepository().read()
    except Exception:
        settings_snapshot = None
    return validate_employee_directory_operations(
        directory_payload,
        get_feature_flags(),
        va_settings_snapshot=settings_snapshot,
    )


def _va_competency_service() -> CompetencyService:
    return CompetencyService(
        CompetencyRepository(),
        ManagedEmployeeRepository(),
    )


@sup_parameters_bp.get("/sup-parameters")
def sup_parameters_page():
    return render_template(
        "sup_parameters.html",
        token_configured=bool(_configured_token()),
        sup_admin_ui_config=build_sup_admin_ui_config(
            schedule_manager_metadata=get_va_schedule_manager_metadata(),
        ),
    )


@sup_parameters_bp.get("/sup-parameters/data")
def sup_parameters_data():
    if not _require_token():
        return _token_error()
    return jsonify(get_sup_parameters_data())


@sup_parameters_bp.post("/sup-parameters/save")
def sup_parameters_save():
    if not _require_token():
        return _token_error()

    payload = request.get_json(silent=True) or {}
    try:
        data = save_sup_parameters(
            payload.get("config"),
            str(payload.get("revision") or "").strip(),
        )
        return jsonify(data)
    except SupParametersConflictError as exc:
        return jsonify({"success": False, "error": str(exc), "conflict": True}), 409
    except SupParametersValidationError as exc:
        return jsonify({"success": False, "error": "Ошибки валидации", "errors": exc.errors}), 400
    except Exception as exc:
        return jsonify(
            {
                "success": False,
                "error": f"Не удалось сохранить СУП-параметры: {type(exc).__name__}",
            }
        ), 500


@sup_parameters_bp.get("/sup-parameters/release-monitor-refresh")
def release_monitor_refresh_data():
    auth_error = require_sup_admin_request()
    if auth_error is not None:
        return auth_error
    try:
        return jsonify(get_release_monitor_refresh_admin_status())
    except Exception:
        logging.exception("Failed to read release monitor refresh status")
        return jsonify(
            {
                "success": False,
                "error": "Не удалось получить статус обновления Блока релизов.",
            }
        ), 500


@sup_parameters_bp.post("/sup-parameters/release-monitor-refresh/start")
def release_monitor_refresh_start():
    auth_error = require_sup_admin_request()
    if auth_error is not None:
        return auth_error
    csrf_error = csrf_protect_request()
    if csrf_error is not None:
        return csrf_error
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict) or set(payload) != {"mode"}:
        return jsonify(
            {"success": False, "error": "Поддерживается только параметр mode."}
        ), 400
    mode = str(payload.get("mode") or "").strip().lower()
    if mode not in {"quick", "full", "reliable_full"}:
        return jsonify(
            {"success": False, "error": "Неизвестный режим обновления."}
        ), 400
    try:
        result = start_release_monitor_refresh(mode=mode, trigger="manual")
        if not result.get("started"):
            return jsonify(
                {
                    "success": False,
                    "error": "Обновление Блока релизов уже выполняется.",
                    "conflict": True,
                    "refresh": result.get("status") or {},
                }
            ), 409
        return jsonify(
            {
                "success": True,
                "started": True,
                "refresh": result.get("status") or {},
            }
        ), 202
    except Exception:
        logging.exception("Failed to start release monitor refresh")
        return jsonify(
            {
                "success": False,
                "error": "Не удалось запустить обновление Блока релизов.",
            }
        ), 500


@sup_parameters_bp.get("/sup-parameters/employee-directory")
def employee_directory_data():
    auth_error = require_sup_admin_request()
    if auth_error is not None:
        return auth_error
    data = get_employee_directory_admin_data()
    health = get_consumer_health(
        load_employee_directory_context(),
        feature_flags=get_feature_flags(),
    )
    data["directory_health"] = health["directory"]
    data["consumer_health"] = health["consumer_health"]
    return jsonify(data)


@sup_parameters_bp.post("/sup-parameters/employee-directory/save")
def employee_directory_save():
    auth_error = require_sup_admin_request()
    if auth_error is not None:
        return auth_error
    csrf_error = csrf_protect_request()
    if csrf_error is not None:
        return csrf_error
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"success": False, "error": "JSON object required."}), 400
    allowed_keys = {"expected_revision", "expected_etag", "employees"}
    if set(payload) != allowed_keys:
        return jsonify(
            {"success": False, "error": "Unsupported employee directory payload."}
        ), 400
    try:
        data = save_employee_directory(
            payload.get("employees"),
            expected_revision=payload.get("expected_revision"),
            expected_etag=str(payload.get("expected_etag") or ""),
            operational_validator=_validate_directory_operational,
        )
        health = get_consumer_health(
            load_employee_directory_context(),
            feature_flags=get_feature_flags(),
        )
        data["directory_health"] = health["directory"]
        data["consumer_health"] = health["consumer_health"]
        return jsonify(data)
    except EmployeeDirectoryConflictError:
        return jsonify(
            {
                "success": False,
                "error": "Employee directory was changed by another process.",
                "conflict": True,
            }
        ), 409
    except EmployeeDirectoryValidationError as exc:
        return jsonify(
            {
                "success": False,
                "error": "Не удалось сохранить справочник сотрудников.",
                "errors": exc.errors,
            }
        ), 400
    except EmployeeDirectoryStateError as exc:
        return jsonify({"success": False, "error": str(exc)}), 409
    except Exception as exc:
        return jsonify(
            {
                "success": False,
                "error": f"Employee directory save failed: {type(exc).__name__}",
            }
        ), 500


@sup_parameters_bp.get("/sup-parameters/va-schedule-manager")
def va_schedule_manager_admin_data():
    auth_error = require_sup_admin_request()
    if auth_error is not None:
        return auth_error
    try:
        return jsonify(build_va_schedule_manager_admin_data())
    except Exception as exc:
        return jsonify(
            {
                "success": False,
                "error": (
                    "VA Schedule Manager settings load failed: "
                    f"{type(exc).__name__}"
                ),
            }
        ), 500


@sup_parameters_bp.put(
    "/sup-parameters/va-schedule-manager/employees/<employee_id>/settings"
)
def va_employee_settings_save(employee_id: str):
    auth_error = require_sup_admin_request()
    if auth_error is not None:
        return auth_error
    csrf_error = csrf_protect_request()
    if csrf_error is not None:
        return csrf_error
    payload = request.get_json(silent=True)
    allowed_keys = {
        "directory_etag",
        "settings_revision",
        "settings_etag",
        "settings",
    }
    if not isinstance(payload, dict) or set(payload) != allowed_keys:
        return jsonify(
            {
                "success": False,
                "error": "Unsupported VA employee settings payload.",
            }
        ), 400
    values = payload.get("settings")
    if not isinstance(values, dict) or set(values) != {
        "status",
        "role",
        "competencies",
        "overtime_ready",
    }:
        return jsonify(
            {
                "success": False,
                "error": "Unsupported VA employee settings fields.",
            }
        ), 400
    try:
        EmployeeSettingsRepository().save_employee_settings(
            employee_id,
            values,
            expected_revision=payload.get("settings_revision"),
            expected_etag=str(payload.get("settings_etag") or ""),
            expected_directory_etag=str(
                payload.get("directory_etag") or ""
            ),
        )
        return jsonify(build_va_schedule_manager_admin_data())
    except EmployeeSettingsConflictError as exc:
        return jsonify(
            {
                "success": False,
                "error": str(exc),
                "conflict": True,
            }
        ), 409
    except EmployeeSettingsValidationError as exc:
        return jsonify({"success": False, "error": str(exc)}), 400
    except Exception as exc:
        return jsonify(
            {
                "success": False,
                "error": (
                    "VA employee settings save failed: "
                    f"{type(exc).__name__}"
                ),
            }
        ), 500


@sup_parameters_bp.post(
    "/sup-parameters/va-schedule-manager/competencies"
)
def va_competency_add():
    return _mutate_va_competency("add")


@sup_parameters_bp.patch(
    "/sup-parameters/va-schedule-manager/competencies/<code>"
)
def va_competency_update(code: str):
    return _mutate_va_competency("update", code)


@sup_parameters_bp.delete(
    "/sup-parameters/va-schedule-manager/competencies/<code>"
)
def va_competency_delete(code: str):
    return _mutate_va_competency("delete", code)


def _mutate_va_competency(operation: str, code: str = ""):
    auth_error = require_sup_admin_request()
    if auth_error is not None:
        return auth_error
    csrf_error = csrf_protect_request()
    if csrf_error is not None:
        return csrf_error
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify(
            {"success": False, "error": "JSON object required."}
        ), 400
    expected_keys = (
        {"expected_etag"}
        if operation == "delete"
        else {"expected_etag", "competency"}
    )
    if set(payload) != expected_keys:
        return jsonify(
            {
                "success": False,
                "error": "Unsupported competency payload.",
            }
        ), 400
    competency = payload.get("competency")
    if operation != "delete" and (
        not isinstance(competency, dict)
        or set(competency) != {"code", "name", "description"}
    ):
        return jsonify(
            {
                "success": False,
                "error": "Unsupported competency fields.",
            }
        ), 400
    try:
        service = _va_competency_service()
        expected_etag = str(payload.get("expected_etag") or "")
        if operation == "add":
            result = service.add_competency(
                competency,
                expected_etag=expected_etag,
            )
        elif operation == "update":
            result = service.update_competency(
                code,
                competency,
                expected_etag=expected_etag,
            )
        else:
            result = service.delete_competency(
                code,
                expected_etag=expected_etag,
            )
        return jsonify({"success": True, "competencies": result})
    except CompetencyConflictError as exc:
        return jsonify(
            {
                "success": False,
                "error": str(exc),
                "conflict": True,
            }
        ), 409
    except (CompetencyValidationError, CompetencyInUseError) as exc:
        return jsonify({"success": False, "error": str(exc)}), 400
    except Exception as exc:
        return jsonify(
            {
                "success": False,
                "error": (
                    "Competency update failed: "
                    f"{type(exc).__name__}"
                ),
            }
        ), 500
