from flask import Blueprint, jsonify, render_template, request

from config import TOKENS
from services.sup_parameters_service import (
    SupParametersConflictError,
    SupParametersValidationError,
    get_employee_directory_consumer_modes_data,
    get_sup_parameters_data,
    save_employee_directory_consumer_modes,
    save_sup_parameters,
)
from services.employee_directory_repository import (
    EmployeeDirectoryConflictError,
    EmployeeDirectoryStateError,
    EmployeeDirectoryValidationError,
    get_employee_directory_admin_data,
    save_employee_directory,
)
from services.sup_admin_auth_service import csrf_protect_request, require_sup_admin_request


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


@sup_parameters_bp.get("/sup-parameters")
def sup_parameters_page():
    return render_template(
        "sup_parameters.html",
        token_configured=bool(_configured_token()),
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


@sup_parameters_bp.get("/sup-parameters/employee-directory")
def employee_directory_data():
    auth_error = require_sup_admin_request()
    if auth_error is not None:
        return auth_error
    data = get_employee_directory_admin_data()
    data.update(get_employee_directory_consumer_modes_data())
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
        )
        data.update(get_employee_directory_consumer_modes_data())
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
            {"success": False, "error": "Employee directory validation failed.", "errors": exc.errors}
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


@sup_parameters_bp.post("/sup-parameters/employee-directory/consumers")
def employee_directory_consumers_save():
    auth_error = require_sup_admin_request()
    if auth_error is not None:
        return auth_error
    csrf_error = csrf_protect_request()
    if csrf_error is not None:
        return csrf_error
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict) or set(payload) != {"expected_revision", "consumers"}:
        return jsonify({"success": False, "error": "Unsupported consumer modes payload."}), 400
    try:
        return jsonify(
            {
                "success": True,
                **save_employee_directory_consumer_modes(
                    payload.get("consumers"),
                    str(payload.get("expected_revision") or ""),
                ),
            }
        )
    except SupParametersConflictError:
        return jsonify(
            {
                "success": False,
                "error": "Feature flags were changed by another process.",
                "conflict": True,
            }
        ), 409
    except SupParametersValidationError as exc:
        return jsonify(
            {"success": False, "error": "Consumer modes validation failed.", "errors": exc.errors}
        ), 400
    except Exception as exc:
        return jsonify(
            {"success": False, "error": f"Consumer modes save failed: {type(exc).__name__}"}
        ), 500
