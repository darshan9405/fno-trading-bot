"""Authentication API: SSO, JWT cookies, refresh, logout."""

import logging
from urllib.parse import quote

from flask import Blueprint, jsonify, redirect, request
from upstox_client.rest import ApiException

from app.auth import (
    ACCESS_TOKEN_COOKIE,
    REFRESH_TOKEN_COOKIE,
    USER_NOT_ALLOWED_CODE,
    UpstoxTokenStore,
    _cfg,
    assert_allowed_user,
    create_bootstrap_code,
    find_valid_refresh,
    generate_refresh_token,
    issue_access_jwt,
    redeem_bootstrap_code,
    request_token,
    revoke_refresh,
    rotate_refresh,
    save_refresh_token,
    verify_access_jwt,
)
from app.broker import get_broker
from app.extensions import limiter

log = logging.getLogger(__name__)

bp = Blueprint("auth", __name__, url_prefix="/api/auth")


def _set_cookies(response, config, access_jwt, refresh_token):
    response.set_cookie(
        ACCESS_TOKEN_COOKIE,
        access_jwt,
        httponly=True,
        samesite="Lax",
        secure=config.COOKIE_SECURE,
        max_age=config.JWT_ACCESS_TTL_MINUTES * 60,
        path="/",
    )
    response.set_cookie(
        REFRESH_TOKEN_COOKIE,
        refresh_token,
        httponly=True,
        samesite="Lax",
        secure=config.COOKIE_SECURE,
        max_age=config.JWT_REFRESH_TTL_DAYS * 86400,
        path="/api/auth",
    )
    return response


def _clear_cookies(response, config):
    response.delete_cookie(ACCESS_TOKEN_COOKIE, path="/")
    response.delete_cookie(REFRESH_TOKEN_COOKIE, path="/api/auth")
    return response


def _request_refresh_token(config) -> str | None:
    # Explicit header (UI) wins; fall back to the HttpOnly cookie (browser).
    return request.headers.get("X-Refresh-Token") or request.cookies.get(REFRESH_TOKEN_COOKIE)


@bp.get("/status")
def status():
    config = _cfg()
    payload = verify_access_jwt(request_token())
    if payload:
        return jsonify({"status": "ok", "data": {"authenticated": True, "user_id": payload["sub"]}})
    return jsonify({"status": "ok", "data": {"authenticated": False}})


@bp.get("/upstox/login")
@limiter.limit("20 per minute")
def upstox_login():
    config = _cfg()
    if not config.UPSTOX_CLIENT_ID or not config.UPSTOX_REDIRECT_URI:
        return jsonify({"status": "error", "error": {"code": "config", "message": "Upstox SSO not configured."}}), 500
    url = (
        f"{config.UPSTOX_API_BASE}/v2/login/authorization/dialog?client_id={quote(config.UPSTOX_CLIENT_ID)}"
        f"&redirect_uri={quote(config.UPSTOX_REDIRECT_URI)}&response_type=code"
    )
    return redirect(url)


@bp.get("/upstox/callback")
def upstox_callback():
    config = _cfg()
    code = request.args.get("code")
    error = request.args.get("error")
    if error:
        return redirect(f"{config.FRONTEND_URL}?auth_error={quote(error)}")
    if not code:
        return jsonify({"status": "error", "error": {"code": "bad_request", "message": "Missing auth code."}}), 400

    import upstox_client

    try:
        from app.broker.upstox_broker import make_configuration

        resp = upstox_client.LoginApi(upstox_client.ApiClient(make_configuration(config))).token(
            config.UPSTOX_API_VERSION,
            code=code,
            client_id=config.UPSTOX_CLIENT_ID,
            client_secret=config.UPSTOX_CLIENT_SECRET,
            redirect_uri=config.UPSTOX_REDIRECT_URI,
            grant_type="authorization_code",
        )
    except ApiException as e:
        log.error("SSO token exchange failed: %s", e)
        return redirect(f"{config.FRONTEND_URL}?auth_error={quote('token_exchange_failed')}")

    access_token = getattr(resp, "access_token", None)
    if not access_token:
        return redirect(f"{config.FRONTEND_URL}?auth_error={quote('no_access_token')}")

    user_id = getattr(resp, "user_id", None) or "single-user"

    try:
        assert_allowed_user(user_id)
    except Exception:
        log.warning("SSO rejected: user_id=%s is not in the allowlist", user_id)
        return (
            jsonify(
                {
                    "status": "error",
                    "error": {
                        "code": USER_NOT_ALLOWED_CODE,
                        "message": "This account is not permitted to access the trading bot.",
                    },
                }
            ),
            403,
        )

    UpstoxTokenStore.set(access_token)
    try:
        get_broker(config).set_access_token(access_token)
    except Exception as e:  # broker rebuild should not break login
        log.warning("broker token injection failed: %s", e)

    refresh_token = generate_refresh_token()
    save_refresh_token(refresh_token, user_id)
    access_jwt = issue_access_jwt(user_id)
    bootstrap = create_bootstrap_code(user_id)

    response = redirect(f"{config.FRONTEND_URL}?bootstrap={quote(bootstrap)}")
    return _set_cookies(response, config, access_jwt, refresh_token)


@bp.post("/bootstrap")
@limiter.limit("5 per minute")
def bootstrap():
    """One-time exchange for tokens; UI stores them in session_state."""
    config = _cfg()
    code = (request.get_json(silent=True) or {}).get("code") or request.form.get("code")
    user_id = redeem_bootstrap_code(code or "")
    if not user_id:
        return jsonify({"status": "error", "error": {"code": "invalid_code", "message": "Bootstrap code invalid or expired."}}), 401
    refresh_token = generate_refresh_token()
    save_refresh_token(refresh_token, user_id)
    access_jwt = issue_access_jwt(user_id)
    response = jsonify(
        {
            "status": "ok",
            "data": {
                "access_token": access_jwt,
                "refresh_token": refresh_token,
                "user_id": user_id,
                "expires_in": config.JWT_ACCESS_TTL_MINUTES * 60,
            },
        }
    )
    return _set_cookies(response, config, access_jwt, refresh_token)


@bp.post("/refresh")
@limiter.limit("10 per minute")
def refresh():
    config = _cfg()
    old_refresh = _request_refresh_token(config)
    row = find_valid_refresh(old_refresh)
    if row is None:
        return jsonify({"status": "error", "error": {"code": "refresh_invalid", "message": "Refresh token invalid or expired."}}), 401

    try:
        assert_allowed_user(row.user_id)
    except Exception:
        log.warning("refresh rejected: stored user_id=%s is not in the allowlist", row.user_id)
        if old_refresh:
            revoke_refresh(old_refresh)
        return (
            jsonify(
                {
                    "status": "error",
                    "error": {
                        "code": USER_NOT_ALLOWED_CODE,
                        "message": "This account is not permitted to access the trading bot.",
                    },
                }
            ),
            403,
        )

    new_refresh = rotate_refresh(old_refresh, row.user_id)
    access_jwt = issue_access_jwt(row.user_id)
    response = jsonify(
        {
            "status": "ok",
            "data": {
                "access_token": access_jwt,
                "refresh_token": new_refresh,
                "expires_in": config.JWT_ACCESS_TTL_MINUTES * 60,
            },
        }
    )
    return _set_cookies(response, config, access_jwt, new_refresh)


@bp.get("/logout")
@bp.post("/logout")
@limiter.limit("10 per minute")
def logout():
    config = _cfg()
    refresh = _request_refresh_token(config)
    if refresh:
        from app.auth import revoke_refresh

        revoke_refresh(refresh)
    UpstoxTokenStore.clear()
    if request.method == "GET":
        response = redirect(config.FRONTEND_URL)
    else:
        response = jsonify({"status": "ok", "data": {}})
    return _clear_cookies(response, config)