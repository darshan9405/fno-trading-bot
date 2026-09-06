#!/usr/bin/env python3
"""Live Upstox integration matrix (production API).

Validates the UpstoxBroker against the real Upstox API. Prints a pass/fail
matrix and exits non-zero on any failure.

Token: set UPSTOX_INTEGRATION_TOKEN (Upstox access tokens expire, so the
integration check always takes an explicit token).

Usage:
    UPSTOX_INTEGRATION_TOKEN=<token> python scripts/upstox_integration.py
    UPSTOX_INTEGRATION_TOKEN=<token> UPSTOX_LIVE_ORDER=1 python scripts/upstox_integration.py  # + real order
"""

import os
import socket

socket.setdefaulttimeout(20)  # bound every HTTP request
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.broker import UpstoxBroker  # noqa: E402
from app.broker.base import BrokerError, ModifyOrderParams, OrderRequest  # noqa: E402
from app.config import Config  # noqa: E402


def probe(fn):
    try:
        return fn(), None
    except BrokerError as e:
        return None, e


def _is_empty(res) -> bool:
    if res is None:
        return True
    if hasattr(res, "empty"):  # pandas DataFrame
        return res.empty
    return res in ([], {}, "")


def main() -> int:
    token = os.getenv("UPSTOX_INTEGRATION_TOKEN")
    if not token:
        print("FATAL: set UPSTOX_INTEGRATION_TOKEN to run the integration check.")
        return 2
    broker = UpstoxBroker(Config(), access_token=token)

    print("Upstox integration check — PRODUCTION API")
    print("=" * 78)
    results = []

    def check(name, fn, note_ok="ok"):
        res, err = probe(fn)
        ok = err is None and not _is_empty(res)
        results.append((name, ok, f"{err.api_status}: {err.api_message}" if err else note_ok))

    # auth
    results.append(("token configured", bool(broker._token), ""))
    try:
        UpstoxBroker(Config(), access_token="garbage-token").search_instruments("NIFTY")
        results.append(("bad token -> 401", False, "no error raised"))
    except BrokerError as e:
        results.append(("bad token -> 401", e.api_status == 401, f"api_status={e.api_status}"))

    # instruments / market data
    check("search instruments", lambda: broker.search_instruments("NIFTY"))
    check("historical candles", lambda: broker.get_historical_candles(
        "NSE_INDEX|Nifty 50", "day", date.today() - timedelta(days=61), date.today() - timedelta(days=1)))
    check("ltp", lambda: broker.get_ltp(["NSE_INDEX|Nifty 50"]))
    expiries, e_err = probe(lambda: broker.get_expiries("NSE_INDEX|Nifty 50"))
    results.append(("expiries", e_err is None and bool(expiries), f"{e_err.api_status}: {e_err.api_message}" if e_err else "ok"))
    if expiries:
        check("option contracts", lambda: broker.get_option_contracts("NSE_INDEX|Nifty 50", expiry=expiries[0]))
    else:
        results.append(("option contracts", False, "no expiries"))

    # portfolio
    check("positions", lambda: broker.get_positions())
    check("funds", lambda: broker.get_funds())
    check("profile", lambda: broker.get_profile())
    order_book, ob_err = probe(broker.get_order_book)
    results.append(("order book", ob_err is None and isinstance(order_book, list),
                    f"{ob_err.api_status}: {ob_err.api_message}" if ob_err else "ok"))

    # order lifecycle (REAL ORDER)
    if os.getenv("UPSTOX_LIVE_ORDER", "0") == "1":
        try:
            contracts = [i for i in broker.search_instruments("NIFTY 25000 CE") if i.instrument_type == "CE"]
            c = contracts[0]
            qty = c.lot_size or 1
            oid = broker.place_order(OrderRequest(
                instrument_key=c.instrument_key, transaction_type="BUY", quantity=qty,
                product="I", order_type="LIMIT", price=1.0, tag="integration-script"))
            in_book = any(o.order_id == oid for o in broker.get_order_book())
            broker.modify_order(ModifyOrderParams(order_id=oid, quantity=qty, price=2.0,
                                                  order_type="LIMIT", trigger_price=0.0))
            broker.cancel_order(oid)
            after = {o.order_id: o.status for o in broker.get_order_book()}
            cancelled = after.get(oid, "cancelled") == "cancelled"
            results.append(("order lifecycle (place/modify/cancel)", in_book and cancelled,
                            f"order_id={oid}, in_book={in_book}, cancelled={cancelled}"))
        except Exception as e:  # noqa: BLE001
            results.append(("order lifecycle (place/modify/cancel)", False, str(e)))
    else:
        results.append(("order lifecycle (place/modify/cancel)", True, "SKIPPED (set UPSTOX_LIVE_ORDER=1)"))

    width = 44
    print(f"{'Check':<{width}} {'Result':<8} Note")
    print("-" * 78)
    all_ok = True
    for name, ok, note in results:
        all_ok = all_ok and ok
        print(f"{name:<{width}} {'PASS' if ok else 'FAIL':<8} {note}")

    print("=" * 78)
    passed = sum(1 for _, ok, _ in results if ok)
    print(f"{passed}/{len(results)} checks passed ({'OK' if all_ok else 'FAILED'})")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())