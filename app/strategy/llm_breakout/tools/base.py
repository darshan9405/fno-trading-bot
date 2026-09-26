"""Tool protocol + shared helpers for the LLM agent loop.

Every tool the LLM can call exposes:
  - `name`        — stable string identifier (snake_case, matches the JSON
                    schema `function.name`)
  - `description` — natural-language purpose, surfaced to the LLM in the
                    tool definition's `description` field
  - `parameters`  — JSON-schema dict for the LLM's `function.parameters`
  - `run(args)`   — execute the tool. Return value MUST be a JSON-serialisable
                    dict (the agent loop wraps it as `tool_result` content
                    for the next LLM message). Never raise — return an
                    `{"error": "..."}` dict on failure so the agent loop
                    can surface the problem and continue iterating.

Tools are pure functions of their arguments plus injected context. Context
(dict) is passed to every tool at construction time so the LLM can stay
agnostic of broker wiring (e.g. indicator tool needs the per-instrument
candles, option chain tool needs the broker + today).
"""

from __future__ import annotations

from typing import Any, Protocol


class LLMTool(Protocol):
    name: str
    description: str
    parameters: dict[str, Any]

    def run(self, args: dict[str, Any]) -> dict[str, Any]:  # pragma: no cover - protocol
        ...


def schema(name: str, description: str, params: dict[str, Any]) -> dict[str, Any]:
    """Build the OpenAI-compat `tools[].function` schema dict."""
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": params,
        },
    }