"""`breakout_calc` tool — quant helpers the LLM needs to make a defensible call.

Wraps the math-heavy parts of the breakout decision so the model can
delegate them and reason over the results. Pure functions; no network.

Implements:
  - swing_distance(price, swing_high, swing_low) — % distance from key
    swing points
  - breakout_strength(price, trigger, atr) — distance in ATR units (a
    >1.0 ATR break is meaningfully stronger than a 0.3 ATR break)
  - risk_reward(entry, stop, target) — R:R ratio
  - expected_value(win_rate, avg_win, avg_loss) — expectancy per trade
  - position_size(capital, risk_pct, entry, stop) — fixed-fractional lot
    sizing hint
  - volatility_percentile(atr, atr_history) — where current ATR sits
    vs the trailing distribution (compressed vol → breakout is more
    meaningful)
  - trend_strength(ema_fast, ema_slow) — % gap between EMAs
  - pullback_depth(close, swing_high, swing_low) — % pullback from the
    most recent swing extreme (helps confirm continuation vs exhaustion)

Returned dict shape is always JSON-safe; NaN/inf become None.
"""

from __future__ import annotations

import math
from typing import Any

from app.strategy.llm_breakout.tools.base import schema


def _safe(v: float | None) -> float | None:
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return f


class BreakoutCalcTool:
    name = "breakout_calc"
    description = (
        "Perform a single breakout-quantity computation by name. Pass `op` and "
        "the numeric arguments it requires. Available ops: "
        "`breakout_strength(price, trigger, atr)` — distance in ATR multiples; "
        "`risk_reward(entry, stop, target)` — R:R ratio; "
        "`expected_value(win_rate, avg_win, avg_loss)` — expectancy; "
        "`position_size(capital, risk_pct, entry, stop)` — fixed-fractional "
        "size hint in shares/lots; "
        "`volatility_percentile(current_atr, atr_series)` — rank of current "
        "ATR in [0,1] across the trailing window; "
        "`trend_strength(ema_fast, ema_slow)` — % gap; "
        "`pullback_depth(close, swing_high, swing_low)` — % retrace from "
        "the most recent swing extreme. "
        "Use this tool for any precise arithmetic that would otherwise be "
        "done by the LLM in its head."
    )
    parameters = {
        "type": "object",
        "properties": {
            "op": {
                "type": "string",
                "enum": [
                    "breakout_strength",
                    "risk_reward",
                    "expected_value",
                    "position_size",
                    "volatility_percentile",
                    "trend_strength",
                    "pullback_depth",
                ],
            },
            "args": {
                "type": "object",
                "description": "Numeric arguments required by the chosen op.",
                "additionalProperties": True,
            },
        },
        "required": ["op", "args"],
        "additionalProperties": False,
    }

    def __init__(self, context: dict[str, Any]) -> None:
        # Context optional; tool is mostly stateless.
        self._lot_size = int(context.get("lot_size") or 1)

    def run(self, args: dict[str, Any]) -> dict[str, Any]:
        op = str(args.get("op", "")).strip()
        a = dict(args.get("args") or {})
        try:
            if op == "breakout_strength":
                price = _safe(a.get("price"))
                trigger = _safe(a.get("trigger"))
                atr = _safe(a.get("atr"))
                if price is None or trigger is None or atr is None or atr <= 0:
                    return {"result": None, "error": "need price, trigger, atr>0"}
                return {"result": float((price - trigger) / atr), "op": op}

            if op == "risk_reward":
                entry = _safe(a.get("entry"))
                stop = _safe(a.get("stop"))
                target = _safe(a.get("target"))
                if entry is None or stop is None or target is None:
                    return {"result": None, "error": "need entry, stop, target"}
                risk = abs(entry - stop)
                reward = abs(target - entry)
                if risk == 0:
                    return {"result": None, "error": "zero risk"}
                return {"result": float(reward / risk), "op": op,
                        "risk_points": risk, "reward_points": reward}

            if op == "expected_value":
                win_rate = _safe(a.get("win_rate"))
                avg_win = _safe(a.get("avg_win"))
                avg_loss = _safe(a.get("avg_loss"))
                if win_rate is None or avg_win is None or avg_loss is None:
                    return {"result": None, "error": "need win_rate, avg_win, avg_loss"}
                if not (0.0 <= win_rate <= 1.0):
                    return {"result": None, "error": "win_rate must be in [0,1]"}
                ev = win_rate * avg_win - (1.0 - win_rate) * abs(avg_loss)
                return {"result": float(ev), "op": op}

            if op == "position_size":
                capital = _safe(a.get("capital"))
                risk_pct = _safe(a.get("risk_pct"))
                entry = _safe(a.get("entry"))
                stop = _safe(a.get("stop"))
                if None in (capital, risk_pct, entry, stop):
                    return {"result": None, "error": "need capital, risk_pct, entry, stop"}
                risk_per_unit = abs(entry - stop)
                if risk_per_unit <= 0:
                    return {"result": None, "error": "zero risk per unit"}
                risk_budget = capital * (risk_pct / 100.0)
                qty = risk_budget / risk_per_unit
                lots = max(1, int(qty // self._lot_size))
                return {"result": int(lots * self._lot_size), "lots": lots,
                        "risk_budget": risk_budget, "op": op}

            if op == "volatility_percentile":
                current = _safe(a.get("current_atr"))
                series = a.get("atr_series") or []
                if current is None or not series:
                    return {"result": None, "error": "need current_atr, atr_series (list)"}
                vals = sorted([float(x) for x in series if x is not None])
                if not vals:
                    return {"result": None, "error": "empty atr_series"}
                rank = sum(1 for v in vals if v <= current) / len(vals)
                return {"result": float(rank), "op": op}

            if op == "trend_strength":
                fast = _safe(a.get("ema_fast"))
                slow = _safe(a.get("ema_slow"))
                if fast is None or slow is None or slow == 0:
                    return {"result": None, "error": "need ema_fast, ema_slow (slow != 0)"}
                return {"result": float((fast - slow) / slow * 100.0), "op": op}

            if op == "pullback_depth":
                close = _safe(a.get("close"))
                swing_high = _safe(a.get("swing_high"))
                swing_low = _safe(a.get("swing_low"))
                if None in (close, swing_high, swing_low):
                    return {"result": None, "error": "need close, swing_high, swing_low"}
                range_ = swing_high - swing_low
                if range_ <= 0:
                    return {"result": None, "error": "zero range"}
                pct = (swing_high - close) / range_ * 100.0
                return {"result": float(pct), "op": op}

            return {"error": f"unknown op {op!r}"}
        except Exception as e:  # noqa: BLE001
            return {"error": f"breakout_calc failed: {e}"}

    @staticmethod
    def to_schema() -> dict[str, Any]:
        return schema(BreakoutCalcTool.name, BreakoutCalcTool.description,
                      BreakoutCalcTool.parameters)