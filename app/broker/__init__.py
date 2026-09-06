"""Broker package: factory returns the real Upstox integration."""

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


def get_broker(config: Config | None = None) -> UpstoxBroker:
    """Build a broker with the stored Upstox access token (from SSO), if any."""
    from app.auth import UpstoxTokenStore

    config = config or Config()
    return UpstoxBroker(config, access_token=UpstoxTokenStore.get())


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
]