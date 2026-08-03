"""In-process BYOK chat engine backing the dashboard's ``/chat`` page.

Reuses the exact same tool definitions and tool functions the MCP server
(``mcp/server.py``) already exposes — :func:`_mcp_tool_schemas` sources its
JSON schemas from ``mcp.list_tools()`` (no second, hand-written copy) and
every tool call goes through ``mcp.call_tool()``, all **in-process**: no HTTP
hop to ``:8002``, no auth, because the dashboard and the MCP server share the
same trust boundary (the same machine, the same Parquet files on disk).

One turn runs a small ``pydantic_graph`` (Plan → [Explore → re-Plan, at most
once] → Execute → Synthesize) instead of handing a single ``pydantic-ai``
``Agent`` a live tool-calling loop. The model commits to a plan — which read
calls it needs — in one structured-output round with no tools involved;
``ExecuteNode`` dedupes that plan deterministically before anything runs, so
a weak model repeating itself in its own plan gets caught once, in code,
before a single real DuckDB round trip happens (rather than being caught
reactively after it already re-issued an identical call). The independent
reads then run concurrently. See ``pydantic_ai._agent_graph`` for the same
``BaseNode``/``GraphBuilder`` pattern pydantic-ai's own agent loop is built
on — this file follows the same shape.

The LLM call itself goes through ``pydantic-ai``, which normalizes tool-calling
across providers (native OpenAI/Anthropic/Google, plus anything speaking the
OpenAI-compatible wire format — Ollama, Databricks, self-hosted) so any
``provider``/``model``/``api_key``/``base_url`` combination the user picks
just works — "plug and play" happens by typing a different value into those
fields, never a code change.
"""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Annotated, Any, Literal, cast

import httpx
from mcp.types import CallToolResult
from pydantic import BaseModel, Field
from pydantic_ai import Agent, UsageLimits
from pydantic_ai.exceptions import UnexpectedModelBehavior, UsageLimitExceeded
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, TextPart
from pydantic_ai.models import Model
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.models.google import GoogleModel
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.anthropic import AnthropicProvider
from pydantic_ai.providers.google import GoogleProvider
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.usage import RunUsage
from pydantic_graph import BaseNode, End, Graph, GraphBuilder, GraphRunContext

from flashlight.dashboard.data import provider_label
from flashlight.gold.reader import distinct_values
from flashlight.lake.chat_turns import record_chat_turn
from flashlight.mcp.server import mcp
from flashlight.transform.catalog import current_catalog, discover_provider_groups

# ponytail: a plan round makes zero native tool calls, so a healthy model
# finishes in exactly 1 model request — this is headroom for pydantic-ai's own
# structured-output validation retries (a model returning malformed JSON), not
# a round budget. Set generously rather than tight: 3 proved too tight against
# a real Databricks gpt-oss-20b, which burned the budget on malformed
# structured output and surfaced "didn't get a final answer within this turn's
# round limit" on a perfectly ordinary question. _QuirkTransport absorbs
# *empty* rounds for free (they never reach pydantic-ai), but a malformed
# tool-call payload is a real request and does count here.
_OUTPUT_REQUEST_LIMIT = 8

# ponytail: ceilings against a runaway plan, not floors — raise if a real
# workflow needs a bigger single-turn plan or more filter-value lookups.
MAX_PLAN_STEPS = 12
MAX_EXPLORE_LOOKUPS = 6


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


def _data_window_line() -> str:
    """Ground the model in today's date and the charge months actually present.

    Regression fix: a model has no idea what day it is, so "last month"/"the
    previous month" is genuinely unanswerable to it — confirmed live, a
    Databricks gpt-oss-20b answered the already-unambiguous option "Total spend
    across AWS & Databricks for the previous month (default)" with *another*
    clarifying question ("if today is August 2026, do you mean July 2026?").
    That whole class of question is a grounding gap, not real ambiguity: state
    the current month and the real charge_month range and there's nothing left
    to ask. Sourced live from the published data (reusing the same
    gold.reader.distinct_values the list_dimension_values tool uses), so it
    can't drift.
    """
    today = datetime.now(UTC).date()
    lines = [f"Today is {today.isoformat()} (current month: {today.strftime('%Y-%m')})."]
    for view in current_catalog():
        if "charge_month" not in view.dimensions:
            continue
        try:
            months = [str(m) for m in distinct_values(view.name, "charge_month") if m is not None]
        except Exception:  # noqa: BLE001,S112 - see below
            # A view can be in the catalog but not queryable: the fixed groups
            # (shared/efficiency/...) are always catalogued, yet only published
            # once their data exists, so DuckDB raises a CatalogException for a
            # missing schema. Keep trying later views rather than giving up on
            # the first one — with only AWS ingested, shared.tco_by_cluster_month
            # fails but aws.monthly_bill has the real range. This whole line is
            # best-effort grounding: it must never break a turn, hence the broad
            # catch.
            continue
        if months:
            lines.append(
                f"Charge months present in the data: {min(months)} through {max(months)}. "
                "Resolve a relative window (last month, year to date) against these "
                "yourself — never ask the user which month they meant."
            )
            break
    return " ".join(lines)


def _catalog_line() -> str:
    """Ground the model in the exact metric views that exist, inline, instead of
    making it spend a round on list_metrics to discover the same thing —
    every turn was paying that round trip even though the catalog is small,
    static-shaped, and already computed server-side. Sourced live from
    current_catalog() (the same data list_metrics itself returns), so this
    can't drift: a new connector or GOLD view shows up on the very next turn,
    same as _connected_providers_line(). Dimensions/measures only, no prose
    description — describe_metric stays available as a plannable step for the
    rare view that needs the fuller text, without doubling this prompt for
    every view on every turn."""
    views = current_catalog()
    if not views:
        return ""
    lines = (
        f"- {v.name}: dimensions=[{', '.join(v.dimensions)}] measures=[{', '.join(v.measures)}]"
        for v in views
    )
    return "Available metric views:\n" + "\n".join(lines)


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
    options: list[str] = field(default_factory=list)  # from a ClarifyingQuestion, if asked


class ClarifyingQuestion(BaseModel):
    """Structured "I need more info" output — returned instead of a Plan when
    the request is genuinely ambiguous (see _PLAN_INSTRUCTIONS). Can come back
    from either PlanNode pass (before or after an explore round)."""

    question: str
    options: list[str] = Field(min_length=2, max_length=4)


# --- Plan steps -------------------------------------------------------------
#
# One typed model per plannable MCP tool, mirroring each tool's real
# signature in mcp/server.py exactly (param names, types, defaults) — not a
# single generic PlanStep(tool, args: dict[str, Any]). A free-form args dict
# gives the model weak structured-output guidance ("any JSON object") and
# lets bad args survive all the way to a {"error": ...} at execution time.
# Typed fields give the model real per-field guidance (the Field
# descriptions below are where the "use a known value, never guess" /
# "exactly one measure -> chartable" guidance lives, read directly off the
# schema the model is filling in) and make drift from the real MCP signature
# loud (a validation error) instead of silent (a dict key silently ignored).
# A test guards each step's field set against the real tool signature.


class QueryMetricStep(BaseModel):
    tool: Literal["query_metric"] = "query_metric"
    name: str = Field(description="Provider-scoped view name, e.g. 'aws.monthly_bill'.")
    limit: int = 200
    order_by: str | list[str] | None = None
    descending: bool = False
    filters: dict[str, Any] | None = Field(
        default=None,
        description="Equality filters; a list value means IN-match. Use a value "
        "already known or discovered via a list_dimension_values lookup — never guess one.",
    )
    measures: list[str] | None = Field(
        default=None,
        description="Pass exactly one measure for a chartable dimension+measure "
        "result; omit for every measure on the view (too wide to chart).",
    )


class RunSqlStep(BaseModel):
    tool: Literal["run_sql"] = "run_sql"
    sql: str = Field(
        description="A single SELECT/WITH statement against one provider's GOLD "
        "schema. Never JOIN/UNION/window across providers to combine or compute — "
        "plan one query_metric step per provider/period instead."
    )
    limit: int = 200


class ListDimensionValuesStep(BaseModel):
    tool: Literal["list_dimension_values"] = "list_dimension_values"
    name: str
    dimension: str
    limit: int = 500


class DescribeMetricStep(BaseModel):
    tool: Literal["describe_metric"] = "describe_metric"
    name: str


class ListOptimizationRulesStep(BaseModel):
    tool: Literal["list_optimization_rules"] = "list_optimization_rules"


class ListPolicyRulesStep(BaseModel):
    tool: Literal["list_policy_rules"] = "list_policy_rules"


PlanStep = Annotated[
    QueryMetricStep
    | RunSqlStep
    | ListDimensionValuesStep
    | DescribeMetricStep
    | ListOptimizationRulesStep
    | ListPolicyRulesStep,
    Field(discriminator="tool"),
]

# For tests: a field-set drift guard against each tool's real signature.
PLAN_STEP_MODELS: tuple[type[BaseModel], ...] = (
    QueryMetricStep,
    RunSqlStep,
    ListDimensionValuesStep,
    DescribeMetricStep,
    ListOptimizationRulesStep,
    ListPolicyRulesStep,
)


class Plan(BaseModel):
    """A committed, deduped-before-execution set of read-only tool calls — the
    model commits to this once, in one structured-output round, instead of
    deciding what to call next after seeing each result. ExecuteNode dedupes
    `steps` by (tool, args) before running anything, then runs the
    (independent) reads concurrently."""

    steps: list[PlanStep] = Field(default_factory=list, max_length=MAX_PLAN_STEPS)


class ExploreRequest(BaseModel):
    """Returned instead of a Plan when the model needs a filter *value* it
    doesn't already know (see _EXPLORE_INSTRUCTIONS) — query_metric/run_sql
    don't require this, but the model can't know a valid tag key or
    charge_month range without looking it up first, a genuine sequential
    dependency a flat Plan can't express. Resolved deterministically by
    ExploreNode (no LLM round to decide what to look up), then fed back into
    a second, must-commit PlanNode call."""

    lookups: list[ListDimensionValuesStep] = Field(min_length=1, max_length=MAX_EXPLORE_LOOKUPS)


_ROUND_LIMIT_MESSAGE = (
    "Didn't get a final answer within this turn's round limit — try rephrasing or asking again."
)

# ponytail: an empty round (see _QuirkTransport below) is a provider quirk, not
# progress — counting it against a model's own output-retry budget meant a
# flaky endpoint (confirmed on Databricks gpt-oss: it drops a round entirely
# under real conversational load) could burn that budget on nothing before a
# single real structured-output attempt happened. Capped separately so it
# can't stall a turn forever, but doesn't compete with pydantic-ai's own
# output-validation retries.
EMPTY_ROUND_RETRY_LIMIT = 3

_HARMONY_MARKER = re.compile(r"<\|.*$", re.DOTALL)


def _flatten_content_parts(content: object) -> str | None:
    """Normalize an OpenAI-compatible message's `content` into a plain string.
    Confirmed against a live Databricks gpt-oss response: `content` can arrive
    as a list of typed parts (e.g. `[{"type": "reasoning", "text": "..."}]`)
    instead of a plain string — pydantic-ai's OpenAI-compatible parser expects
    `content: str | None` per the real OpenAI wire contract, and choked on the
    list with a Pydantic serializer warning, silently treating the round as
    unusable and burning a retry on it instead of surfacing a clean error.
    A reasoning-only part list also isn't a real answer anyway — only
    "text"-typed parts count. Returns None (leave alone) if content wasn't a
    list to begin with."""
    if not isinstance(content, list):
        return None
    return "".join(
        p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"
    )


class _QuirkTransport(httpx.AsyncBaseTransport):
    """Wraps the real HTTP transport for every OpenAI-compatible provider
    (Ollama, Databricks, self-hosted/custom) and fixes three Databricks
    gpt-oss serving bugs at the wire level, before pydantic-ai or the OpenAI
    SDK ever parses a response:

    1. A round can come back with no content and no tool call at all (the
       model's real intent survives only in a reasoning trace, never a
       structured response) — silently retried here, re-issuing the same
       request, up to EMPTY_ROUND_RETRY_LIMIT times, before anything is
       returned upward. If every retry still comes back empty, the last
       (still-empty) response is returned as normal — pydantic-ai treats that
       as an output-validation failure and gives up after its own output-retry
       budget via UnexpectedModelBehavior, which run_turn catches.
    2. A tool call's name can leak a raw Harmony-format marker, e.g.
       "query_metric<|channel|>analysis" instead of "query_metric" — trimmed
       before the response is returned.
    3. `content` can arrive as a list of typed parts (e.g. a reasoning block)
       instead of a plain string (see _flatten_content_parts) — normalized
       before the "no content and no tool call" check above, so a
       reasoning-only round is correctly treated as empty and retried instead
       of passed through as unparseable.

    A no-op for a well-formed response, so wiring this into every
    OpenAI-compatible provider (not just Databricks) is harmless.
    """

    def __init__(self, inner: httpx.AsyncBaseTransport | None = None) -> None:
        self._inner = inner or httpx.AsyncHTTPTransport()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        for attempt in range(EMPTY_ROUND_RETRY_LIMIT + 1):
            response = await self._inner.handle_async_request(request)
            if response.status_code != 200:
                return response
            await response.aread()
            try:
                body = json.loads(response.content)
                message = body["choices"][0]["message"]
            except (json.JSONDecodeError, KeyError, IndexError, TypeError):
                return response  # not a shape we know how to inspect — pass through as-is
            flattened = _flatten_content_parts(message.get("content"))
            if flattened is not None:
                message["content"] = flattened
            is_empty = not message.get("content") and not message.get("tool_calls")
            if is_empty and attempt < EMPTY_ROUND_RETRY_LIMIT:
                continue
            for call in message.get("tool_calls") or []:
                name = call["function"]["name"]
                call["function"]["name"] = _HARMONY_MARKER.sub("", name) or name
            # response.content is already decompressed (httpx did that transparently
            # on .aread()) but response.headers still says e.g. "content-encoding:
            # gzip" for the *original* compressed bytes — carrying that header over
            # onto our fresh, uncompressed content.encode() made whatever reads this
            # response try to gunzip already-plain JSON and fail with "Error -3
            # while decompressing data: incorrect header check" (confirmed against a
            # real gzip-compressing endpoint; every local test fixture up to this
            # point never compressed its canned responses, so this never surfaced).
            # content-length is also stale for the new body size; httpx recomputes
            # it from the given content when omitted.
            headers = {
                k: v
                for k, v in response.headers.items()
                if k.lower() not in ("content-encoding", "content-length")
            }
            return httpx.Response(
                response.status_code,
                headers=headers,
                content=json.dumps(body).encode(),
                request=request,
            )
        raise AssertionError("unreachable: the loop always returns within the retry budget")

    async def aclose(self) -> None:
        await self._inner.aclose()


def _build_model(provider: str, model: str, api_key: str, base_url: str | None) -> Model:
    """Construct the right pydantic-ai Model/Provider pair for a BYOK
    provider+model+api_key+base_url combination typed into the settings
    dialog. Native providers (openai/anthropic/google) all accept a custom
    base_url too, for a proxy or gateway in front of them. Everything else
    (Ollama, Databricks, self-hosted/custom) speaks the OpenAI-compatible
    wire format, so one branch covers all of them — and is the one branch
    that gets the Databricks-quirk transport wired in, harmlessly for
    providers that never trigger it."""
    if provider == "openai":
        return OpenAIChatModel(model, provider=OpenAIProvider(api_key=api_key, base_url=base_url))
    if provider == "anthropic":
        return AnthropicModel(model, provider=AnthropicProvider(api_key=api_key, base_url=base_url))
    if provider == "google":
        return GoogleModel(model, provider=GoogleProvider(api_key=api_key, base_url=base_url))
    return OpenAIChatModel(
        model,
        provider=OpenAIProvider(
            api_key=api_key,
            base_url=base_url,
            http_client=httpx.AsyncClient(transport=_QuirkTransport()),
        ),
    )


def _dedup_key(name: str, kwargs: dict[str, Any]) -> str:
    """A stable key for "this exact tool call, these exact arguments" —
    argument order in the dict doesn't matter, only the values do."""
    return f"{name}:{json.dumps(kwargs, sort_keys=True, default=str)}"


async def _call_mcp_tool(name: str, kwargs: dict[str, Any]) -> Any:
    """Dispatch one MCP tool call in-process through the same mcp.call_tool()
    the real MCP server uses, unwrapping CallToolResult the same way a real
    client would."""
    try:
        result = await mcp.call_tool(name, kwargs)
    except Exception as exc:  # noqa: BLE001 - surfaced to the model, not the caller
        return {"error": str(exc)}
    if not isinstance(result, CallToolResult):
        # None of Flashlight's tools ask for elicited input — this branch is
        # unreachable in practice, but the SDK's return type isn't narrowed for us.
        return {"error": "tool requires interactive input, which chat cannot provide"}
    if result.structured_content is not None:
        return result.structured_content
    return "\n".join(getattr(block, "text", "") for block in result.content)


def _agent_model(state: ChatState) -> Model:
    return _build_model(state.provider, state.model, state.api_key, state.base_url)


async def _execute_step(step: PlanStep) -> Any:
    return await _call_mcp_tool(step.tool, step.model_dump(exclude={"tool"}))


def _step_to_tool_step(step: PlanStep, result: Any) -> ToolStep:
    args = step.model_dump(exclude={"tool"})
    if isinstance(result, dict):
        return ToolStep(
            name=step.tool, arguments=args, rows=result.get("rows"), error=result.get("error")
        )
    return ToolStep(name=step.tool, arguments=args)


_tool_schemas_cache: list[tuple[str, str, dict[str, Any]]] | None = None


async def _mcp_tool_schemas() -> list[tuple[str, str, dict[str, Any]]]:
    """(name, description, json_schema) for every plannable Flashlight MCP
    tool — excludes list_metrics (its data is inlined via _catalog_line()
    instead — offering it too just invites a plan to spend a step
    re-deriving what it was already told). Cached — mcp.list_tools() is
    static at runtime."""
    global _tool_schemas_cache
    if _tool_schemas_cache is None:
        tools = await mcp.list_tools()
        _tool_schemas_cache = [
            (t.name, t.description or "", t.input_schema) for t in tools if t.name != "list_metrics"
        ]
    return _tool_schemas_cache


_plan_tool_catalog_cache: str | None = None


async def _plan_tool_catalog() -> str:
    """Plain-text summary of the 6 plannable MCP tools, sourced from the same
    descriptions mcp/server.py already declares (via _mcp_tool_schemas). The
    per-field *shape* of each tool is duplicated above as a typed PlanStep
    model (a deliberate tradeoff — see the comment above QueryMetricStep),
    but the prose *description* of what each tool is for stays
    single-sourced from MCP. Cached — static at runtime."""
    global _plan_tool_catalog_cache
    if _plan_tool_catalog_cache is None:
        schemas = await _mcp_tool_schemas()
        lines = (f"- {name}: {description}" for name, description, _ in schemas)
        _plan_tool_catalog_cache = "Plannable tools:\n" + "\n".join(lines)
    return _plan_tool_catalog_cache


def _redact(text: str, secret: str) -> str:
    """Strip a literal occurrence of *secret* out of provider error text before
    it's shown to the user or logged — some providers (confirmed: OpenAI's
    AuthenticationError) echo the submitted API key straight back in their
    error message on a bad/invalid key."""
    return text.replace(secret, "[REDACTED]") if secret else text


def _root_cause(exc: BaseException) -> str:
    """Walk an exception's __cause__ chain to the deepest distinct message.
    Confirmed by reproducing a real connection failure: a network error comes
    back wrapped three levels deep (pydantic_ai.ModelAPIError ->
    openai.APIConnectionError -> httpx.ConnectError), and the outer two both
    just say "Connection error." — the actually actionable detail (e.g. a DNS
    failure) only shows up at the innermost level. str(exc) alone silently
    drops it."""
    messages = []
    node: BaseException | None = exc
    while node is not None:
        text = str(node)
        if text and text not in messages:
            messages.append(text)
        node = node.__cause__
    return ": ".join(messages)


# --- Plan-phase instructions -------------------------------------------------

_PLAN_INSTRUCTIONS = (
    "You are Flashlight's spend assistant, planning how to answer a question "
    "about the user's cloud billing data. Don't guess at a genuinely ambiguous "
    "request — if it could reasonably mean more than one thing (which provider "
    "or connection, which time window, which cost metric — net vs. list vs. "
    "billed, which entity), return a ClarifyingQuestion with 2-4 concrete "
    "options (your best-guess default listed first) instead of guessing. If "
    "the request is already clear, return a Plan directly — with an empty "
    "steps list if no new data is needed (a greeting, thanks, or a question "
    "already answered earlier in this conversation).\n\n"
    "Metric names are provider-scoped (e.g. \"aws.monthly_bill\", "
    "\"databricks.monthly_bill\") — there is no single metric with every "
    "provider's spend already combined. The \"shared\" group is Databricks TCO "
    "only (DBU vs. attributed AWS infra), not a general cross-provider total. "
    "For \"across all providers\"/\"total\" spend, or a computed comparison "
    "(month-over-month growth, which grew the most), plan one query_metric "
    "step per provider/period — the arithmetic happens in the final answer, "
    "never plan a run_sql step with a JOIN, UNION, or window function to "
    "combine or compute across providers yourself: run_sql has no correctness "
    "check on what you write, and a wrong GROUP BY or window frame produces a "
    "plausible-looking result that's silently wrong. Only plan a run_sql step "
    "when query_metric genuinely can't express the question (e.g. no view has "
    "the dimension you need), and keep it as simple as possible.\n\n"
    "When the user asks for a chart, graph, or trend, plan a query_metric step "
    "with measures=[the one relevant measure] (e.g. [\"net_cost\"]) so the "
    "result is one dimension + one measure — a chart renders automatically "
    "from that shape. Leaving measures unset returns every measure on the view "
    "(net/gross/credit/list/savings/...), which is too wide to chart."
)

_EXPLORE_INSTRUCTIONS = (
    "If answering needs a filter *value* you don't already know (a tag key, a "
    "sku_id, the valid charge_month range) — not a metric/dimension/measure "
    "name, all of which are already listed above — return an ExploreRequest "
    "with one list_dimension_values lookup per value you need, instead of "
    "guessing. You get exactly one exploration round this turn: after it, you "
    "must return a Plan or a ClarifyingQuestion, not another ExploreRequest."
)

_MUST_COMMIT_INSTRUCTIONS = (
    "You already used this turn's one exploration round — the values you "
    "asked for, if any were found, are listed below. Return a Plan now, or a "
    "ClarifyingQuestion if those values still leave the request ambiguous. Do "
    "not ask to explore again."
)

_ANSWERED_CLARIFICATION_INSTRUCTIONS = (
    "The user's message is them picking one of the options you just offered in "
    "your own clarifying question — the ambiguity is already resolved. Commit "
    "to a Plan now; asking anything further is not available to you this turn."
)

_SYNTHESIZE_INSTRUCTIONS = (
    "You are Flashlight's spend assistant. Answer the user's question using "
    "only the data gathered below — never invent a number that isn't in it. "
    "If a step errored, say so plainly rather than working around it "
    "silently. For a cross-provider or computed comparison (total, "
    "month-over-month growth, which grew the most), do the arithmetic "
    "yourself on the rows gathered per provider/period.\n\n"
    "Never draw a chart yourself: no ASCII art, no code block pretending to "
    "be a plot, no chart described in a markdown table — a chart renders "
    "automatically above your reply when the gathered data is one dimension "
    "+ one measure. State any other result in prose or a real markdown table."
)


def _plan_instructions(
    *,
    allow_explore: bool,
    allow_clarify: bool,
    explored: dict[str, tuple[ListDimensionValuesStep, Any]],
    tool_catalog: str,
) -> str:
    sections = [
        _PLAN_INSTRUCTIONS,
        _connected_providers_line(),
        _data_window_line(),
        _catalog_line(),
        tool_catalog,
    ]
    if allow_explore:
        sections.append(_EXPLORE_INSTRUCTIONS)
    else:
        if explored:
            findings = "\n".join(
                f"- {lookup.name}.{lookup.dimension}: "
                f"{result.get('values') if isinstance(result, dict) else result}"
                for lookup, result in explored.values()
            )
            sections.append(f"Discovered dimension values:\n{findings}")
        sections.append(_MUST_COMMIT_INSTRUCTIONS)
    if not allow_clarify:
        sections.append(_ANSWERED_CLARIFICATION_INSTRUCTIONS)
    return "\n\n".join(s for s in sections if s)


def _describe_step(step: ToolStep) -> str:
    if step.error:
        return f"- {step.name}({step.arguments}): error: {step.error}"
    return f"- {step.name}({step.arguments}): {step.rows if step.rows is not None else 'ok'}"


# --- Graph --------------------------------------------------------------


@dataclass
class ChatState:
    """Mutable state threaded through one turn's Plan -> [Explore -> re-Plan]
    -> Execute -> Synthesize graph run."""

    question: str
    history: list[ModelMessage]
    provider: str
    api_key: str
    model: str
    base_url: str | None
    allow_clarify: bool = True
    pending_lookups: list[ListDimensionValuesStep] | None = None
    explored: dict[str, tuple[ListDimensionValuesStep, Any]] = field(default_factory=dict)
    plan: Plan | None = None
    steps: list[ToolStep] = field(default_factory=list)
    usage: RunUsage = field(default_factory=RunUsage)


@dataclass
class PlanNode(BaseNode[ChatState, None, ChatTurnResult]):
    """One structured-output Agent.run(), no tools. Commits to a ClarifyingQuestion,
    an ExploreRequest (first pass only), or a Plan.

    ClarifyingQuestion is dropped from the offered output types entirely when
    ``ChatState.allow_clarify`` is False (the user's message is them clicking an
    option from our own last clarifying question). Enforced structurally, not
    just asked for in the prompt: a weak model was confirmed re-asking "which
    month?" right after the user picked "...for the previous month (default)",
    and a schema that has no room for a question can't produce one.
    """

    allow_explore: bool = True

    async def run(
        self, ctx: GraphRunContext[ChatState, None]
    ) -> ExploreNode | ExecuteNode | End[ChatTurnResult]:
        tool_catalog = await _plan_tool_catalog()
        instructions = _plan_instructions(
            allow_explore=self.allow_explore,
            allow_clarify=ctx.state.allow_clarify,
            explored=ctx.state.explored,
            tool_catalog=tool_catalog,
        )
        output_types: list[type[BaseModel]] = [Plan]
        if self.allow_explore:
            output_types.insert(0, ExploreRequest)
        if ctx.state.allow_clarify:
            output_types.insert(0, ClarifyingQuestion)
        agent = Agent(_agent_model(ctx.state), instructions=instructions, output_type=output_types)
        result = await agent.run(
            ctx.state.question,
            message_history=ctx.state.history,
            usage_limits=UsageLimits(request_limit=_OUTPUT_REQUEST_LIMIT),
        )
        ctx.state.usage.incr(result.usage)
        match result.output:
            case ClarifyingQuestion() as cq:
                return End(ChatTurnResult(text=cq.question, options=cq.options))
            case ExploreRequest() as request:
                ctx.state.pending_lookups = request.lookups
                return ExploreNode()
            case Plan() as plan:
                ctx.state.plan = plan
                return ExecuteNode()
            case _:
                # output_type is built from a runtime list, not a literal, so mypy
                # can't prove exhaustiveness here the way assert_never wants — this
                # is a genuine "should never happen per the Agent's output_type
                # contract" guard, not a reachable branch.
                raise AssertionError(f"unexpected plan output: {result.output!r}")


@dataclass
class ExploreNode(BaseNode[ChatState, None, ChatTurnResult]):
    """No LLM call. Dedupes and runs the requested list_dimension_values
    lookups concurrently, then hands back to a must-commit PlanNode."""

    async def run(self, ctx: GraphRunContext[ChatState, None]) -> PlanNode:
        assert ctx.state.pending_lookups is not None
        by_key: dict[str, ListDimensionValuesStep] = {}
        for lookup in ctx.state.pending_lookups:
            by_key.setdefault(_dedup_key(lookup.tool, lookup.model_dump(exclude={"tool"})), lookup)
        keys = list(by_key)
        outcomes = await asyncio.gather(*(_execute_step(by_key[key]) for key in keys))
        ctx.state.explored = {
            key: (by_key[key], outcome) for key, outcome in zip(keys, outcomes, strict=True)
        }
        ctx.state.pending_lookups = None
        return PlanNode(allow_explore=False)


@dataclass
class ExecuteNode(BaseNode[ChatState, None, ChatTurnResult]):
    """No LLM call. Dedupes Plan.steps by (tool, args) — reusing anything
    already fetched during an explore round — and runs the rest concurrently.
    This is the root-cause dedup: a repeated step in the committed plan never
    reaches DuckDB twice, dedeuped in code before execution rather than
    reactively after a duplicate call already happened."""

    async def run(self, ctx: GraphRunContext[ChatState, None]) -> SynthesizeNode:
        assert ctx.state.plan is not None
        by_key: dict[str, PlanStep] = {}
        results: dict[str, Any] = {}
        for key, (lookup, outcome) in ctx.state.explored.items():
            by_key[key] = lookup
            results[key] = outcome
        for step in ctx.state.plan.steps:
            by_key.setdefault(_dedup_key(step.tool, step.model_dump(exclude={"tool"})), step)

        to_run = [key for key in by_key if key not in results]
        if to_run:
            outcomes = await asyncio.gather(*(_execute_step(by_key[key]) for key in to_run))
            results.update(zip(to_run, outcomes, strict=True))

        ctx.state.steps = [_step_to_tool_step(by_key[key], results[key]) for key in by_key]
        return SynthesizeNode()


@dataclass
class SynthesizeNode(BaseNode[ChatState, None, ChatTurnResult]):
    """One Agent.run(), no tools, output_type=str. Writes the final answer
    from the gathered ToolStep rows/errors — charting is already decided at
    Plan time, this node only writes prose."""

    async def run(self, ctx: GraphRunContext[ChatState, None]) -> End[ChatTurnResult]:
        agent = Agent(_agent_model(ctx.state), instructions=_SYNTHESIZE_INSTRUCTIONS)
        gathered = (
            "\n".join(_describe_step(step) for step in ctx.state.steps) or "(no data was gathered)"
        )
        prompt = f"User question: {ctx.state.question}\n\nData gathered:\n{gathered}"
        result = await agent.run(
            prompt,
            message_history=ctx.state.history,
            usage_limits=UsageLimits(request_limit=_OUTPUT_REQUEST_LIMIT),
        )
        ctx.state.usage.incr(result.usage)
        return End(ChatTurnResult(text=result.output, steps=ctx.state.steps))


# ponytail: Graph/GraphBuilder are runtime-Generic (pydantic_graph.graph_builder
# uses typing_extensions' infer_variance=True TypeVars), but the installed mypy
# doesn't resolve that as subscriptable — left unparameterized rather than
# fighting a mypy/typing_extensions version gap; state_type/input_type/
# output_type below still give GraphBuilder everything it needs at runtime.
def _build_graph() -> Graph:
    g = GraphBuilder(
        name="chat_turn",
        state_type=ChatState,
        input_type=PlanNode,
        output_type=ChatTurnResult,
    )
    g.add(
        g.edge_from(g.start_node).to(PlanNode),
        g.node(PlanNode),
        g.node(ExploreNode),
        g.node(ExecuteNode),
        g.node(SynthesizeNode),
    )
    return g.build()


_graph = _build_graph()


async def run_turn(
    messages: list[ModelMessage],
    question: str,
    *,
    provider: str,
    api_key: str,
    model: str,
    base_url: str | None,
    session_id: str,
    answering_clarification: bool = False,
) -> ChatTurnResult:
    """Run one user turn to completion: Plan -> [Explore -> re-Plan, at most
    once] -> Execute -> Synthesize.

    Extends *messages* in place with exactly two new messages — the user
    question and the final answer — regardless of how many internal
    Plan/Explore/Execute/Synthesize steps ran this turn: none of this turn's
    planning/tool-call detail leaks into cross-turn history. Returns the
    final assistant text (or clarifying question) plus every tool call made
    along the way (so the UI can show what was actually queried, not just
    the model's prose summary of it), and logs exactly one row to
    ``meta/chat_turns/`` per call, with token usage summed across every LLM
    call this turn made.

    Set *answering_clarification* when *question* is the user picking one of the
    options from this engine's own previous ClarifyingQuestion — that resolves
    the ambiguity by construction, so this turn may not ask another one (see
    PlanNode).
    """
    state = ChatState(
        question=question,
        history=list(messages),
        provider=provider,
        api_key=api_key,
        model=model,
        base_url=base_url,
        allow_clarify=not answering_clarification,
    )

    def _log(result: ChatTurnResult) -> ChatTurnResult:
        record_chat_turn(
            turn_id=str(uuid.uuid4()),
            session_id=session_id,
            model=model,
            prompt_tokens=state.usage.input_tokens or None,
            completion_tokens=state.usage.output_tokens or None,
            total_tokens=state.usage.total_tokens or None,
            tool_call_count=len(result.steps),
            occurred_at=datetime.now(UTC),
        )
        return result

    try:
        # cast: _graph is left unparameterized (see _build_graph) so mypy can't
        # resolve .run()'s OutputT on its own — GraphBuilder(output_type=ChatTurnResult)
        # already pins this at runtime.
        result = cast(ChatTurnResult, await _graph.run(inputs=PlanNode(), state=state))
    except (UsageLimitExceeded, UnexpectedModelBehavior):
        # ponytail: two distinct pydantic-ai exceptions both mean "didn't get
        # a real structured output within budget" — UsageLimitExceeded is
        # _OUTPUT_REQUEST_LIMIT exhausted; a model stuck returning
        # invalid/empty output (e.g. every _QuirkTransport retry still empty)
        # hits pydantic-ai's own output-retry budget first and raises
        # UnexpectedModelBehavior instead. Same user-facing story either way.
        return _log(ChatTurnResult(text=_ROUND_LIMIT_MESSAGE))
    except Exception as exc:  # noqa: BLE001 - bad key/base_url/network: expected, not exceptional
        return _log(ChatTurnResult(text=f"Request failed: {_redact(_root_cause(exc), api_key)}"))

    messages.append(ModelRequest.user_text_prompt(question))
    messages.append(ModelResponse(parts=[TextPart(result.text)]))
    return _log(result)
