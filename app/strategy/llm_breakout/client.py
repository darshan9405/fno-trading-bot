"""OpenAI-compatible chat client for the LLM breakout detector.

Talks to OpenRouter (or any OpenAI-compatible provider) over HTTPS. Uses
`httpx` for the HTTP layer. Returns the parsed JSON response dict — never
raises on transport errors; the caller (detector) translates transport
failures into an empty signal list.

OpenRouter-specific behaviour: app-attribution headers (`HTTP-Referer`,
`X-Title`) are sent only when configured via env (`OPENROUTER_APP_URL`,
`OPENROUTER_APP_NAME`); the rest of the request shape is identical to
plain OpenAI. BASE_URL defaults to OpenRouter's root in `app.config.Config`.

The client is injectable: tests substitute a stub via the `LLMClient`
constructor argument on the strategy class so the rest of the pipeline can
be exercised without a real API call.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Protocol

import httpx

log = logging.getLogger(__name__)


class LLMTransportError(Exception):
    """Raised on a non-retryable HTTP failure or after retries are exhausted."""


class LLMClient(Protocol):
    """Minimal interface every LLM client (real or stub) must implement."""

    def chat_json(self, system: str, user: str) -> dict[str, Any]:
        """Send the chat and return the parsed JSON body. Never raises."""
        ...


class OpenAICompatClient:
    """Concrete OpenAI-compatible chat-completions client."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        timeout_s: float = 30.0,
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
        # Provider-specific headers (OpenRouter app-attribution, etc.).
        # Merged AFTER the auth/content-type headers so callers can't override
        # them by accident.
        self._extra_headers: dict[str, str] = {
            str(k): str(v) for k, v in (extra_headers or {}).items()
        }

    def chat_json(self, system: str, user: str) -> dict[str, Any]:
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
        # Reasoning / chain-of-thought. OpenRouter auto-routes per model:
        # OpenAI-style -> `reasoning_effort`, Anthropic-style ->
        # `reasoning.max_tokens`. OpenRouter rejects requests that set BOTH
        # ("Only one of reasoning.effort and reasoning.max_tokens can be
        # specified"), so we send at most one. `reasoning_effort` wins if both
        # are configured. Empty/0 values skip the field entirely so
        # non-reasoning models don't reject the request.
        if self._reasoning_effort:
            payload["reasoning_effort"] = self._reasoning_effort
        elif self._reasoning_max_tokens > 0:
            payload["reasoning"] = {"max_tokens": self._reasoning_max_tokens}
        log.debug("llm: POST %s model=%s temperature=%.2f reasoning=%s reasoning_effort=%s",
                  url, self._model, self._temperature,
                  payload.get("reasoning"), payload.get("reasoning_effort"))
        return self._post_with_retry(url, headers, payload)

    def _post_with_retry(self, url: str, headers: dict, payload: dict) -> dict[str, Any]:
        attempts = self._max_retries + 1
        try:
            for attempt in range(attempts):
                try:
                    return self._post_once(url, headers, payload)
                except (httpx.HTTPStatusError, httpx.TransportError) as exc:
                    if attempt == attempts - 1:
                        raise LLMTransportError(str(exc)) from exc
                    if isinstance(exc, httpx.HTTPStatusError):
                        status = exc.response.status_code
                        if status < 500 and status != 429:
                            raise LLMTransportError(
                                f"non-retryable HTTP {status}: {exc.response.text[:200]}"
                            ) from exc
                    wait = min(2 ** attempt, 8)
                    log.warning(
                        "llm: transient failure (attempt %d/%d): %s; retrying in %ds",
                        attempt + 1,
                        attempts,
                        exc,
                        wait,
                    )
                    time.sleep(wait)
        except LLMTransportError:
            log.exception("llm: chat_json failed after retries")
            return {"_transport_error": True}
        except Exception as exc:  # noqa: BLE001
            log.exception("llm: unexpected chat_json failure: %s", exc)
            return {"_transport_error": True}

    def _post_once(self, url: str, headers: dict, payload: dict) -> dict[str, Any]:
        # Merge: caller-supplied auth/content-type headers are authoritative;
        # provider-specific extras (HTTP-Referer / X-Title) layer on top.
        merged = {**headers, **self._extra_headers}
        with httpx.Client(timeout=self._timeout_s) as client:
            resp = client.post(url, headers=merged, json=payload)
            resp.raise_for_status()
            body = resp.json()
        # Surface the full raw response (incl. any `reasoning_content` /
        # `reasoning` field) at DEBUG so operators can inspect what the model
        # actually said + thought, without polluting INFO logs.
        log.debug("llm: raw response: %s", json.dumps(body, default=str))
        try:
            content = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMTransportError(f"malformed response: missing choices[0].message.content: {exc}") from exc
        if not isinstance(content, str):
            raise LLMTransportError("message content is not a string")
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError as exc:
            raise LLMTransportError(f"message content is not valid JSON: {exc}") from exc
        # Stash the model's reasoning (chain-of-thought) alongside the parsed
        # payload so callers / logs can surface it. Non-reasoning models simply
        # don't populate `reasoning_content`.
        msg = body.get("choices", [{}])[0].get("message", {})
        if isinstance(msg, dict) and (msg.get("reasoning_content") or msg.get("reasoning")):
            parsed["_reasoning"] = msg.get("reasoning_content") or msg.get("reasoning")
        return parsed


def build_default_client() -> LLMClient | None:
    """Build a client from `Config` (env-driven). Returns None if not configured.

    When the configured BASE_URL is OpenRouter (the default), the optional
    `OPENROUTER_APP_URL` / `OPENROUTER_APP_NAME` env vars are forwarded as
    `HTTP-Referer` / `X-Title` headers. Off by default — only set the headers
    if the env vars are non-empty.
    """
    from app.config import Config

    cfg = Config()
    if not cfg.LLM_API_KEY or not cfg.LLM_BASE_URL or not cfg.LLM_MODEL:
        return None

    extra_headers: dict[str, str] = {}
    if cfg.OPENROUTER_APP_URL:
        extra_headers["HTTP-Referer"] = cfg.OPENROUTER_APP_URL
    if cfg.OPENROUTER_APP_NAME:
        extra_headers["X-Title"] = cfg.OPENROUTER_APP_NAME

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
