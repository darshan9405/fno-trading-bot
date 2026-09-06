"""Runtime config API (Stage 7): read/update settings."""

from flask import Blueprint, request

from app.auth import jwt_required
from app.settings import DEFAULT_SETTINGS, get_setting, set_setting
from app.api.common import error, ok

bp = Blueprint("config", __name__, url_prefix="/api/config")


@bp.get("")
@jwt_required
def get_config():
    data = {key: get_setting(key) for key in DEFAULT_SETTINGS}
    return ok(data)


@bp.put("")
@jwt_required
def update_config():
    body = request.get_json(silent=True) or {}
    if not isinstance(body, dict) or not body:
        return error("bad_request", "Provide a JSON object of settings to update.")
    unknown = [k for k in body if k not in DEFAULT_SETTINGS]
    if unknown:
        return error("bad_request", f"Unknown settings: {', '.join(unknown)}")
    for key, value in body.items():
        set_setting(key, value)
    return ok({"updated": list(body.keys())})