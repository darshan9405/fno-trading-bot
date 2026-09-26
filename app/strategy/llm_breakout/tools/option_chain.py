"""`option_chain_summary` tool — at-the-money OI/IV context for the underlying.

Wraps the existing `contract_service` to give the LLM a quick read on:
  - At-the-money CE/PE premiums
  - Put-Call ratio (OI basis)
  - Max-pain strike (highest total OI)
  - Total CE / PE open interest
  - IV skew proxy: (CE_IV - PE_IV) at the ATM

This is a single-broker-call read; we cap the depth at 5 strikes either
side of ATM so the response stays small. The LLM uses this as supporting
context for the breakout verdict (e.g. an above-average PCR with rising
CE OI strengthens a CALL breakout).
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

from app.strategy.llm_breakout.tools.base import schema

log = logging.getLogger(__name__)


class OptionChainSummaryTool:
    name = "option_chain_summary"
    description = (
        "Fetch a snapshot of at-the-money option-chain context for the "
        "underlying: ATM CE/PE premiums, put-call ratio, max-pain strike, "
        "total CE/PE open interest, and an IV skew proxy. Use this as "
        "supporting context — option-chain context on its own is not a "
        "trigger; combine with the price breakout evidence from the candle "
        "data + indicators. Requires the broker to be reachable (call only "
        "after the broader breakout evidence is already strong)."
    )
    parameters = {
        "type": "object",
        "properties": {
            "underlying_key": {"type": "string",
                               "description": "Spot instrument key, e.g. NSE_EQ|INE002A01018."},
            "depth": {"type": "integer", "minimum": 1, "maximum": 10,
                      "description": "Strikes either side of ATM to include (default 3)."},
        },
        "required": ["underlying_key"],
        "additionalProperties": False,
    }

    def __init__(self, context: dict[str, Any]) -> None:
        self._broker = context.get("broker")
        self._today = context.get("today") or date.today()

    def run(self, args: dict[str, Any]) -> dict[str, Any]:
        if self._broker is None:
            return {"error": "broker not available in tool context"}
        underlying_key = str(args.get("underlying_key") or "").strip()
        depth = int(args.get("depth") or 3)
        if not underlying_key:
            return {"error": "underlying_key is required"}
        try:
            from app.services.contract_service import (
                next_expiry,
                option_chain_summary,
            )
        except Exception as e:  # noqa: BLE001
            return {"error": f"contract_service import failed: {e}"}
        try:
            expiry = next_expiry(self._broker, underlying_key, min_days=0, today=self._today)
            if expiry is None:
                return {"error": "no live expiry"}
            contracts = self._broker.get_option_contracts(underlying_key, expiry=expiry)
            spot = (self._broker.get_ltp([underlying_key]) or {}).get(underlying_key)
            if spot is None:
                return {"error": "no spot LTP"}
            return option_chain_summary(self._broker, contracts, spot, depth=depth)
        except Exception as e:  # noqa: BLE001
            log.warning("option_chain_summary: %s failed: %s", underlying_key, e)
            return {"error": f"option_chain failed: {e}"}

    @staticmethod
    def to_schema() -> dict[str, Any]:
        return schema(OptionChainSummaryTool.name, OptionChainSummaryTool.description,
                      OptionChainSummaryTool.parameters)