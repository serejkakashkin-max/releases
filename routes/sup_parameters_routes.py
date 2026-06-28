from flask import Blueprint, jsonify, render_template, request

from config import TOKENS
from services.sup_parameters_service import (
    SupParametersConflictError,
    SupParametersValidationError,
    get_sup_parameters_data,
    save_sup_parameters,
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
