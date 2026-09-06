"""Authentication: Upstox SSO + short-lived JWT (daily-rotating key) + refresh.

Mechanism (per requirement):
- Browser hits the UI -> UI asks /api/auth/status -> no valid cookie -> SSO.
- Backend exchanges the Upstox auth code, then sets two HttpOnly cookies:
  a short-lived JWT access token and an opaque refresh token.
- The UI may also authenticate with `Authorization: Bearer <jwt>` (Streamlit
  calls the API server-side, where browser cookies are not available).
- The JWT is signed with HMAC(JWT_MASTER_SECRET, today) so the key rotates
  daily; yesterday's tokens are rejected. Refresh tokens are opaque, stored
  hashed, and rotated on use.
"""

import base64
import hashlib
import hmac
import json
import secrets
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import jwt
from cryptography.fernet import Fernet, InvalidToken
from flask import jsonify, request
from sqlalchemy import select

from app.config import Config
from app.db import session_scope
from app.models import AuthToken

ACCESS_TOKEN_COOKIE = "upstox_at"
REFRESH_TOKEN_COOKIE = "upstox_rt"

IST = ZoneInfo("Asia/Kolkata")
# Upstox access tokens expire at 3:30 AM IST the following day.
UPSTOX_TOKEN_RESET_IST = time(3, 30)

_config: Config | None = None


def configure(config: Config) -> None:
    global _config
    _config = config


def _cfg() -> Config:
    return _config or Config()


def _utcnow() -> datetime:
    # naive UTC for consistent DB (SQLite) comparisons and PyJWT encoding
    return datetime.now(timezone.utc).replace(tzinfo=None)


# --- daily-rotating JWT --------------------------------------------------


def daily_key(day: str | None = None) -> bytes:
    cfg = _cfg()
    day = day or _utcnow().date().isoformat()
    return hmac.new(cfg.JWT_MASTER_SECRET.encode(), day.encode(), hashlib.sha256).digest()


def issue_access_jwt(user_id: str, day: str | None = None) -> str:
    cfg = _cfg()
    now = _utcnow()
    payload = {
        "sub": user_id,
        "day": day or now.date().isoformat(),
        "iat": now,
        "exp": now + timedelta(minutes=cfg.JWT_ACCESS_TTL_MINUTES),
    }
    return jwt.encode(payload, daily_key(day), algorithm="HS256")


def verify_access_jwt(token: str | None) -> dict | None:
    if not token:
        return None
    try:
        payload = jwt.decode(
            token,
            daily_key(),
            algorithms=["HS256"],
            options={"require": ["exp", "day"]},
        )
    except (jwt.InvalidTokenError, ValueError):
        return None
    if payload.get("day") != _utcnow().date().isoformat():
        return None
    return payload


def request_token() -> str | None:
    """Token from `Authorization: Bearer` header or the access cookie."""
    header = request.headers.get("Authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip() or None
    return request.cookies.get(ACCESS_TOKEN_COOKIE)


def current_user_id() -> str | None:
    payload = verify_access_jwt(request_token())
    return payload.get("sub") if payload else None


# --- refresh tokens ------------------------------------------------------


def generate_refresh_token() -> str:
    return secrets.token_urlsafe(48)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def save_refresh_token(token: str, user_id: str) -> None:
    cfg = _cfg()
    with session_scope() as session:
        session.add(
            AuthToken(
                token_type="refresh",
                token_hash=hash_token(token),
                user_id=user_id,
                expires_at=_utcnow() + timedelta(days=cfg.JWT_REFRESH_TTL_DAYS),
                revoked=False,
            )
        )


def find_valid_refresh(token: str | None) -> AuthToken | None:
    if not token:
        return None
    with session_scope() as session:
        row = session.execute(
            select(AuthToken).where(
                AuthToken.token_type == "refresh",
                AuthToken.token_hash == hash_token(token),
                AuthToken.revoked.is_(False),
            )
        ).scalar_one_or_none()
        if row is not None and row.expires_at > _utcnow():
            return row
    return None


def revoke_refresh(token: str) -> None:
    with session_scope() as session:
        row = session.execute(
            select(AuthToken).where(
                AuthToken.token_type == "refresh",
                AuthToken.token_hash == hash_token(token),
            )
        ).scalar_one_or_none()
        if row is not None:
            row.revoked = True


def rotate_refresh(old_token: str, user_id: str) -> str:
    revoke_refresh(old_token)
    new_token = generate_refresh_token()
    save_refresh_token(new_token, user_id)
    return new_token


# --- one-time bootstrap code (UI hand-off after SSO) ---------------------


def create_bootstrap_code(user_id: str) -> str:
    code = secrets.token_urlsafe(24)
    with session_scope() as session:
        session.add(
            AuthToken(
                token_type="bootstrap",
                token_hash=hash_token(code),
                user_id=user_id,
                expires_at=_utcnow() + timedelta(minutes=1),
                revoked=False,
            )
        )
    return code


def redeem_bootstrap_code(code: str) -> str | None:
    with session_scope() as session:
        row = session.execute(
            select(AuthToken).where(
                AuthToken.token_type == "bootstrap",
                AuthToken.token_hash == hash_token(code),
                AuthToken.revoked.is_(False),
            )
        ).scalar_one_or_none()
        if row is None or row.expires_at <= _utcnow():
            return None
        row.revoked = True  # one-time use
        return row.user_id


# --- Upstox access token at rest (Fernet-encrypted) ----------------------


def _fernet() -> Fernet:
    key = hashlib.sha256(_cfg().SECRET_KEY.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def encrypt_upstox_token(token: str) -> str:
    return _fernet().encrypt(token.encode()).decode()


def decrypt_upstox_token(enc: str) -> str | None:
    try:
        return _fernet().decrypt(enc.encode()).decode()
    except (InvalidToken, ValueError):
        return None


def upstox_token_expiry(token: str) -> datetime | None:
    """Real expiry of an Upstox access token from its JWT `exp` claim (naive UTC).

    Upstox gateway tokens carry an `exp` (~30 days for console-generated tokens).
    Returns None if it cannot be read.
    """
    try:
        payload = token.split(".")[1]
        payload = payload.rstrip("=") + "=" * (-len(payload.rstrip("=")) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload))
        exp = data.get("exp")
        if exp:
            return datetime.fromtimestamp(int(exp), tz=timezone.utc).replace(tzinfo=None)
    except Exception:
        pass
    return None


def _daily_0330_expiry(now: datetime | None = None) -> datetime:
    """Fallback (SSO-style tokens): expires at the next 3:30 AM IST."""
    now = now or _utcnow()
    now_ist = now.replace(tzinfo=timezone.utc).astimezone(IST)
    if now_ist.time() < UPSTOX_TOKEN_RESET_IST:
        expiry_ist = now_ist.replace(hour=3, minute=30, second=0, microsecond=0)
    else:
        expiry_ist = (now_ist + timedelta(days=1)).replace(hour=3, minute=30, second=0, microsecond=0)
    return expiry_ist.astimezone(timezone.utc).replace(tzinfo=None)


class UpstoxTokenStore:
    """Module-level holder for the raw Upstox access token, persisted
    Fernet-encrypted in `auth_tokens` so it survives restarts."""

    _token: str | None = None
    _loaded = False

    @classmethod
    def set(cls, token: str) -> None:
        cls._token = token
        expires_at = upstox_token_expiry(token) or _daily_0330_expiry()
        with session_scope() as session:
            row = session.execute(
                select(AuthToken).where(AuthToken.token_type == "upstox")
            ).scalar_one_or_none()
            if row is None:
                session.add(
                    AuthToken(
                        token_type="upstox",
                        token_hash=encrypt_upstox_token(token),
                        user_id="system",
                        expires_at=expires_at,
                        revoked=False,
                    )
                )
            else:
                row.token_hash = encrypt_upstox_token(token)
                row.expires_at = expires_at

    @classmethod
    def get_expiry(cls) -> datetime | None:
        """Naive-UTC expiry of the stored Upstox token, or None."""
        with session_scope() as session:
            row = session.execute(
                select(AuthToken).where(AuthToken.token_type == "upstox")
            ).scalar_one_or_none()
            return row.expires_at if row is not None else None

    @classmethod
    def get(cls) -> str | None:
        cls._ensure_loaded()
        return cls._token

    @classmethod
    def clear(cls) -> None:
        cls._token = None
        cls._loaded = True
        with session_scope() as session:
            row = session.execute(
                select(AuthToken).where(AuthToken.token_type == "upstox")
            ).scalar_one_or_none()
            if row is not None:
                session.delete(row)

    @classmethod
    def _ensure_loaded(cls) -> None:
        if cls._loaded:
            return
        cls._loaded = True
        with session_scope() as session:
            row = session.execute(
                select(AuthToken).where(AuthToken.token_type == "upstox")
            ).scalar_one_or_none()
            if row is not None:
                cls._token = decrypt_upstox_token(row.token_hash)


# --- Flask decorator -----------------------------------------------------


def jwt_required(fn):
    from functools import wraps

    @wraps(fn)
    def wrapper(*args, **kwargs):
        payload = verify_access_jwt(request_token())
        if payload is None:
            return (
                jsonify(
                    {
                        "status": "error",
                        "error": {"code": "unauthorized", "message": "Missing or invalid access token."},
                    }
                ),
                401,
            )
        request.auth_user_id = payload["sub"]
        return fn(*args, **kwargs)

    return wrapper