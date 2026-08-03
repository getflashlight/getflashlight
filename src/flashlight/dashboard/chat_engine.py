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
import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import litellm
from mcp.types import CallToolResult

from flashlight.dashboard.data import provider_label
from flashlight.lake.chat_turns import record_chat_turn
from flashlight.mcp.server import mcp
from flashlight.transform.catalog import discover_provider_groups

# ponytail: fixed round cap against a misbehaving/looping model — raise if a
# real workflow needs deeper tool-call chains.
MAX_TOOL_ROUNDS = 4

# A lighter, answer-oriented take on the "grill-me" pattern (interview before
# acting): ask when a question is genuinely ambiguous, but — unlike grill-me's
# plan-only interviews — still answer outright once the request is clear,
# since most spend questions aren't ambiguous and a chat that always
# interrogates first is just annoying to use.
SYSTEM_PROMPT = (
    "You are Flashlight's spend assistant, answering questions about the "
    "user's cloud billing data with the tools below. Don't guess at a "
    "genuinely ambiguous request — if it could reasonably mean more than one "
    "thing (which provider or connection, which time window, which cost "
    "metric — net vs. list vs. billed, which entity), call "
    "ask_clarifying_question with 2-4 concrete options (your best-guess "
    "default listed first) instead of asking in plain text — the user picks "
    "one with a click rather than typing a reply. If the request is already "
    "clear, skip it and answer directly.\n\n"
    "When the user asks for a chart, graph, or trend, call query_metric with "
    "measures=[the one relevant measure] (e.g. [\"net_cost\"]) so the result is "
    "one dimension + one measure — a chart renders automatically above your "
    "reply from that data. Leaving measures unset returns every measure on the "
    "view (net/gross/credit/list/savings/...), which is too wide to chart. "
    "Never draw a chart yourself: no ASCII art, no code block pretending to be "
    "a plot, no chart described in a markdown table. If the shape genuinely "
    "isn't chartable (a single number, or you need more than one measure), "
    "just state the numbers in prose or a real markdown table instead.\n\n"
    "Metric names are provider-scoped (e.g. \"aws.monthly_bill\", "
    "\"databricks.monthly_bill\") — there is no single metric with every "
    "provider's spend already combined. The \"shared\" group is Databricks TCO "
    "only (DBU vs. attributed AWS infra), not a general cross-provider total. "
    "For \"across all providers\"/\"total\" spend, or a computed comparison "
    "(month-over-month growth, which grew the most), call query_metric once "
    "per provider/period and do the arithmetic yourself on the rows you get "
    "back — never write a run_sql query with a join, UNION, or window "
    "function to do that combining/computing for you. run_sql has no "
    "correctness check on what you write: a wrong GROUP BY or window frame "
    "produces a plausible-looking chart that's silently wrong, and the user "
    "can't tell without reading the SQL themselves. Only reach for run_sql "
    "when query_metric genuinely can't express the question (e.g. no view "
    "has the dimension you need), and keep it as simple as possible."
)


def _connected_providers_line() -> str:
    """Ground the model in which providers are *actually* connected — otherwise
    it pattern-matches on common cloud names from training data (confirmed:
    offering "GCP"/"Azure" as clarifying-question options in an instance that
    only has AWS and Databricks). Sourced the same way the nav sidebar is
    (discover_provider_groups reads gold/ live), so it can't drift out of date."""
    groups = discover_provider_groups()
    if not groups:
        return "No providers are connected yet — say so if asked about spend."
    names = ", ".join(provider_label(g) for g in groups)
    return f"Connected providers: {names}. Never mention or offer any other provider."


def _ensure_system_prompt(messages: list[dict[str, Any]]) -> None:
    """Keep the system message at the head of the conversation in sync with
    ground-truth connected providers — messages accumulates across turns in the
    same browser tab, and a connection can be added mid-session."""
    content = f"{SYSTEM_PROMPT}\n\n{_connected_providers_line()}"
    if messages and messages[0].get("role") == "system":
        messages[0]["content"] = content
    else:
        messages.insert(0, {"role": "system", "content": content})

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
    options: list[str] = field(default_factory=list)  # from ask_clarifying_question, if asked


# A synthetic tool, not one of MCP's — chat-only, so it lives here rather than
# in mcp/server.py (other MCP consumers, e.g. Claude Desktop, have their own
# way to ask the user something; this one is specifically "render option chips
# in Flashlight's chat UI"). The model calls it instead of asking a question in
# plain text, giving the UI structured options instead of prose to parse.
_ASK_CLARIFYING_QUESTION_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "ask_clarifying_question",
        "description": (
            "Ask the user one short clarifying question with 2-4 concrete answer "
            "options they can pick with a single click, instead of asking in your "
            "reply text. List your best-guess default option first."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "question": {"type": "string"},
                "options": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 2,
                    "maxItems": 4,
                },
            },
            "required": ["question", "options"],
        },
    },
}


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


# A serving layer is supposed to fully parse a reasoning model's response
# before an OpenAI-compatible API ever returns it to us. Databricks' gpt-oss
# endpoints have been observed leaking a raw marker into a tool call's name —
# this guard exists for that one bug, not a general parser.
_TOOL_NAME_ARTIFACT = re.compile(r"<\|.*$", re.DOTALL)


def _normalize_provider_quirks(message: Any) -> None:
    """Mutates *message* in place: a tool call's name can arrive as e.g.
    "query_metric<|channel|>analysis" instead of "query_metric" (confirmed on
    Databricks gpt-oss endpoints) — trim to the clean prefix so dispatch
    matches. A no-op for a well-formed response."""
    for call in getattr(message, "tool_calls", None) or []:
        call.function.name = _TOOL_NAME_ARTIFACT.sub("", call.function.name) or call.function.name


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
    tools = [*await tool_schemas(), _ASK_CLARIFYING_QUESTION_TOOL]
    _ensure_system_prompt(messages)
    total_prompt = total_completion = 0
    tool_call_count = 0
    steps: list[ToolStep] = []

    def _log(text: str, options: list[str] | None = None) -> ChatTurnResult:
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
        return ChatTurnResult(text=text, steps=steps, options=options or [])

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
        _normalize_provider_quirks(message)
        tool_calls = getattr(message, "tool_calls", None)
        if not tool_calls:
            if message.content:
                return _log(message.content)
            # No content AND no tool call: the provider dropped this round
            # entirely rather than returning a real answer or a tool call
            # (confirmed on Databricks gpt-oss endpoints — its reasoning trace
            # shows real intent, e.g. "call query_metric...", that never made
            # it into a structured response). That reasoning trace isn't a
            # considered answer, so retry the round instead of showing it.
            continue

        # A plain role/content/tool_calls dict, not message.model_dump(): litellm's
        # Message object carries extra fields (e.g. thinking_blocks, reasoning_content)
        # that some OpenAI-compatible backends (confirmed: Databricks Model Serving)
        # strictly reject as unknown fields when they come back in the next request.
        assistant_message: dict[str, Any] = {"role": "assistant", "content": message.content}
        if tool_calls:
            assistant_message["tool_calls"] = [call.model_dump() for call in tool_calls]
        messages.append(assistant_message)

        clarify_call = next(
            (c for c in tool_calls if c.function.name == "ask_clarifying_question"), None
        )
        if clarify_call is not None:
            # Keep history structurally valid (every tool_call in this round needs a
            # matching tool-role reply) even though we're aborting the round to ask —
            # a mixed round (data tool + clarify in the same round) shouldn't happen
            # per the system prompt, but isn't relied upon not to.
            for call in tool_calls:
                placeholder = (
                    "Waiting for the user's answer."
                    if call is clarify_call
                    else "Skipped — the assistant asked a clarifying question instead."
                )
                messages.append({"role": "tool", "tool_call_id": call.id, "content": placeholder})
            tool_call_count += len(tool_calls)
            try:
                clarify_args = json.loads(clarify_call.function.arguments or "{}")
            except json.JSONDecodeError:
                clarify_args = {}
            options = [str(o) for o in (clarify_args.get("options") or []) if str(o).strip()]
            return _log(str(clarify_args.get("question", "")), options=options)

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

    return _log(
        "Didn't get a final answer within this turn's round limit — try rephrasing or asking again."
    )
