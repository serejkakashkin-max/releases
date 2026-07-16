from typing import Optional

from flask import jsonify


def api_success(data: Optional[dict] = None, status: int = 200, meta: Optional[dict] = None):
    payload = {
        "ok": True,
        "data": data or {},
        "error": None,
        "meta": meta or {},
    }
    return jsonify(payload), status


def api_error(code: str, message: str, status: int = 400, details: Optional[dict] = None):
    payload = {
        "ok": False,
        "data": None,
        "error": {
            "code": code,
            "message": message,
            "details": details or {},
        },
        "meta": {},
    }
    return jsonify(payload), status
