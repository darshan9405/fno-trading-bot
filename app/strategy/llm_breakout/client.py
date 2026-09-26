"""OpenAI-compatible chat client for the LLM breakout detector.

We talk to the MiniMax M3 provider directly (not via OpenRouter) using its
OpenAI-compatible `/v1/chat/completions` endpoint. The client is a thin
wrapper around `httpx`: it posts the chat request with Bearer auth and
returns the parsed JSON response. Never raises on transport errors — the
caller (detector) translates transport failures into an empty signal list.

Two request shapes are supported:
  * `chat_json`     — single-turn, JSON-mode response_format. Used when the
                      caller doesn't need tool calls.
  * `chat_with_tools` — multi-turn with tool definitions; returns the raw
                      assistant message so the agent loop can branch on
                      `tool_calls` vs final content.

The client is injectable: tests substitute a stub via the `LLMClient`
constructor argument on the strategy class so the rest of the pipeline can
be exercised without a real API call.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any, Protocol

import httpx

log = logging.getLogger(__name__)


class LLMTransportError(Exception):
    """Raised on a non-retryable HTTP failure or after retries are exhausted."""


class LLMClient(Protocol):
    """Minimal interface every LLM client (real or stub) must implement."""

    def chat_json(self, system: str, user: str) -> dict[str, Any]:
        """Single-turn JSON chat. Never raises; returns `{_transport_error: True}` on failure."""
        ...

    def chat_with_tools(
        self,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any] | None:
        """Multi-turn chat that may return tool_calls. Returns the raw assistant
        message dict (with `content`, optional `tool_calls`, optional `reasoning`)
        or None on transport failure."""
        ...


class OpenAICompatClient:
    """Concrete OpenAI-compatible chat-completions client."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        timeout_s: float = 60.0,
        max_retries: int = 2,
        temperature: float = 0.2,
        reasoning_effort: str = "",
        reasoning_max_tokens: int = 0,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._timeout_s = float(timeout_s)
        self._max_retries = max(0, int(max_retries))
        self._temperature = float(temperature)
        self._reasoning_effort = str(reasoning_effort or "").strip()
        self._reasoning_max_tokens = max(0, int(reasoning_max_tokens))
        # Provider-specific headers (MiniMax app attribution, etc.).
        # Merged AFTER the auth/content-type headers so callers can't override
        # them by accident.
        self._extra_headers: dict[str, str] = {
            str(k): str(v) for k, v in (extra_headers or {}).items()
        }
        # Persistent httpx connection pool. Reused across calls so we avoid
        # the TLS handshake cost on every prompt. ``httpx.Client`` is NOT
        # safe to share concurrently; ``_http_lock`` serialises the post
        # itself which is fast relative to network I/O.
        self._http_lock = threading.Lock()
        limits = httpx.Limits(
            max_connections=16,
            max_keepalive_connections=8,
            keepalive_expiry=30.0,
        )
        self._http = httpx.Client(
            timeout=self._timeout_s,
            limits=limits,
            headers={"Content-Type": "application/json"},
        )

    # ---- public API ------------------------------------------------------

    def chat_json(self, system: str, user: str) -> dict[str, Any]:
        """Single-turn chat with `response_format={"type":"json_object"}`.

        Never raises; returns `{"_transport_error": True}` on failure so the
        detector can short-circuit without try/except.
        """
        url = f"{self._base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        payload: dict[str, Any] = {
            "model": self._model,
            "temperature": self._temperature,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        self._apply_reasoning(payload)
        try:
            body = self._post_with_retry(url, headers, payload)
        except LLMTransportError:
            return {"_transport_error": True}
        try:
            content = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            log.warning("llm: malformed single-turn response: %s", exc)
            return {"_transport_error": True}
        if not isinstance(content, str):
            return {"_transport_error": True}
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError as exc:
            log.warning("llm: content is not valid JSON: %s", exc)
            return {"_transport_error": True}
        # Stash the model's reasoning (chain-of-thought) alongside the parsed
        # payload so callers / logs can surface it. Non-reasoning models simply
        # don't populate `reasoning_content`.
        msg = body.get("choices", [{}])[0].get("message", {})
        if isinstance(msg, dict) and (msg.get("reasoning_content") or msg.get("reasoning")):
            parsed["_reasoning"] = msg.get("reasoning_content") or msg.get("reasoning")
        return parsed

    def chat_with_tools(
        self,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any] | None:
        """Multi-turn chat that may return tool_calls.

        Returns the raw assistant `message` dict with `role`, `content`,
        optional `tool_calls`, and optional `reasoning_content` fields
        extracted from the response. Returns None on transport failure.
        """
        url = f"{self._base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        payload: dict[str, Any] = {
            "model": self._model,
            "temperature": self._temperature,
            "messages": [{"role": "system", "content": system}] + list(messages),
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        self._apply_reasoning(payload)
        try:
            body = self._post_with_retry(url, headers, payload)
        except LLMTransportError:
            return None
        try:
            msg = body["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as exc:
            log.warning("llm: malformed tool response: %s", exc)
            return None
        if not isinstance(msg, dict):
            return None
        return msg

    # ---- internals -------------------------------------------------------

    def _apply_reasoning(self, payload: dict[str, Any]) -> None:
        """Inject the reasoning/thinking controls.

        MiniMax M3 accepts `reasoning_effort` ("low"|"medium"|"high"). Empty
        / 0 values skip the field entirely so non-reasoning models don't
        reject the request.
        """
        if self._reasoning_effort:
            payload["reasoning_effort"] = self._reasoning_effort
        elif self._reasoning_max_tokens > 0:
            payload["reasoning"] = {"max_tokens": self._reasoning_max_tokens}

    def _post_with_retry(self, url: str, headers: dict, payload: dict) -> dict[str, Any]:
        attempts = self._max_retries + 1
        last_exc: Exception | None = None
        for attempt in range(attempts):
            try:
                return self._post_once(url, headers, payload)
            except (httpx.HTTPStatusError, httpx.TransportError) as exc:
                last_exc = exc
                if attempt == attempts - 1:
                    break
                if isinstance(exc, httpx.HTTPStatusError):
                    status = exc.response.status_code
                    if status < 500 and status != 429:
                        break
                wait = min(2 ** attempt, 8)
                log.warning(
                    "llm: transient failure (attempt %d/%d): %s; retrying in %ds",
                    attempt + 1,
                    attempts,
                    exc,
                    wait,
                )
                time.sleep(wait)
        raise LLMTransportError(str(last_exc) if last_exc else "unknown transport error")

    def _post_once(self, url: str, headers: dict, payload: dict) -> dict[str, Any]:
        merged = {**headers, **self._extra_headers}
        log.debug("llm: POST %s model=%s temperature=%.2f tools=%d reasoning=%s",
                  url, self._model, self._temperature,
                  len(payload.get("tools") or []),
                  payload.get("reasoning") or payload.get("reasoning_effort"))
        # ``httpx.Client`` isn't safe to share concurrently across threads
        # (mutable headers / connection state). The lock is held only for the
        # duration of the POST; everything else (JSON serialisation, retries)
        # runs outside the lock.
        with self._http_lock:
            resp = self._http.post(url, headers=merged, json=payload)
            resp.raise_for_status()
            body = resp.json()
        log.debug("llm: raw response: %s", json.dumps(body, default=str)[:2000])
        return body

    def close(self) -> None:
        """Close the underlying httpx connection pool. Idempotent."""
        try:
            self._http.close()
        except Exception:  # noqa: BLE001
            pass


def build_default_client() -> LLMClient | None:
    """Build a client from `Config` (env-driven). Returns None if not configured."""
    from app.config import Config

    cfg = Config()
    if not cfg.LLM_API_KEY or not cfg.LLM_BASE_URL or not cfg.LLM_MODEL:
        return None

    extra_headers: dict[str, str] = {}
    if cfg.LLM_APP_URL:
        extra_headers["HTTP-Referer"] = cfg.LLM_APP_URL
    if cfg.LLM_APP_NAME:
        extra_headers["X-Title"] = cfg.LLM_APP_NAME

    return OpenAICompatClient(
        base_url=cfg.LLM_BASE_URL,
        api_key=cfg.LLM_API_KEY,
        model=cfg.LLM_MODEL,
        timeout_s=cfg.LLM_TIMEOUT_S,
        max_retries=cfg.LLM_MAX_RETRIES,
        reasoning_effort=cfg.LLM_REASONING_EFFORT,
        reasoning_max_tokens=cfg.LLM_REASONING_MAX_TOKENS,
        extra_headers=extra_headers or None,
    )