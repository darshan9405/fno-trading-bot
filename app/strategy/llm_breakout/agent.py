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
from dataclasses import dataclass, field
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


@dataclass
class AgentResult:
    """What a single ``detect_one`` invocation actually produced.

    Built from one round of the tool-calling agent loop. The strategy's
    ``generate()`` converts this into ``LeadCandidate`` rows when signals
    are present; the lead generator separately persists a ``LeadScanOutcome``
    row from the same object so the "Scanned stocks" panel can show *why*
    the model declined even when no lead came out.

    Fields:
      signals:            validated signal dicts (may be empty when the LLM
                          rejects the setup). This is what the legacy
                          `detect_one()` return value used to be — the rest
                          of the dataclass is new.
      short_reason:       A single-sentence (≤200 char) UI-friendly summary
                          of the decision — the LLM populates this for both
                          a generated lead AND a no-signal outcome. The
                          Leads table shows this verbatim, so the operator
                          can scan the run at a glance without opening the
                          detail modal. The bail paths (transport error,
                          parse failure, max iters) supply a stable
                          human-readable fallback so the table is never
                          empty even when the LLM never replied.
      rejection_reason:   The LLM-supplied natural-language explanation for
                          an empty signals list. The system prompt asks the
                          model to populate this field whenever it returns
                          `{"signals": []}` so the operator can audit the
                          decision; falls back to the last assistant
                          message if the field is absent / unparseable.
                          Rendered in full inside the detail modal.
      tool_calls:         full log of every tool call the agent loop made,
                          in order, with args + result payload. The UI
                          modal shows this verbatim.
      agent_iters:        number of agent-loop iterations that ran (== number
                          of tool-call turns; the final assistant message
                          doesn't count).
      agent_duration_s:   wall-clock seconds for the full loop, rounded.
      rationale:          the LAST assistant text the model emitted — i.e.
                          the final-message prose that may have wrapped the
                          JSON payload. Surface in the UI as a fallback
                          when `rejection_reason` is empty.
      error:              short string if the loop bailed (transport failure,
                          parse failure, validator rejection, max iters).
                          Empty on success. Full technical detail; the UI
                          shows it inside the detail modal.
    """

    signals: list[dict[str, Any]] = field(default_factory=list)
    short_reason: str | None = None
    rejection_reason: str | None = None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    agent_iters: int = 0
    agent_duration_s: float = 0.0
    rationale: str | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        """True when the loop finished without bailing (signals may still be empty)."""
        return self.error is None

    def to_dict(self) -> dict[str, Any]:
        return {
            "signals": list(self.signals),
            "short_reason": self.short_reason,
            "rejection_reason": self.rejection_reason,
            "tool_calls": list(self.tool_calls),
            "agent_iters": self.agent_iters,
            "agent_duration_s": self.agent_duration_s,
            "rationale": self.rationale,
            "error": self.error,
        }


FINAL_SYSTEM_SUFFIX = (
    "\n\nWhen you are ready to emit your final answer, return a JSON object "
    "with a top-level `signals` array. Each signal must have keys: "
    "`direction` (CALL|PUT), `pattern_type` (one of: horizontal_range, "
    "trendline, triangle, flag_pennant, head_shoulders, volume_breakout, "
    "custom), `trigger_price` (float, within 0.5% of last close), "
    "`confidence` (float in [0,1]), `rationale` (≤ 200 chars). Do NOT wrap "
    "your JSON in markdown fences. If there is no breakout, return "
    "`{\"signals\": []}` — never fabricate a signal. You MUST also include "
    "a top-level `short_reason` string (≤ 200 chars, ONE sentence) that "
    "the operator dashboard shows verbatim on a row: for a generated lead, "
    "say which pattern broke and where (e.g. \"CALL horizontal_range at "
    "1010; resistance cluster cleared\"); for a no-signal outcome, say why "
    "(e.g. \"awaiting breakout confirmation\", \"range too tight\", \"no "
    "volume expansion\", \"news risk too high\"). When signals is empty "
    "ALSO include `rejection_reason` with the longer technical explanation "
    "(up to 400 chars); the short reason stays short."
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


def _wall_budget_s() -> float:
    """Hard wall-time cap (seconds) for one per-instrument detection.

    Belt AND braces alongside ``LLM_AGENT_MAX_ITERATIONS``: when the LLM
    provider is degraded or the agent drifts into a degenerate tool-call
    loop, this guarantees the run never blocks the scheduler for more than
    the configured budget per instrument. 0 disables the check (not
    recommended for production).
    """
    try:
        from app.config import Config
        cfg = Config()
        return float(cfg.LLM_AGENT_WALL_BUDGET_S or 0.0)
    except Exception:
        return 90.0


def _max_duplicate_tools() -> int:
    try:
        from app.config import Config
        cfg = Config()
        return _safe_int(cfg.LLM_AGENT_MAX_DUPLICATE_TOOLS, 1)
    except Exception:
        return 1


def _tool_call_key(name: str, raw_args: Any) -> str:
    """Stable hash of (tool name, normalised arguments) for duplicate detection."""
    try:
        if isinstance(raw_args, str):
            try:
                norm = json.loads(raw_args)
            except json.JSONDecodeError:
                norm = {"_raw": raw_args}
        elif isinstance(raw_args, dict):
            norm = raw_args
        else:
            norm = {"_raw": str(raw_args)}
        return f"{name}|{json.dumps(norm, sort_keys=True, default=str)}"
    except Exception:
        return f"{name}|{str(raw_args)}"


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
) -> AgentResult:
    """Drive the tool-calling agent loop; return an ``AgentResult``.

    Always returns an ``AgentResult`` (never raises). The contract is:

      - ``result.ok`` is True when the loop finished without an internal
        failure; ``result.signals`` may still be empty (the model just
        didn't find a breakout). ``result.rejection_reason`` carries the
        LLM's explanation for that empty list.
      - On transport / parse / validator / max-iter failures ``result.ok``
        is False, ``result.error`` is set, and ``result.signals`` is
        empty. The call still updates ``llm_health`` so the system health
        endpoint reflects the new wiring even when the model never
        returns a final answer.

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
    wall_budget_s = _wall_budget_s()
    max_dup = _max_duplicate_tools()
    tool_call_log: list[dict[str, Any]] = []
    start_ts = time.monotonic()
    final: dict[str, Any] | None = None
    last_content = ""
    # Set of (tool_name, args) hashes called in the PREVIOUS iteration.
    # We compare against this at the start of each iter; if the model
    # re-calls the same tool with the same args, that's a degenerate loop.
    prev_iter_keys: set[str] = set()

    def _bail(error: str, short_reason: str) -> AgentResult:
        """Build an error-bearing result for any failure path.

        `error` is the full technical detail (shown in the modal's Error
        block). `short_reason` is a single-sentence UI summary that lets
        the Leads table stay readable even when the LLM never replied —
        it must be ≤200 chars so it fits cleanly in a row without
        truncation.
        """
        llm_health.record_error(error)
        return AgentResult(
            signals=[],
            short_reason=short_reason[:200],
            tool_calls=list(tool_call_log),
            agent_iters=len(tool_call_log),
            agent_duration_s=round(time.monotonic() - start_ts, 3),
            rationale=last_content or None,
            error=error,
        )

    for it in range(max_iters):
        log.debug("agent_loop: iter %d/%d", it + 1, max_iters)
        # Wall-time guard: short-circuit when the per-instrument budget is
        # exhausted. Done BEFORE the LLM call so a degraded provider can't
        # keep us blocked inside the network read.
        if wall_budget_s > 0 and (time.monotonic() - start_ts) >= wall_budget_s:
            log.warning(
                "agent_loop: wall-time budget %.1fs exceeded after %d iter(s)",
                wall_budget_s, it,
            )
            return _bail(
                f"agent loop exceeded wall-time budget ({wall_budget_s:.0f}s)",
                "LLM analysis timed out",
            )
        if not hasattr(client, "chat_with_tools"):
            # Stub or legacy client without tool support — fall back to single
            # shot chat_json so existing tests still work.
            log.debug("agent_loop: client has no chat_with_tools; falling back to chat_json")
            response = client.chat_json(system_prompt + FINAL_SYSTEM_SUFFIX, user_prompt)
            if isinstance(response, dict):
                last_content = json.dumps(response)
                final = response
            else:
                return _bail("non-dict fallback response", "LLM returned an unexpected response")
            break
        try:
            msg = client.chat_with_tools(
                system_prompt + FINAL_SYSTEM_SUFFIX,
                messages,
                tools=tool_schemas,
            )
        except Exception as e:  # noqa: BLE001
            log.warning("agent_loop: chat_with_tools raised: %s", e)
            return _bail(
                f"chat_with_tools raised: {e}",
                "LLM unavailable — request failed",
            )
        if msg is None:
            # The OpenAI-compat client returns None after exhausting retries
            # on transport / 5xx errors. Surface a friendly one-liner so the
            # operator sees "LLM unavailable" in the table instead of the
            # raw internal tag; full detail still lives in `error`.
            return _bail(
                "transport error in agent loop",
                "LLM unavailable — transport error",
            )
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
                return _bail(
                    "final content not parseable JSON",
                    "LLM response unparseable",
                )
            break

        # Tool-call turn: append the assistant message verbatim (so the LLM
        # sees its own tool_calls in the conversation history) then add a
        # tool message per result.
        messages.append({
            "role": "assistant",
            "content": content,
            "tool_calls": tool_calls,
        })
        # Reset the duplicate-streak at the top of EACH iteration. The
        # guard below only fires when the model makes the EXACT same call
        # (same name + same args) across iterations, not when it makes
        # multiple distinct calls in a single iter.
        curr_iter_keys: set[str] = set()
        duplicate_bail = False
        for tc in tool_calls:
            fn = tc.get("function") or {}
            name = fn.get("name") or ""
            args = fn.get("arguments")
            tool_call_id = tc.get("id") or ""
            # Duplicate tool-call guard: when the same single call appears
            # in BOTH this iter AND the previous one, the model is stuck.
            # Inject a one-line tool result and bail rather than letting
            # the next LLM call run (which would just hit the same tool
            # again).
            key = _tool_call_key(name, args)
            curr_iter_keys.add(key)
            if (
                max_dup >= 0
                and it >= 1
                and len(tool_calls) == 1
                and len(prev_iter_keys) == 1
                and key in prev_iter_keys
            ):
                log.warning(
                    "agent_loop: duplicate tool call %s (%s) across iters — short-circuiting",
                    name, str(args)[:120],
                )
                result = {
                    "warning": (
                        f"duplicate tool call: {name} was just executed with "
                        "the same arguments in the previous iteration; "
                        "please converge with the data you already have."
                    ),
                    "duplicate_short_circuit": True,
                }
                tool_call_log.append({
                    "iter": it + 1,
                    "name": name,
                    "args": args,
                    "result": result,
                    "short_circuit": True,
                })
                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call_id,
                    "name": name,
                    "content": json.dumps(result, default=str),
                })
                # Force the next iter to bail with a "no_signal" final.
                duplicate_bail = True
                break
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
        # Save this iter's keys for the next iter's duplicate guard.
        prev_iter_keys = curr_iter_keys
        # Duplicate tool-call short-circuit: don't keep calling the LLM
        # with the same degenerate history — bail through the same _bail
        # helper used by the other safety nets so the AgentResult shape
        # (error string, ok=False) is consistent across bail reasons.
        if duplicate_bail:
            return _bail(
                "agent loop aborted: duplicate tool call detected",
                "LLM did not converge (duplicate tool call)",
            )
        if it == max_iters - 1:
            log.warning("agent_loop: hit max iterations (%d) for symbol", max_iters)

    duration_s = time.monotonic() - start_ts
    if final is None:
        # Cap hit; try to salvage a last parse from the latest content.
        log.warning("agent_loop: max iters reached; final_content=[:300] %s", last_content[:300])
        return _bail(
            "agent loop max iterations reached",
            "LLM didn't reach a decision (max iterations)",
        )

    raw_signals = final.get("signals", [])
    if not isinstance(raw_signals, list):
        return _bail(
            "malformed signals payload",
            "LLM returned malformed signals payload",
        )

    # Pull the model-supplied reasons. `short_reason` is the single-sentence
    # UI summary the table shows verbatim (≤200 chars). `rejection_reason`
    # is the verbose natural-language explanation the modal renders in full.
    # Both fall back gracefully so the UI never sees NULL for a
    # non-empty "no_signal" outcome.
    short_reason = final.get("short_reason")
    rejection_reason = final.get("rejection_reason")
    if not rejection_reason and not raw_signals:
        fallback = (last_content or "").strip()
        if fallback:
            rejection_reason = fallback[:32_000]
    if not short_reason and not raw_signals:
        # No explicit short_reason from the LLM — derive one from the
        # available context so the table always has something to show.
        snippet = (rejection_reason or rationale or "").strip().replace("\n", " ")
        if snippet:
            short_reason = snippet[:200]
    if not short_reason and raw_signals:
        # Generated signals — synthesise a one-line summary from the first
        # valid signal's `rationale` field (always populated by validate_signals).
        try:
            first = raw_signals[0] if isinstance(raw_signals, list) else None
            if isinstance(first, dict):
                d = str(first.get("direction") or "").strip()
                p = str(first.get("pattern_type") or "").strip().replace("_", " ")
                r = str(first.get("rationale") or "").strip().replace("\n", " ")
                if d and p and r:
                    short_reason = f"{d} {p}: {r}"[:200]
                elif d and p:
                    short_reason = f"{d} {p} breakout"[:200]
        except Exception:
            pass

    valid = validate_signals(
        raw_signals,
        today_close=today_close,
        max_distance_pct=divergence_pct,
        min_confidence=min_confidence,
    )
    if not valid and raw_signals:
        log.info("agent_loop: validator rejected all %d signals for symbol", len(raw_signals))
        # All signals got filtered out by the structural validator — the
        # table should still show *why*, not blank. Use the LLM's own
        # short_reason / rejection_reason if present; otherwise fall back
        # to a generic one-liner so the row stays readable.
        if not short_reason:
            short_reason = "All signals failed structural validation"[:200]

    # Successful transport + parse path — count as a success regardless of
    # how many signals survived.
    llm_health.record_success()
    return AgentResult(
        signals=valid,
        short_reason=str(short_reason)[:200] if short_reason else None,
        rejection_reason=str(rejection_reason)[:32_000] if rejection_reason else None,
        tool_calls=list(tool_call_log),
        agent_iters=len(tool_call_log),
        agent_duration_s=round(duration_s, 3),
        rationale=last_content or None,
        error=None,
    )