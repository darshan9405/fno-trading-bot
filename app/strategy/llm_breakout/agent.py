"""LLM agent loop: run the breakout detector with tool-calling.

The base `detect_one` pipeline does a single chat call and parses JSON.
This module wraps that pipeline in a TOOL-CALLING agent loop so the model
can:
  1. Call `compute_indicators` for exact ATR/EMA/ADX/RSI/swing math.
  2. Call `breakout_calc` for risk/reward, expectancy, position size, etc.
  3. Call `fetch_news` to ground the call in symbol-specific news.
  4. Call `option_chain_summary` for at-the-money OI/PCR/max-pain context.

Loop termination:
  * The model returns a final assistant message with `content` containing a
    JSON signal payload (parsed by `validator`).
  * OR `LLM_AGENT_MAX_ITERATIONS` iterations have passed (cap, default 8).
  * OR a tool call returned a hard error AND we've already iterated >=2.

The whole loop is bounded — the scheduler must not block on a runaway
LLM. Every iteration logs the tool name + result size so operators can
see what the model is doing.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Callable

import pandas as pd

from app.strategy.llm_breakout import health as llm_health
from app.strategy.llm_breakout.client import LLMClient
from app.strategy.llm_breakout.tools.base import LLMTool, schema
from app.strategy.llm_breakout.tools.calculator import BreakoutCalcTool
from app.strategy.llm_breakout.tools.indicators import IndicatorsTool
from app.strategy.llm_breakout.tools.news import FetchNewsTool
from app.strategy.llm_breakout.tools.option_chain import OptionChainSummaryTool
from app.strategy.llm_breakout.validator import validate_signals

log = logging.getLogger(__name__)


FINAL_SYSTEM_SUFFIX = (
    "\n\nWhen you are ready to emit your final answer, return a JSON object "
    "with a top-level `signals` array. Each signal must have keys: "
    "`direction` (CALL|PUT), `pattern_type` (one of: horizontal_range, "
    "trendline, triangle, flag_pennant, head_shoulders, volume_breakout, "
    "custom), `trigger_price` (float, within 0.5% of last close), "
    "`confidence` (float in [0,1]), `rationale` (≤ 200 chars). Do NOT wrap "
    "your JSON in markdown fences. If there is no breakout, return "
    "`{\"signals\": []}` — never fabricate a signal."
)


def _safe_int(env_value: int | None, default: int) -> int:
    try:
        return int(env_value) if env_value is not None else default
    except (TypeError, ValueError):
        return default


def _truncate_for_emit(args: Any, max_len: int = 120) -> Any:
    """Best-effort short-form of a tool's args for live UI events.

    JSON-encoded args come in as either a `str` (raw `arguments` from the
    OpenAI chat-completion payload) or a `dict` (some clients pre-parse).
    We never want to push the full multi-KB option chain into a 2s polling
    payload — cap the string form at `max_len` chars.
    """
    if isinstance(args, str):
        s = args.strip()
        return s if len(s) <= max_len else s[: max_len - 1] + "…"
    try:
        s = json.dumps(args, default=str)
        return s if len(s) <= max_len else s[: max_len - 1] + "…"
    except Exception:
        return str(args)[:max_len]


def _max_iterations() -> int:
    try:
        from app.config import Config
        cfg = Config()
        return _safe_int(cfg.LLM_AGENT_MAX_ITERATIONS, 8)
    except Exception:
        return 8


def _build_toolbox(context: dict[str, Any]) -> list[LLMTool]:
    return [
        IndicatorsTool(context),
        BreakoutCalcTool(context),
        FetchNewsTool(context),
        OptionChainSummaryTool(context),
    ]


def _tool_schemas(tools: list[LLMTool]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for t in tools:
        out.append({
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description,
                "parameters": t.parameters,
            },
        })
    return out


def _execute_tool(tools: list[LLMTool], name: str, raw_args: Any) -> dict[str, Any]:
    for t in tools:
        if t.name == name:
            if isinstance(raw_args, str):
                try:
                    args = json.loads(raw_args) if raw_args.strip() else {}
                except json.JSONDecodeError:
                    return {"error": f"tool args for {name} not valid JSON: {raw_args[:200]}"}
            elif isinstance(raw_args, dict):
                args = raw_args
            else:
                args = {}
            try:
                return t.run(args)
            except Exception as e:  # noqa: BLE001
                return {"error": f"{name} crashed: {e}"}
    return {"error": f"unknown tool {name!r}"}


def _parse_final(content: str) -> dict[str, Any] | None:
    """Pull the JSON payload out of the assistant's final message.

    LLMs reliably emit final answers in one of these shapes:
      A. Pure JSON:                     `{"signals": []}`
      B. JSON in a code fence:          `` ```json\\n{...}\\n``` ``
      C. JSON fenced but unlabeled:     `` ```\\n{...}\\n``` ``
      D. JSON with prose around it:      `Here is the call: {...} — end`
      E. JSON with trailing commas:     `{"a": 1,}` (some open-source models)
      F. JSON with leading bullet/list: `- {"signals": [...]}` (rare)

    We try them in order of cheapness; first success wins. Returns None
    only when nothing parses, which the caller surfaces as the
    "final content not parseable JSON" health error.

    Logs the full content at debug level when all attempts fail so the
    operator can reproduce the model output without losing detail.
    """
    if not isinstance(content, str):
        return None
    s = content.strip()
    if not s:
        return None

    # (A) direct parse — fast path for well-behaved models
    parsed = _try_loads(s)
    if parsed is not None:
        return parsed

    # (B)+(C) strip a single code fence (with or without language tag)
    fenced = _strip_code_fence(s)
    if fenced is not None and fenced != s:
        parsed = _try_loads(fenced)
        if parsed is not None:
            return parsed

    # (D) extract the largest balanced { ... } block from the content.
    #     We pick the OUTERMOST braces (not the first inner-most), since
    #     the model's outer wrapper tends to be {"signals": [...]}.
    block = _extract_outer_json(s)
    if block is not None:
        parsed = _try_loads(block)
        if parsed is not None:
            return parsed

    # Last resort: drop trailing commas before `}` / `]` (some OSS models
    # emit them despite the prompt forbidding them) and retry the
    # outer-block attempt.
    cleaned = _strip_trailing_commas(s)
    if cleaned != s:
        parsed = _try_loads(cleaned)
        if parsed is not None:
            return parsed
        block = _extract_outer_json(cleaned)
        if block is not None:
            parsed = _try_loads(block)
            if parsed is not None:
                return parsed

    log.debug("agent_loop: parse_final exhausted; raw content=%r", content)
    return None


def _try_loads(s: str) -> dict[str, Any] | None:
    """Single-attempt `json.loads` — None on failure. No exception escape."""
    try:
        loaded = json.loads(s)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(loaded, dict):
        # The model occasionally wraps a JSON list or scalar in its reply;
        # those don't fit our `{signals: [...]}` contract, so reject.
        return None
    return loaded


def _strip_code_fence(s: str) -> str | None:
    """Strip ONE surrounding `` ``` `` fence (any language tag).

    Returns the unwrapped body, or None if `s` doesn't start with a fence.
    Tolerates ```` ``` `` ``, ```` ```json ``, ```` ```jsonc ``, etc., and
    a missing closing fence (we just return everything after the opener).
    """
    if not s.startswith("```"):
        return None
    # Drop the opening fence + optional language tag on the same line.
    first_nl = s.find("\n")
    if first_nl == -1:
        # No newline at all — there's no body to extract.
        return None
    body = s[first_nl + 1 :]
    # Drop the closing fence if present.
    if body.endswith("```"):
        body = body[: -3]
    return body.strip()


def _extract_outer_json(s: str) -> str | None:
    """Return the outermost balanced `{ ... }` substring, or None.

    LLMs sometimes wrap the JSON in prose ("Here is the answer: {...}.").
    Picking the first `{` and the last `}` is wrong when the outer block
    also contains inner braces (a `signals` list of dicts). Instead we
    scan for the first `{` and match braces to find its true closer.

    If the content opens with a `[...]` (a list wrapper, with or without
    prose), we skip past that wrapper before searching for the dict.
    A top-level JSON list does not fit the `{signals: [...]}` contract,
    so a payload that is *only* a list is rejected as a parse failure.
    """
    # Skip leading whitespace.
    i = 0
    while i < len(s) and s[i] in " \t\r\n":
        i += 1
    # If we land on a `[`, walk past one balanced list. Pure-list payloads
    # are not contract-compliant, but skipping lets us recover lists that
    # the model wrapped in extra prose.
    if i < len(s) and s[i] == "[":
        depth = 0
        in_str = False
        escape = False
        for j in range(i, len(s)):
            c = s[j]
            if in_str:
                if escape:
                    escape = False
                elif c == "\\":
                    escape = True
                elif c == '"':
                    in_str = False
                continue
            if c == '"':
                in_str = True
            elif c == "[":
                depth += 1
            elif c == "]":
                depth -= 1
                if depth == 0:
                    i = j + 1
                    break
        else:
            return None
        while i < len(s) and s[i] in " \t\r\n":
            i += 1

    start = s.find("{", i)
    if start == -1:
        return None
    depth = 0
    in_str = False
    escape = False
    for i in range(start, len(s)):
        c = s[i]
        if in_str:
            if escape:
                escape = False
            elif c == "\\":
                escape = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return s[start : i + 1]
    return None


def _strip_trailing_commas(s: str) -> str:
    """Remove trailing commas that precede `}` or `]` (some OSS models do this).

    Doesn't touch commas inside string literals — we step character-by-
    character and only act on commas that are at "structure" positions.
    """
    out: list[str] = []
    in_str = False
    escape = False
    for i, c in enumerate(s):
        if in_str:
            out.append(c)
            if escape:
                escape = False
            elif c == "\\":
                escape = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
            out.append(c)
            continue
        if c == ",":
            # Look ahead to see if the next non-whitespace char is } or ].
            j = i + 1
            while j < len(s) and s[j] in " \t\r\n":
                j += 1
            if j < len(s) and s[j] in "}]":
                continue  # skip this trailing comma
        out.append(c)
    return "".join(out)


def run_agent_loop(
    client: LLMClient,
    *,
    system_prompt: str,
    user_prompt: str,
    context: dict[str, Any],
    today_close: float,
    divergence_pct: float,
    min_confidence: float,
    on_tool_call: Callable[[dict[str, Any]], None] | None = None,
) -> list[dict[str, Any]]:
    """Drive the tool-calling agent loop; return validated signals.

    Mirrors the legacy `detect_one` contract:
      - returns [] on any failure (transport, parse, validation, max iters)
      - never raises
      - updates llm_health stats on every iteration (so health endpoint
        reflects the new wiring even when the model never returns a final
        answer)

    `on_tool_call(event)` is invoked once per completed tool execution with
    `{"iter": int, "name": str, "args": <truncated>, "result_keys": [str,...]}`.
    Used by the lead generator to pipe live LLM activity into the UI's
    progress panel — must be cheap and non-blocking (it's called from a
    worker thread).
    """
    tools = _build_toolbox(context)
    tool_schemas = _tool_schemas(tools)
    messages: list[dict[str, Any]] = [{"role": "user", "content": user_prompt}]
    max_iters = _max_iterations()
    tool_call_log: list[dict[str, Any]] = []
    start_ts = time.monotonic()
    final: dict[str, Any] | None = None
    last_content = ""

    for it in range(max_iters):
        log.debug("agent_loop: iter %d/%d", it + 1, max_iters)
        if not hasattr(client, "chat_with_tools"):
            # Stub or legacy client without tool support — fall back to single
            # shot chat_json so existing tests still work.
            log.debug("agent_loop: client has no chat_with_tools; falling back to chat_json")
            response = client.chat_json(system_prompt + FINAL_SYSTEM_SUFFIX, user_prompt)
            if isinstance(response, dict):
                response["_tool_calls"] = []
                last_content = json.dumps(response)
            else:
                llm_health.record_error("non-dict fallback response")
                return []
            final = response
            break
        try:
            msg = client.chat_with_tools(
                system_prompt + FINAL_SYSTEM_SUFFIX,
                messages,
                tools=tool_schemas,
            )
        except Exception as e:  # noqa: BLE001
            log.warning("agent_loop: chat_with_tools raised: %s", e)
            llm_health.record_error(f"chat_with_tools raised: {e}")
            return []
        if msg is None:
            llm_health.record_error("transport error in agent loop")
            return []
        content = msg.get("content") or ""
        tool_calls = msg.get("tool_calls") or []
        if content:
            last_content = content
        if not tool_calls:
            # Model emitted a final answer (no tool calls).
            final = _parse_final(content)
            if final is None:
                log.warning("agent_loop: final content not parseable as JSON (truncated): %s",
                            (content or "")[:300])
                llm_health.record_error("final content not parseable JSON")
                return []
            break

        # Tool-call turn: append the assistant message verbatim (so the LLM
        # sees its own tool_calls in the conversation history) then add a
        # tool message per result.
        messages.append({
            "role": "assistant",
            "content": content,
            "tool_calls": tool_calls,
        })
        for tc in tool_calls:
            fn = tc.get("function") or {}
            name = fn.get("name") or ""
            args = fn.get("arguments")
            tool_call_id = tc.get("id") or ""
            result = _execute_tool(tools, name, args)
            tool_call_log.append({
                "iter": it + 1,
                "name": name,
                "args": args,
                "result": result,
            })
            messages.append({
                "role": "tool",
                "tool_call_id": tool_call_id,
                "name": name,
                "content": json.dumps(result, default=str),
            })
            log.info("agent_loop: tool=%s iter=%d result_keys=%s",
                     name, it + 1, list(result.keys())[:5])
            # Live UI hook: notify callers (lead generator → job progress)
            # that a tool call just finished. Keep the payload small and
            # thread-safe (cheap dict, no shared state).
            if on_tool_call is not None:
                try:
                    on_tool_call({
                        "iter": it + 1,
                        "name": name,
                        # Truncate args/result aggressively — UI only needs a
                        # glance. Full data is still persisted in
                        # tool_call_log → lead meta for after-the-fact debug.
                        "args": _truncate_for_emit(args),
                        "result_keys": list(result.keys())[:6],
                    })
                except Exception as cb_err:  # noqa: BLE001
                    # A bad UI hook must never kill the agent loop.
                    log.debug("agent_loop: on_tool_call raised: %s", cb_err)
        if it == max_iters - 1:
            log.warning("agent_loop: hit max iterations (%d) for symbol", max_iters)

    duration_s = time.monotonic() - start_ts
    if final is None:
        # Cap hit; try to salvage a last parse from the latest content.
        log.warning("agent_loop: max iters reached; final_content=[:300] %s", last_content[:300])
        llm_health.record_error("agent loop max iterations reached")
        return []

    raw_signals = final.get("signals", [])
    if not isinstance(raw_signals, list):
        llm_health.record_error("malformed signals payload")
        return []

    # Attach the tool-call log so the lead's `meta` can surface it.
    final["_tool_calls"] = tool_call_log
    final["_agent_iters"] = len(tool_call_log)
    final["_agent_duration_s"] = round(duration_s, 3)

    valid = validate_signals(
        raw_signals,
        today_close=today_close,
        max_distance_pct=divergence_pct,
        min_confidence=min_confidence,
    )
    if not valid:
        log.info("agent_loop: validator rejected all %d signals for symbol", len(raw_signals))
    # Successful transport + parse path — count as a success regardless of
    # how many signals survived.
    llm_health.record_success()
    return valid