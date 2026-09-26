"""Tests for the LLM breakout tool system + agent loop.

Covers:
  - IndicatorsTool: every indicator branch (full + insufficient data)
  - BreakoutCalcTool: every op (numeric edge cases + unknown op)
  - FetchNewsTool: handles empty symbol, no key, etc.
  - OptionChainSummaryTool: requires broker + underlying_key
  - run_agent_loop: stub-driven end-to-end (final answer, tool-call path,
    max-iter cap, transport-error handling)

The legacy StubClient (in `app.strategy.llm_breakout`) keeps its single-
turn chat_json contract; the agent loop auto-detects the absence of
`chat_with_tools` and falls back, so existing tests continue to pass.
"""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from typing import Any

import numpy as np
import pandas as pd
import pytest


# --- fixtures ------------------------------------------------------------


def _df(n: int = 260, *, base: float = 100.0, vol: float = 0.5) -> pd.DataFrame:
    rng = np.random.default_rng(0)
    close = base + np.cumsum(rng.normal(0, vol, n))
    high = close + np.abs(rng.normal(0, 1, n))
    low = close - np.abs(rng.normal(0, 1, n))
    vol_arr = np.abs(rng.normal(1000, 200, n))
    idx = pd.date_range("2024-01-01", periods=n, freq="D")
    return pd.DataFrame(
        {"open": close, "high": high, "low": low, "close": close, "volume": vol_arr},
        index=idx,
    )


class _StubClientWithTools:
    """Minimal OpenAI-compat client exposing `chat_with_tools`.

    Each call pops a response from `_responses`. If the response has
    `tool_calls`, the agent loop will execute them and call again. The
    final response (without tool_calls) is parsed as JSON.
    """

    def __init__(self, responses: list[dict[str, Any]]):
        self._responses = list(responses)
        self.calls: list[Any] = []

    def chat_json(self, system, user):
        return {"_transport_error": True}

    def chat_with_tools(self, system, messages, tools=None):
        self.calls.append({"system": system, "messages": list(messages), "tools": tools})
        if not self._responses:
            return {"role": "assistant", "content": '{"signals": []}'}
        return self._responses.pop(0)


# --- IndicatorsTool ------------------------------------------------------


def test_indicators_tool_returns_full_payload():
    from app.strategy.llm_breakout.tools.indicators import IndicatorsTool

    df = _df(260)
    tool = IndicatorsTool({"candles": df})
    out = tool.run({})
    assert "error" not in out
    assert out["last_close"] == float(df["close"].iloc[-1])
    assert out["atr_14"] > 0
    assert out["ema_20"] is not None
    assert out["adx_14"] is not None
    assert 0 <= out["rsi_14"] <= 100
    assert out["bollinger_20_2"]["zscore"] is not None
    assert out["year_high_low"]["pct_from_high"] is not None
    assert isinstance(out["swing_points"]["highs"], list)
    assert isinstance(out["swing_points"]["lows"], list)
    assert out["pivots"]["pp"] is not None


def test_indicators_tool_handles_short_history():
    from app.strategy.llm_breakout.tools.indicators import IndicatorsTool

    df = _df(15)  # too short for ATR/ADX/etc
    tool = IndicatorsTool({"candles": df})
    out = tool.run({})
    assert "error" not in out
    # ATR needs 15+ bars; with only 15 we still get values, just less stable.
    # The point of this test is "no crash".
    assert "last_close" in out


def test_indicators_tool_handles_empty_candles():
    from app.strategy.llm_breakout.tools.indicators import IndicatorsTool

    tool = IndicatorsTool({"candles": pd.DataFrame()})
    out = tool.run({})
    assert "error" in out


# --- BreakoutCalcTool ----------------------------------------------------


def test_breakout_calc_breakout_strength():
    from app.strategy.llm_breakout.tools.calculator import BreakoutCalcTool
    out = BreakoutCalcTool({}).run({"op": "breakout_strength",
                                    "args": {"price": 110, "trigger": 105, "atr": 2.5}})
    assert out["result"] == pytest.approx(2.0)


def test_breakout_calc_risk_reward():
    from app.strategy.llm_breakout.tools.calculator import BreakoutCalcTool
    out = BreakoutCalcTool({}).run({"op": "risk_reward",
                                    "args": {"entry": 100, "stop": 95, "target": 115}})
    assert out["result"] == pytest.approx(3.0)
    assert out["risk_points"] == 5.0
    assert out["reward_points"] == 15.0


def test_breakout_calc_expected_value():
    from app.strategy.llm_breakout.tools.calculator import BreakoutCalcTool
    out = BreakoutCalcTool({}).run({"op": "expected_value",
                                    "args": {"win_rate": 0.6, "avg_win": 200, "avg_loss": 100}})
    # 0.6 * 200 - 0.4 * 100 = 80
    assert out["result"] == pytest.approx(80.0)


def test_breakout_calc_position_size():
    from app.strategy.llm_breakout.tools.calculator import BreakoutCalcTool
    out = BreakoutCalcTool({}).run({"op": "position_size",
                                    "args": {"capital": 100000, "risk_pct": 1,
                                             "entry": 100, "stop": 95}})
    # risk budget 1000, risk/unit 5 -> qty 200 (no lot size cap)
    assert out["result"] == 200


def test_breakout_calc_volatility_percentile():
    from app.strategy.llm_breakout.tools.calculator import BreakoutCalcTool
    out = BreakoutCalcTool({}).run({"op": "volatility_percentile",
                                    "args": {"current_atr": 2.5,
                                             "atr_series": [1.5, 2.0, 2.2, 2.5, 3.0, 3.5]}})
    assert 0 <= out["result"] <= 1


def test_breakout_calc_unknown_op_returns_error():
    from app.strategy.llm_breakout.tools.calculator import BreakoutCalcTool
    out = BreakoutCalcTool({}).run({"op": "nope", "args": {}})
    assert "error" in out


# --- FetchNewsTool -------------------------------------------------------


def test_fetch_news_requires_symbol():
    from app.strategy.llm_breakout.tools.news import FetchNewsTool

    tool = FetchNewsTool({})
    out = tool.run({})
    assert "error" in out
    assert out.get("items") == []


# A trimmed real-world Google News RSS payload for "RELIANCE" — modelled on
# the live format so the parser is exercised against the actual shape (title
# with ` - Publisher` suffix, Google redirect `<link>`, `<source url=...>`,
# RFC-822 `<pubDate>`, HTML-embedded `<description>`).
_GOOGLE_NEWS_SAMPLE = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>RELIANCE - Google News</title>
    <item>
      <title>Reliance Q3 results: profit jumps 12% YoY - The Economic Times</title>
      <link>https://news.google.com/rss/articles/CBMi_TEST_ARTICLE_1</link>
      <pubDate>Fri, 26 Sep 2026 09:30:00 GMT</pubDate>
      <description>&lt;a href=&quot;...&quot;&gt;Reliance Q3 results&lt;/a&gt;&amp;nbsp;&lt;font&gt;The Economic Times&lt;/font&gt;</description>
      <source url="https://m.economictimes.com">The Economic Times</source>
    </item>
    <item>
      <title>Jio IPO: Reliance arm files DRHP with SEBI - Livemint</title>
      <link>https://news.google.com/rss/articles/CBMi_TEST_ARTICLE_3</link>
      <pubDate>Wed, 24 Sep 2026 07:15:00 GMT</pubDate>
      <description>&lt;a href=&quot;...&quot;&gt;Jio IPO: Reliance arm files DRHP&lt;/a&gt;</description>
      <source url="https://www.livemint.com">Livemint</source>
    </item>
    <item>
      <title>Reliance Jio wins regulatory nod for IPO - CNBC - CNBC</title>
      <link>https://news.google.com/rss/articles/CBMi_TEST_ARTICLE_4</link>
      <pubDate>Sun, 30 Aug 2026 07:00:00 GMT</pubDate>
      <description>&lt;a href=&quot;...&quot;&gt;Reliance Jio wins regulatory nod&lt;/a&gt;</description>
      <source url="https://www.cnbc.com">CNBC</source>
    </item>
  </channel>
</rss>"""

# Tiny feed used to exercise the dedup path: same headline syndicated by two
# different publishers should collapse to a single row.
_GOOGLE_NEWS_DUP_SAMPLE = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <item>
      <title>Reliance Q3 results: profit jumps 12% YoY - The Economic Times</title>
      <link>https://news.google.com/rss/articles/DUP_A</link>
      <pubDate>Fri, 26 Sep 2026 09:30:00 GMT</pubDate>
      <source url="https://m.economictimes.com">The Economic Times</source>
    </item>
    <item>
      <title>Reliance Q3 results: profit jumps 12% YoY - Business Standard</title>
      <link>https://news.google.com/rss/articles/DUP_B</link>
      <pubDate>Fri, 26 Sep 2026 08:00:00 GMT</pubDate>
      <source url="https://www.business-standard.com">Business Standard</source>
    </item>
  </channel>
</rss>"""


class _FakeHTTPResponse:
    def __init__(self, content: bytes, status_code: int = 200) -> None:
        self.content = content
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _FakeHTTPClientOK:
    def __init__(self, content: bytes) -> None:
        self._content = content

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get(self, url, headers=None):
        return _FakeHTTPResponse(self._content, 200)


class _FakeHTTPClientFail:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get(self, url, headers=None):
        raise RuntimeError("network down")


def test_fetch_news_parses_google_rss(monkeypatch):
    from app.strategy import llm_breakout
    from app.strategy.llm_breakout.tools import news

    monkeypatch.setattr(news.httpx, "Client", lambda *a, **kw: _FakeHTTPClientOK(_GOOGLE_NEWS_SAMPLE))

    out = llm_breakout.tools.news.FetchNewsTool({}).run({"symbol": "RELIANCE", "n": 5})

    assert "items" in out and len(out["items"]) == 3
    # Title suffix ` - Publisher` is stripped (we already surface it via `source`).
    assert out["items"][0]["title"] == "Reliance Q3 results: profit jumps 12% YoY"
    assert out["items"][1]["title"] == "Jio IPO: Reliance arm files DRHP with SEBI"
    # Source = publisher display name from <source> element.
    assert out["items"][0]["source"] == "The Economic Times"
    assert out["items"][1]["source"] == "Livemint"
    # URL is the Google redirect (clickable to the real article in a browser).
    assert out["items"][0]["url"].startswith("https://news.google.com/rss/articles/")
    # pubDate converted to ISO-8601 UTC.
    assert out["items"][0]["published"] == "2026-09-26T09:30:00Z"
    # age is a relative string and not None when published is known.
    assert out["items"][0]["age"] is not None
    # Snippet has HTML stripped.
    assert "<" not in out["items"][0]["snippet"]
    assert out["count"] == 3


def test_fetch_news_dedupes_syndications(monkeypatch):
    """Same headline published by multiple outlets should collapse to one row."""
    from app.strategy.llm_breakout.tools.news import FetchNewsTool

    monkeypatch.setattr(
        "app.strategy.llm_breakout.tools.news.httpx.Client",
        lambda *a, **kw: _FakeHTTPClientOK(_GOOGLE_NEWS_DUP_SAMPLE),
    )

    out = FetchNewsTool({}).run({"symbol": "RELIANCE", "n": 5})
    titles = [it["title"] for it in out["items"]]
    # Two items share a headline (syndication) — only one survives.
    assert len(titles) == 1
    assert titles[0] == "Reliance Q3 results: profit jumps 12% YoY"


def test_fetch_news_handles_network_failure(monkeypatch):
    from app.strategy.llm_breakout.tools.news import FetchNewsTool

    monkeypatch.setattr(
        "app.strategy.llm_breakout.tools.news.httpx.Client",
        lambda *a, **kw: _FakeHTTPClientFail(),
    )

    out = FetchNewsTool({}).run({"symbol": "RELIANCE", "n": 5})
    # Network failure → empty list with the standard "no news returned" note.
    assert out["items"] == []
    assert "note" in out


def test_fetch_news_handles_empty_feed(monkeypatch):
    from app.strategy.llm_breakout.tools.news import FetchNewsTool

    empty = b"""<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0"><channel><title>empty</title></channel></rss>"""
    monkeypatch.setattr(
        "app.strategy.llm_breakout.tools.news.httpx.Client",
        lambda *a, **kw: _FakeHTTPClientOK(empty),
    )

    out = FetchNewsTool({}).run({"symbol": "ILLIQUID", "n": 5})
    assert out["items"] == []
    assert "note" in out


# --- OptionChainSummaryTool ----------------------------------------------


def test_option_chain_tool_requires_broker():
    from app.strategy.llm_breakout.tools.option_chain import OptionChainSummaryTool

    tool = OptionChainSummaryTool({"broker": None, "today": date.today()})
    out = tool.run({"underlying_key": "NSE_EQ|123"})
    assert "error" in out


def test_option_chain_tool_handles_missing_expiry(monkeypatch):
    from app.strategy.llm_breakout.tools.option_chain import OptionChainSummaryTool

    class _FakeBroker:
        def get_ltp(self, keys):
            return {k: 100.0 for k in keys}

        def get_option_contracts(self, underlying_key, expiry=None):
            return []

    # Patch the contract_service import the tool does internally so we don't
    # need a real broker.
    monkeypatch.setattr(
        "app.services.contract_service.next_expiry", lambda *a, **kw: None,
        raising=False,
    )
    tool = OptionChainSummaryTool({"broker": _FakeBroker(), "today": date.today()})
    out = tool.run({"underlying_key": "NSE_EQ|123"})
    assert "error" in out


# --- Agent loop ----------------------------------------------------------


def test_agent_loop_final_answer_parses_signals(monkeypatch):
    from app.strategy.llm_breakout.agent import run_agent_loop
    from app.strategy.llm_breakout.tools.indicators import IndicatorsTool

    # First response: ask to call compute_indicators. Second: final answer.
    client = _StubClientWithTools([
        {"role": "assistant", "content": "",
         "tool_calls": [{"id": "c1", "type": "function",
                         "function": {"name": "compute_indicators",
                                      "arguments": "{}"}}]},
        {"role": "assistant", "content": '{"signals": [{"direction": "CALL", '
                                            '"pattern_type": "horizontal_range", '
                                            '"trigger_price": 105.2, '
                                            '"confidence": 0.85, '
                                            '"rationale": "range break"}]}'},
    ])
    df = _df(260, base=100.0, vol=0.5)
    df.iloc[-1, df.columns.get_loc("close")] = 105.2  # ensure trigger near close
    context = {"candles": df, "broker": None, "today": date.today(), "lot_size": 50}
    result = run_agent_loop(
        client,
        system_prompt="you are a trading bot",
        user_prompt="analyze RELIANCE",
        context=context,
        today_close=105.2,
        divergence_pct=0.5,
        min_confidence=0.7,
    )
    # Newer return type is `AgentResult` — verify both the signals and the
    # rich metadata the lead generator needs.
    assert len(result.signals) == 1
    assert result.signals[0]["direction"] == "CALL"
    assert result.signals[0]["pattern_type"] == "horizontal_range"
    assert result.ok is True
    assert result.error is None
    assert result.agent_iters == 1  # one tool-call turn before the final answer
    # Tool call log is attached for the lead-meta path.
    assert len(client.calls) == 2


def test_agent_loop_max_iters_no_final(monkeypatch):
    """When the model never stops calling tools, the loop hits a safety net
    (iter cap OR duplicate-tool-call guard) and returns an error-bearing
    AgentResult (no fabricated signals)."""
    from app.strategy.llm_breakout.agent import run_agent_loop

    # Always ask to call compute_indicators with identical args — never
    # emits a final answer. The duplicate-tool guard should fire as soon
    # as the second iter repeats the same call verbatim.
    client = _StubClientWithTools([
        {"role": "assistant", "content": "",
         "tool_calls": [{"id": "c1", "type": "function",
                         "function": {"name": "compute_indicators",
                                      "arguments": "{}"}}]}
        for _ in range(20)
    ])
    df = _df(260)
    context = {"candles": df, "broker": None, "today": date.today(), "lot_size": 1}
    result = run_agent_loop(
        client,
        system_prompt="sys", user_prompt="user",
        context=context,
        today_close=float(df["close"].iloc[-1]),
        divergence_pct=0.5, min_confidence=0.7,
    )
    assert result.signals == []
    assert result.ok is False
    # Two safety nets can fire first: the duplicate-tool guard (preferred,
    # because it surfaces "the LLM got stuck" rather than just "we gave
    # up"), or the iteration cap. Both are correct behaviour; the test
    # only asserts that we bailed before burning all 20 calls.
    assert result.error and ("duplicate" in result.error or "max iterations" in result.error)
    assert len(client.calls) < 20


def test_agent_loop_transport_error_returns_empty():
    from app.strategy.llm_breakout.agent import run_agent_loop

    class _FailingClient:
        def chat_json(self, system, user):
            return {"_transport_error": True}

        def chat_with_tools(self, system, messages, tools=None):
            return None

    df = _df(260)
    context = {"candles": df, "broker": None, "today": date.today(), "lot_size": 1}
    result = run_agent_loop(
        _FailingClient(),
        system_prompt="sys", user_prompt="user",
        context=context,
        today_close=float(df["close"].iloc[-1]),
        divergence_pct=0.5, min_confidence=0.7,
    )
    assert result.signals == []
    assert result.ok is False
    assert result.error is not None
    # Transport error must surface a user-friendly single-sentence
    # short_reason so the Leads table isn't blank for this row.
    assert result.short_reason
    assert len(result.short_reason) <= 200
    assert "transport" in result.short_reason.lower() or "unavailable" in result.short_reason.lower()


def test_agent_loop_short_reason_from_payload():
    """The LLM's `short_reason` (≤200 chars) flows through AgentResult
    verbatim so the Leads table can show it without truncation."""
    from app.strategy.llm_breakout.agent import run_agent_loop

    client = _StubClientWithTools([
        {"role": "assistant",
         "content": '{"signals": [], "short_reason": "Awaiting breakout confirmation", "rejection_reason": "longer explanation"}'},
    ])
    df = _df(260)
    context = {"candles": df, "broker": None, "today": date.today(), "lot_size": 1}
    result = run_agent_loop(
        client,
        system_prompt="sys", user_prompt="user",
        context=context,
        today_close=float(df["close"].iloc[-1]),
        divergence_pct=0.5, min_confidence=0.7,
    )
    assert result.ok is True
    assert result.short_reason == "Awaiting breakout confirmation"
    assert result.rejection_reason == "longer explanation"


def test_agent_loop_short_reason_truncated_at_200():
    """A verbose short_reason is hard-capped at 200 chars so a row
    never blows up the table layout."""
    from app.strategy.llm_breakout.agent import run_agent_loop

    long_reason = "x" * 500
    client = _StubClientWithTools([
        {"role": "assistant",
         "content": f'{{"signals": [], "short_reason": "{long_reason}"}}'},
    ])
    df = _df(260)
    context = {"candles": df, "broker": None, "today": date.today(), "lot_size": 1}
    result = run_agent_loop(
        client,
        system_prompt="sys", user_prompt="user",
        context=context,
        today_close=float(df["close"].iloc[-1]),
        divergence_pct=0.5, min_confidence=0.7,
    )
    assert result.short_reason is not None
    assert len(result.short_reason) <= 200


def test_agent_loop_short_reason_synthesised_when_missing():
    """When the LLM forgets to populate short_reason but supplies a
    rejection_reason, the agent loop derives one so the row stays
    readable."""
    from app.strategy.llm_breakout.agent import run_agent_loop

    client = _StubClientWithTools([
        {"role": "assistant",
         "content": '{"signals": [], "rejection_reason": "range too tight; no volume expansion"}'},
    ])
    df = _df(260)
    context = {"candles": df, "broker": None, "today": date.today(), "lot_size": 1}
    result = run_agent_loop(
        client,
        system_prompt="sys", user_prompt="user",
        context=context,
        today_close=float(df["close"].iloc[-1]),
        divergence_pct=0.5, min_confidence=0.7,
    )
    assert result.ok is True
    assert result.short_reason
    assert "range too tight" in result.short_reason.lower()


def test_agent_loop_captures_rejection_reason():
    """When the model returns `{"signals": [], "rejection_reason": "..."}`,
    the AgentResult surfaces that reason verbatim on `rejection_reason`
    so the lead generator's "Scanned stocks" panel can show it."""
    from app.strategy.llm_breakout.agent import run_agent_loop

    client = _StubClientWithTools([
        {"role": "assistant",
         "content": '{"signals": [], "rejection_reason": "awaiting breakout confirmation"}'},
    ])
    df = _df(260)
    context = {"candles": df, "broker": None, "today": date.today(), "lot_size": 1}
    result = run_agent_loop(
        client,
        system_prompt="sys", user_prompt="user",
        context=context,
        today_close=float(df["close"].iloc[-1]),
        divergence_pct=0.5, min_confidence=0.7,
    )
    assert result.ok is True
    assert result.signals == []
    assert result.rejection_reason == "awaiting breakout confirmation"
    assert result.error is None


def test_detect_one_returns_agent_result():
    """`detect_one` should return an `AgentResult`, not a list, on every path."""
    from app.strategy.llm_breakout.agent import AgentResult
    from app.strategy.llm_breakout.detector import detect_one

    client = _StubClientWithTools([
        {"role": "assistant",
         "content": '{"signals": []}'},
    ])
    df = _df(260)
    res = detect_one(
        client,
        symbol="RELIANCE",
        underlying_key="NSE_EQ|INE002A01018",
        candles=df,
        lookback_candles=250,
        divergence_pct=0.5,
        min_confidence=0.7,
    )
    assert isinstance(res, AgentResult)
    assert res.ok is True
    assert res.signals == []
    # No rejection_reason in the payload → falls back to last assistant text.
    assert res.rejection_reason is not None