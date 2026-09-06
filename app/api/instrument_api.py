"""Instrument whitelist API: list the universe + enable/disable underlyings."""

from flask import Blueprint, request

from app.auth import jwt_required
from app.services import instrument_service
from app.api.common import error, ok

bp = Blueprint("instrument", __name__, url_prefix="/api/instruments")


@bp.get("")
@jwt_required
def list_instruments():
    rows = instrument_service.list_instruments()
    return ok({"count": len(rows), "instruments": rows})


@bp.put("/<int:instrument_id>")
@jwt_required
def set_instrument(instrument_id: int):
    body = request.get_json(silent=True) or {}
    if "enabled" not in body or not isinstance(body["enabled"], bool):
        return error("bad_request", "Provide {'enabled': true|false}.")
    row = instrument_service.set_instrument_enabled(instrument_id, body["enabled"])
    if row is None:
        return error("not_found", f"No instrument with id {instrument_id}.", 404)
    return ok(row)