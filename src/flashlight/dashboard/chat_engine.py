"""In-process BYOK chat engine backing the dashboard's ``/chat`` page.

Reuses the exact same tool definitions and tool functions the MCP server
(``mcp/server.py``) already exposes — :func:`tool_schemas` sources its JSON
schemas from ``mcp.list_tools()`` (no second, hand-written copy) and each tool
call goes through ``mcp.call_tool()``, all **in-process**: no HTTP hop to
``:8002``, no auth, because the dashboard and the MCP server share the same
trust boundary (the same machine, the same Parquet files on disk).

The LLM call itself goes through ``litellm``, which normalizes tool-calling
across providers (OpenAI, Anthropic's native protocol, Gemini, self-hosted
gateways, ...) so any ``model``/``api_key``/``base_url`` combination the user
pastes in just works — "plug and play" happens by typing a different value
into those fields, never a code change.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import litellm
from mcp.types import CallToolResult

from flashlight.lake.chat_turns import record_chat_turn
from flashlight.mcp.server import mcp

# ponytail: fixed round cap against a misbehaving/looping model — raise if a
# real workflow needs deeper tool-call chains.
MAX_TOOL_ROUNDS = 4

_tool_schemas_cache: list[dict[str, Any]] | None = None


@dataclass(frozen=True)
class ToolStep:
    """One tool call made during a turn — surfaced to the UI for transparency
    into what was actually queried, not just the LLM's prose summary of it."""

    name: str
    arguments: dict[str, Any]
    rows: list[dict[str, Any]] | None = None  # only query_metric/run_sql populate this
    error: str | None = None


@dataclass(frozen=True)
class ChatTurnResult:
    text: str
    steps: list[ToolStep] = field(default_factory=list)


def _redact(text: str, secret: str) -> str:
    """Strip a literal occurrence of *secret* out of provider error text before
    it's shown to the user or logged — some providers (confirmed: OpenAI's
    AuthenticationError) echo the submitted API key straight back in their
    error message on a bad/invalid key."""
    return text.replace(secret, "[REDACTED]") if secret else text


async def _acompletion(**kwargs: Any) -> Any:
    """Thin wrapper around ``litellm.acompletion`` — gives tests a stable,
    module-owned attribute to monkeypatch instead of reaching into a
    third-party module."""
    return await litellm.acompletion(**kwargs)


async def tool_schemas() -> list[dict[str, Any]]:
    """OpenAI-style tool definitions for every Flashlight MCP tool, sourced from
    FastMCP's own registry — MCP stays the one place a tool is described."""
    global _tool_schemas_cache
    if _tool_schemas_cache is None:
        tools = await mcp.list_tools()
        _tool_schemas_cache = [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description or "",
                    "parameters": t.input_schema,
                },
            }
            for t in tools
        ]
    return _tool_schemas_cache


async def _call_tool(name: str, arguments: dict[str, Any]) -> str:
    """Invoke a Flashlight tool in-process, returning its result as the string
    for a ``role: "tool"`` message. Never raises — a hallucinated tool name or a
    bad argument becomes an error string the model can see and react to."""
    try:
        result = await mcp.call_tool(name, arguments)
    except Exception as exc:  # noqa: BLE001 - surfaced to the model, not the caller
        return json.dumps({"error": str(exc)})
    if not isinstance(result, CallToolResult):
        # None of Flashlight's tools ask for elicited input — this branch is
        # unreachable in practice, but the SDK's return type isn't narrowed for us.
        return json.dumps({"error": "tool requires interactive input, which chat cannot provide"})
    if result.structured_content is not None:
        return json.dumps(result.structured_content)
    return "\n".join(getattr(block, "text", "") for block in result.content)


async def run_turn(
    messages: list[dict[str, Any]],
    *,
    api_key: str,
    model: str,
    base_url: str | None,
    session_id: str,
) -> ChatTurnResult:
    """Run one user turn to completion, including any tool-calling round trips.

    Appends the assistant's replies (and any tool results) onto *messages* in
    place, returns the final assistant text plus every tool call made along
    the way (so the UI can show what was actually queried, not just the
    model's prose summary of it), and logs exactly one row to
    ``meta/chat_turns/`` per call — regardless of how many tool-calling round
    trips happened underneath — with token usage accumulated across all of them.
    """
    tools = await tool_schemas()
    total_prompt = total_completion = 0
    tool_call_count = 0
    steps: list[ToolStep] = []

    def _log(text: str) -> ChatTurnResult:
        record_chat_turn(
            turn_id=str(uuid.uuid4()),
            session_id=session_id,
            model=model,
            prompt_tokens=total_prompt or None,
            completion_tokens=total_completion or None,
            total_tokens=(total_prompt + total_completion) or None,
            tool_call_count=tool_call_count,
            occurred_at=datetime.now(UTC),
        )
        return ChatTurnResult(text=text, steps=steps)

    for _ in range(MAX_TOOL_ROUNDS):
        try:
            response = await _acompletion(
                model=model,
                api_key=api_key,
                api_base=base_url or None,
                messages=messages,
                tools=tools,
            )
        except Exception as exc:  # noqa: BLE001 - bad key/base_url/network: expected, not exceptional
            return _log(f"Request failed: {_redact(str(exc), api_key)}")

        usage = getattr(response, "usage", None)
        if usage is not None:
            total_prompt += usage.prompt_tokens or 0
            total_completion += usage.completion_tokens or 0

        message = response.choices[0].message
        tool_calls = getattr(message, "tool_calls", None)
        if not tool_calls:
            return _log(message.content or "")

        messages.append(message.model_dump())
        for call in tool_calls:
            tool_call_count += 1
            try:
                args = json.loads(call.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            result_text = await _call_tool(call.function.name, args)
            messages.append({"role": "tool", "tool_call_id": call.id, "content": result_text})
            try:
                parsed = json.loads(result_text)
            except json.JSONDecodeError:
                parsed = {}
            steps.append(
                ToolStep(
                    name=call.function.name,
                    arguments=args,
                    rows=parsed.get("rows") if isinstance(parsed, dict) else None,
                    error=parsed.get("error") if isinstance(parsed, dict) else None,
                )
            )

    return _log("Reached the tool-call limit for this turn — try a narrower question.")
