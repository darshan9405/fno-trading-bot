"""Broker package: factory returns the real Upstox integration.

A single broker instance is reused across requests within a process, so
TTLCache wrappings inside :class:`UpstoxBroker` (LTP / positions / funds /
order book / profile) survive between calls. This matches the deployment
shape (single gunicorn worker; see Dockerfile.backend / deploy/entrypoint.sh).

Tests that want isolation should call :func:`reset_broker_singleton` between
cases — see ``tests/test_broker.py``.
"""

import threading
from typing import Optional

from app.broker.base import (
    BrokerBase,
    BrokerError,
    ExchangeTimingView,
    FillView,
    FundsView,
    HolidayView,
    InstrumentView,
    ModifyOrderParams,
    OrderRequest,
    OrderView,
    PositionView,
    ProfileView,
)
from app.broker.upstox_broker import UpstoxBroker
from app.config import Config

_BROKER_LOCK = threading.Lock()
_BROKER: Optional[UpstoxBroker] = None
_BROKER_TOKEN: Optional[str] = None


def _build_broker(config: Optional[Config], access_token: Optional[str]) -> UpstoxBroker:
    cfg = config or Config()
    # ``UpstoxTokenStore.get`` is imported lazily to avoid circular imports
    # at process start (auth.py also imports from app.broker.base).
    from app.auth import UpstoxTokenStore

    token = access_token if access_token is not None else UpstoxTokenStore.get()
    return UpstoxBroker(cfg, access_token=token)


def get_broker(config: Optional[Config] = None, access_token: Optional[str] = None) -> UpstoxBroker:
    """Return the process-wide broker singleton.

    If ``access_token`` is supplied explicitly, the singleton is rebuilt so
    its read-side caches (profile, funds, LTP, order book) are scoped to the
    new account context — see :meth:`UpstoxBroker.set_access_token`.
    """
    global _BROKER, _BROKER_TOKEN

    explicit_token = access_token is not None
    token = access_token if explicit_token else None  # token-store lookup deferred

    with _BROKER_LOCK:
        if _BROKER is None:
            _BROKER = _build_broker(config, access_token)
            _BROKER_TOKEN = _BROKER._token
            return _BROKER

        if explicit_token and token != _BROKER_TOKEN:
            _BROKER.set_access_token(token or "")
            _BROKER_TOKEN = token or ""
        return _BROKER


def reset_broker_singleton() -> None:
    """Drop the cached broker. Tests use this to keep cases isolated."""
    global _BROKER, _BROKER_TOKEN
    with _BROKER_LOCK:
        _BROKER = None
        _BROKER_TOKEN = None


__all__ = [
    "BrokerBase",
    "BrokerError",
    "ExchangeTimingView",
    "FillView",
    "FundsView",
    "HolidayView",
    "InstrumentView",
    "ModifyOrderParams",
    "OrderRequest",
    "OrderView",
    "PositionView",
    "ProfileView",
    "UpstoxBroker",
    "get_broker",
    "reset_broker_singleton",
]
