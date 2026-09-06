"""Shared API response helpers (unified envelope)."""

from flask import jsonify


def ok(data=None):
    return jsonify({"status": "ok", "data": data or {}})


def error(code: str, message: str, status: int = 400):
    return jsonify({"status": "error", "error": {"code": code, "message": message}}), status


def broker_error(exc) -> tuple:
    code = "broker_error"
    if getattr(exc, "api_status", None) == 401:
        code = "upstox_unauthorized"
    return error(code, str(getattr(exc, "message", exc)), 502)