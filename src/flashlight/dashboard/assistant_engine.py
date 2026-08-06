"""In-process BYOK assistant engine backing the dashboard's ``/assistant`` page.

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
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Annotated, Any, Literal, cast

import httpx
from mcp.types import CallToolResult
from pydantic import BaseModel, Field, field_validator, model_validator
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

from flashlight.core.logging import get_logger
from flashlight.dashboard.answer_caption import caption_for
from flashlight.dashboard.data import provider_label
from flashlight.gold.reader import distinct_values
from flashlight.lake.assistant_turns import record_assistant_turn
from flashlight.mcp.server import mcp
from flashlight.transform.catalog import current_catalog, discover_provider_groups

logger = get_logger(__name__)

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

# How many times a node re-asks after its structured output fails validation.
# pydantic-ai's default is 1, which a live Databricks gpt-oss-20b blew through
# on 3 of 4 identical runs ("Exceeded maximum output retries (1)") — one
# malformed plan and the whole turn died. _normalize_step now absorbs the
# specific malformation that caused those, so this is headroom for the next
# unknown one rather than the primary fix.
_OUTPUT_RETRIES = 3

# ponytail: ceilings against a runaway plan, not floors — raise if a real
# workflow needs a bigger single-turn plan or more filter-value lookups.
MAX_PLAN_STEPS = 12
MAX_EXPLORE_LOOKUPS = 6


def _connected_providers_line() -> str:
    """Ground the model in which providers are *actually* connected — otherwise
    it pattern-matches on common cloud names from training data (confirmed:
    offering "GCP"/"Azure" as clarifying-question options in an instance that
    only has AWS and Databricks). Sourced the same way the nav sidebar is
    (discover_provider_groups reads gold/ live), so it can't drift out of date.

    Each name is paired with its metric group, because the two aren't always the same
    string (data.provider_label overrides "AWS" to "AWS Redshift" — the bill Flashlight
    ingests is Redshift-scoped) and the metric names the model must call are group-
    prefixed: it needs "AWS Redshift" for prose and "aws" for `aws.monthly_bill`."""
    groups = discover_provider_groups()
    if not groups:
        return "No providers are connected yet — say so if asked about spend."
    names = ", ".join(f'{provider_label(g)} (metric group "{g}")' for g in groups)
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
            # (see catalog.FIXED_GROUPS) are always catalogued, yet only
            # published once their data exists, so DuckDB raises a CatalogException
            # for a missing schema. Keep trying later views rather than giving up on
            # the first one — with only AWS cost ingested, efficiency.waste_record
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
    # How the model asked for these rows to be drawn, if it did (see ChartSpec).
    # Validated against the real columns by the view before it's used.
    chart: ChartSpec | None = None


@dataclass(frozen=True)
class AssistantTurnResult:
    text: str
    steps: list[ToolStep] = field(default_factory=list)
    options: list[str] = field(default_factory=list)  # from a ClarifyingQuestion, if asked
    # The model's own reasoning traces for this turn, when the provider sent any
    # (see _split_content_parts). Surfaced collapsed in the UI — mainly so a
    # turn that produced no answer still shows what the model was trying to do.
    reasoning: list[str] = field(default_factory=list)


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


class ChartSpec(BaseModel):
    """How to draw a step's rows, declared by the model rather than guessed from
    the returned shape.

    The shape alone can't carry intent: "spend year to date by service" and
    "the monthly trend" return the exact same columns (service_name,
    charge_month, net_cost) but want service on x and month on x respectively.
    Inferring from rows produced a 39-bar chart labelled
    "Networking · NETWORKING · 2026-07-01" for the first — not a wrong palette,
    a wrong question answered. The model already knows both the intent and the
    view's columns (the catalog is in its prompt), so it says so here; it rides
    on the plan it was emitting anyway, costing no extra model call.

    Validated against the columns actually returned before use, with the
    row-shape inference as the fallback — so a wrong or absent chart degrades to
    today's behaviour instead of breaking the answer.
    """

    kind: Literal["bar", "line", "stacked_bar"] = Field(
        default="bar",
        description="'line' for a trend over time; 'stacked_bar' when a second "
        "dimension breaks each bar down (spend per service split by month); "
        "'bar' for a plain ranking.",
    )
    x: str = Field(description="The dimension column to put on the x axis.")
    series: str | None = Field(
        default=None,
        description="A second dimension to split each x value by (the stack or "
        "line colour). Leave null when the result has only one dimension.",
    )


# The placeholders a SummarySpec may reference. Every one is computed from the rows
# by answer_caption.facts_for, so the model chooses wording and code supplies every
# figure. Kept as a literal list in the field description because that description is
# the only place the model learns the vocabulary.
_SUMMARY_PLACEHOLDERS = (
    "{total} {measure} {count} {dimension} {rows} "
    "{first_period} {last_period} {first_value} {last_value} {periods} {change_pct} "
    "{top_name} {top_value} {top_share}"
)


class SummarySpec(BaseModel):
    """One sentence answering the question, with every number left to code.

    Same bargain as ChartSpec, for prose instead of a chart: the model knows the
    intent and the view's columns, so it says how to phrase the answer while it's
    emitting the plan anyway — and the *figures* are filled in afterwards from the
    rows by ``answer_caption``. That removes the entire Synthesize round trip, which
    on real captured turns was 2.8-7.1s spent restating a table the UI had already
    drawn (and the single largest share of a turn's wall clock).

    The reason this is worth a schema rather than just letting the model write the
    answer: a sentence assembled from ``facts_for`` **cannot contain a figure that
    isn't summed from the returned rows**. A model restating 200 rows of currency
    can transpose a digit; a template can't. So this keeps the one thing synthesis
    was good at (wording that fits the question asked) and drops the one thing it
    was bad at (being trusted with the numbers).

    Optional and self-correcting: no spec, an unknown placeholder, or a result the
    facts don't cover, and the answer falls back to a fixed assembly and then to
    real synthesis — the ChartSpec -> shape-inference -> table ladder again.
    """

    sentence: str = Field(
        description="One sentence answering the user's question, using only these "
        f"placeholders: {_SUMMARY_PLACEHOLDERS}. They are substituted with figures "
        "computed from the rows, so never write a number yourself. Example: "
        "'Spend rose from {first_value} in {first_period} to {last_value} in "
        "{last_period}, with {top_name} the largest at {top_share} of the total.' "
        "Set this whenever one query_metric step answers the question on its own; "
        "leave it unset for a multi-step comparison."
    )

    @model_validator(mode="before")
    @classmethod
    def _accept_a_bare_sentence(cls, value: Any) -> Any:
        """Take ``summary: "..."`` as well as ``summary: {"sentence": "..."}``.

        A single-field object is the shape a weak model most often flattens — the
        same liberality, for the same reason, as ``_normalize_step`` below. Rejecting
        it would cost the whole optimization on exactly the models that need it most.
        """
        return {"sentence": value} if isinstance(value, str) else value


class QueryMetricStep(BaseModel):
    tool: Literal["query_metric"] = "query_metric"
    name: str = Field(description="Provider-scoped view name, e.g. 'aws.monthly_bill'.")
    chart: ChartSpec | None = Field(
        default=None,
        description="How to visualise these rows. Set it whenever the user asks "
        "to see, chart, graph, plot or visualise something, or asks for a trend "
        "or breakdown; leave null when the answer is prose or a single number.",
    )
    summary: SummarySpec | None = Field(
        default=None,
        description="How to word the answer, with the figures filled in from the "
        "rows. Set it whenever this one step answers the question; leave null when "
        "several steps have to be compared.",
    )
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


_PLAN_TOOL_NAMES = frozenset(
    str(m.model_fields["tool"].default) for m in PLAN_STEP_MODELS  # noqa: SLF001 - our own models
)
# Keys a model plausibly nests a step's arguments under instead of inlining them.
_ARG_WRAPPER_KEYS = ("args", "arguments", "parameters", "params", "input")


def _normalize_step(raw: Any) -> Any:
    """Coerce the shapes models *actually* emit for a plan step into the flat,
    ``tool``-discriminated shape PlanStep declares.

    Confirmed against a live Databricks gpt-oss-20b, which failed 3 of 4
    identical runs with `Unable to extract tag using discriminator 'tool'` /
    `steps.0.query_metric.name Field required` because it nests the arguments
    under the tool's own name rather than inlining them next to a ``tool`` tag::

        {"query_metric": {"name": "aws.spend_by_service_month", ...}}
        {"tool": "query_metric", "query_metric": {"name": ..., ...}}
        {"tool": "query_metric", "args": {"name": ..., ...}}

    All three mean the same call, so accepting them is strictly better than
    burning a retry and then failing the turn. Deliberately Postel's law: the
    internal type stays strict (a real discriminated union, exhaustively matched
    in _execute_step) while parsing is liberal about how it arrived. Anything
    unrecognized is passed straight through so pydantic still raises its own
    clear validation error rather than this silently inventing a step."""
    if not isinstance(raw, dict):
        return raw
    step: dict[str, Any] = dict(raw)
    tool = step.get("tool")
    if not isinstance(tool, str) or tool not in _PLAN_TOOL_NAMES:
        # No usable tag: adopt the single tool-named key as the tag, e.g.
        # {"query_metric": {...}} -> {"tool": "query_metric", ...}.
        named = [k for k in step if k in _PLAN_TOOL_NAMES]
        if len(named) != 1:
            return raw
        tool = named[0]
        step["tool"] = tool
    # Tag present (now or already) — hoist arguments out of any nested wrapper,
    # whether keyed by the tool's own name or a generic "args"-style key.
    for wrapper in (tool, *_ARG_WRAPPER_KEYS):
        nested = step.get(wrapper)
        if isinstance(nested, dict):
            step.pop(wrapper)
            # Explicit outer fields win over the nested copy: if the model wrote
            # both, the flat one is the one it committed to last.
            step = {**nested, **step}
    return step


class Plan(BaseModel):
    """A committed, deduped-before-execution set of read-only tool calls — the
    model commits to this once, in one structured-output round, instead of
    deciding what to call next after seeing each result. ExecuteNode dedupes
    `steps` by (tool, args) before running anything, then runs the
    (independent) reads concurrently."""

    steps: list[PlanStep] = Field(default_factory=list, max_length=MAX_PLAN_STEPS)

    @field_validator("steps", mode="before")
    @classmethod
    def _accept_nested_steps(cls, value: Any) -> Any:
        return [_normalize_step(s) for s in value] if isinstance(value, list) else value


class ExploreRequest(BaseModel):
    """Returned instead of a Plan when the model needs a filter *value* it
    doesn't already know (see _EXPLORE_INSTRUCTIONS) — query_metric/run_sql
    don't require this, but the model can't know a valid tag key or
    charge_month range without looking it up first, a genuine sequential
    dependency a flat Plan can't express. Resolved deterministically by
    ExploreNode (no LLM round to decide what to look up), then fed back into
    a second, must-commit PlanNode call."""

    lookups: list[ListDimensionValuesStep] = Field(min_length=1, max_length=MAX_EXPLORE_LOOKUPS)

    @field_validator("lookups", mode="before")
    @classmethod
    def _accept_nested_lookups(cls, value: Any) -> Any:
        return [_normalize_step(s) for s in value] if isinstance(value, list) else value


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


@dataclass
class TurnTiming:
    """Where one turn spent its wall clock, for the ``meta/assistant_turns/`` log.

    Timed at the graph-node boundary rather than around the HTTP call: a Plan or
    Synthesize node does nothing but build a prompt and await the model, so
    ``plan_ms``/``synthesize_ms`` *are* the LLM cost to within the prompt-building
    time, and measuring there needs no extra layer in the request path. (For a
    finer breakdown — per-HTTP-round-trip timings, the provider SDK's own retry
    backoff — use ``scripts/profile_assistant_turn.py``, which wraps the
    transport for a one-off profiling run.)

    Nodes can run more than once in a turn (the empty-plan re-ask, the
    post-explore re-plan, the data-failure re-plan), so node times accumulate and
    ``plan_passes`` records how many.
    """

    node_ms: dict[str, float] = field(default_factory=dict)
    plan_passes: int = 0
    # Rounds _QuirkTransport re-issued because the provider returned neither
    # content nor a tool call. Invisible everywhere else — pydantic-ai never sees
    # them — yet each is a full round trip, so a turn can spend most of its life
    # here and look merely slow.
    empty_round_retries: int = 0
    outcome: str = "error"  # overwritten wherever a real result is built
    # "caption" when the answer was computed from the rows and the Synthesize LLM
    # call was skipped entirely, "model" when it wasn't. The column that makes the
    # saving measurable on /usage rather than asserted.
    answer_source: str | None = None

    def add(self, node: str, ms: float) -> None:
        self.node_ms[node] = self.node_ms.get(node, 0.0) + ms

    def columns(
        self, *, duration_ms: float, llm_requests: int
    ) -> dict[str, float | int | str | None]:
        """The latency columns of :data:`ASSISTANT_TURN_SCHEMA` for this turn."""
        return {
            "duration_ms": round(duration_ms, 1),
            "plan_ms": round(self.node_ms.get("PlanNode", 0.0), 1),
            "explore_ms": round(self.node_ms.get("ExploreNode", 0.0), 1),
            "execute_ms": round(self.node_ms.get("ExecuteNode", 0.0), 1),
            "synthesize_ms": round(self.node_ms.get("SynthesizeNode", 0.0), 1),
            "llm_request_count": llm_requests,
            "plan_pass_count": self.plan_passes,
            "empty_round_retries": self.empty_round_retries,
            "outcome": self.outcome,
            "answer_source": self.answer_source,
        }


# The current turn's timing, for the one place that can't reach AssistantState:
# _QuirkTransport, which is constructed per turn by _build_model and has no
# access to the graph's state. Same per-task ContextVar reasoning as
# _reasoning_sink below — concurrent turns from different browser tabs must not
# share a counter.
_turn_timing: ContextVar[TurnTiming | None] = ContextVar("_turn_timing", default=None)


@contextmanager
def _timed(timing: TurnTiming, node: str) -> Iterator[None]:
    """Accumulate the wall clock of one node run into *timing*."""
    start = time.perf_counter()
    try:
        yield
    finally:
        timing.add(node, (time.perf_counter() - start) * 1000)

# The model's own reasoning for the current turn, collected off the wire by
# _QuirkTransport (see _split_content_parts) and drained by run_turn.
# A ContextVar rather than a parameter because _build_model — the one seam a
# turn has into transport construction — is called per graph node with no
# access to AssistantState, and a ContextVar is per-task, so concurrent turns from
# different browser tabs can't cross-contaminate.
_reasoning_sink: ContextVar[list[str] | None] = ContextVar("_reasoning_sink", default=None)

# HTTP clients built during the current turn, closed by run_turn when it ends
# (see _build_model). Same per-task ContextVar reasoning as _reasoning_sink.
_http_clients: ContextVar[list[httpx.AsyncClient] | None] = ContextVar(
    "_http_clients", default=None
)


def _record_reasoning(text: str) -> None:
    sink = _reasoning_sink.get()
    if sink is not None and text.strip():
        sink.append(text.strip())


def _split_content_parts(content: object) -> tuple[str, str] | None:
    """Split an OpenAI-compatible message's `content` into (answer text, reasoning).

    Confirmed against a live Databricks gpt-oss response: `content` can arrive
    as a list of typed parts (e.g. `[{"type": "reasoning", "text": "..."}]`)
    instead of a plain string — pydantic-ai's OpenAI-compatible parser expects
    `content: str | None` per the real OpenAI wire contract, and choked on the
    list with a Pydantic serializer warning, silently treating the round as
    unusable and burning a retry on it instead of surfacing a clean error.

    Only "text" parts are a real answer, but the reasoning parts are the single
    most useful thing to have when a turn fails: on a reasoning-only round (the
    exact shape behind "didn't get a final answer within this turn's round
    limit") the model's actual intent survives *nowhere else*. They used to be
    dropped on the floor, which is what made that failure impossible to debug —
    now they're returned so the caller can keep them. Returns None (leave
    alone) if content wasn't a list to begin with."""
    if not isinstance(content, list):
        return None
    parts = [p for p in content if isinstance(p, dict)]
    answer = "".join(p.get("text", "") for p in parts if p.get("type") == "text")
    reasoning = "\n".join(p.get("text", "") for p in parts if p.get("type") != "text")
    return answer, reasoning


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
            split = _split_content_parts(message.get("content"))
            if split is not None:
                message["content"], reasoning = split
                _record_reasoning(reasoning)
            # Some OpenAI-compatible servers (Databricks among them) put the
            # thinking in a sibling `reasoning_content` field instead of a typed
            # content part — same information, different shape, equally worth
            # keeping when a turn goes wrong.
            if isinstance(message.get("reasoning_content"), str):
                _record_reasoning(message["reasoning_content"])
            is_empty = not message.get("content") and not message.get("tool_calls")
            if is_empty and attempt < EMPTY_ROUND_RETRY_LIMIT:
                timing = _turn_timing.get()
                if timing is not None:
                    timing.empty_round_retries += 1
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


def _client(transport: httpx.AsyncBaseTransport | None = None) -> httpx.AsyncClient:
    """An HTTP client for this turn, registered so run_turn can close it.

    Every provider branch goes through here. An AsyncClient holds a keep-alive
    connection pool open, and pydantic-ai's providers build one per model when
    they aren't given one (``create_async_http_client()`` — a fresh client, not a
    shared cached one), so leaving the native branches to do that leaked one pool
    per turn for the life of the dashboard process.

    Registered out-of-band rather than returned because _build_model is the seam
    tests monkeypatch, so its signature stays "provider config in, Model out".
    """
    client = httpx.AsyncClient(transport=transport) if transport else httpx.AsyncClient()
    clients = _http_clients.get()
    if clients is not None:
        clients.append(client)
    return client


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
        return OpenAIChatModel(
            model,
            provider=OpenAIProvider(api_key=api_key, base_url=base_url, http_client=_client()),
        )
    if provider == "anthropic":
        return AnthropicModel(
            model,
            provider=AnthropicProvider(api_key=api_key, base_url=base_url, http_client=_client()),
        )
    if provider == "google":
        return GoogleModel(
            model,
            provider=GoogleProvider(api_key=api_key, base_url=base_url, http_client=_client()),
        )
    client = _client(transport=_QuirkTransport())
    return OpenAIChatModel(
        model,
        provider=OpenAIProvider(api_key=api_key, base_url=base_url, http_client=client),
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
        return {"error": "tool requires interactive input, which assistant cannot provide"}
    if result.structured_content is not None:
        return result.structured_content
    return "\n".join(getattr(block, "text", "") for block in result.content)


def _agent_model(state: AssistantState) -> Model:
    """One Model per turn, shared by every node.

    Each node used to build its own, which meant a fresh httpx.AsyncClient and
    so a fresh TLS+TCP handshake to the provider for Plan, for any re-plan, and
    again for Synthesize — 2-3 handshakes to answer one question, when they all
    talk to the same endpoint with the same credentials. Reusing one client lets
    the connection stay keep-alive across the whole turn."""
    if state.agent_model is None:
        state.agent_model = _build_model(state.provider, state.model, state.api_key, state.base_url)
    return state.agent_model


def _step_args(step: PlanStep) -> dict[str, Any]:
    """The step's real MCP arguments — everything except the discriminator,
    ``chart`` and ``summary``, which are presentation the model attaches for the UI
    and not parameters any tool accepts. Also what the dedup key is built from, so
    two otherwise-identical queries don't both run just because one asked to be
    drawn or worded differently."""
    return step.model_dump(exclude={"tool", "chart", "summary"})


async def _execute_step(step: PlanStep) -> Any:
    return await _call_mcp_tool(step.tool, _step_args(step))


def _step_to_tool_step(step: PlanStep, result: Any) -> ToolStep:
    args = _step_args(step)
    chart = getattr(step, "chart", None)
    if isinstance(result, dict):
        return ToolStep(
            name=step.tool,
            arguments=args,
            rows=result.get("rows"),
            error=result.get("error"),
            chart=chart,
        )
    return ToolStep(name=step.tool, arguments=args, chart=chart)


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
    "provider's spend already combined. Because each view is already scoped to "
    "one provider, never filter it by provider_name: every row is that provider "
    "already, and a guessed value simply matches nothing — filters are exact and "
    "case-sensitive, so \"databricks\" finds none of the \"Databricks\" rows. "
    "The fixed \"efficiency\", \"driver_health\", \"policy\", \"ai_usage\" and "
    "\"storage\" groups are the exception — they span providers and carry "
    "provider_name as a real column, so filtering those by provider_name is "
    "correct. None of them is a cross-provider spend total. "
    "\"storage.backing_storage_month\" in particular is AWS-billed S3 cost that is "
    "NOT in aws.monthly_bill (provider GOLD excludes Amazon S3); it is the storage "
    "behind Databricks, named Databricks Storage when mapping='databricks', not "
    "Databricks spend. Never add it to a Databricks figure or present a combined "
    "\"total Databricks cost\" — Databricks' own bill covers DBU compute only, and "
    "those are two separate bills. It carries two provider columns for that reason: "
    "billing_provider_name (who invoices it) and platform_provider_name (whose "
    "metadata claims the bucket). Only mapping='databricks' counts as Databricks "
    "storage (the Unity Catalog metastore root); 'unmapped' includes external-location "
    "buckets that are deliberately excluded because that data isn't Databricks-owned, so "
    "never add unmapped buckets back in to make the number look complete. It is a FLOOR, "
    "not a total — workspace DBFS roots and per-catalog storage roots are Databricks-managed "
    "but not counted — so describe it as at-least, never as the full figure. Where "
    "mapping_confidence = 'prefix_scoped', the cost is an upper bound for that bucket, so "
    "say so rather than quoting it as exact. "
    "For \"across all providers\"/\"total\" spend, or a computed comparison "
    "(month-over-month growth, which grew the most), plan one query_metric "
    "step per provider/period — the arithmetic happens in the final answer, "
    "never plan a run_sql step with a JOIN, UNION, or window function to "
    "combine or compute across providers yourself: run_sql has no correctness "
    "check on what you write, and a wrong GROUP BY or window frame produces a "
    "plausible-looking result that's silently wrong. Only plan a run_sql step "
    "when query_metric genuinely can't express the question (e.g. no view has "
    "the dimension you need), and keep it as simple as possible.\n\n"
    "Choosing between net_cost and gross_cost, on any view that offers both: "
    "gross_cost is charges only; net_cost also applies credits and adjustments. "
    "A \"where is the money going\" question — spend, a breakdown, a trend, a "
    "mover, what grew — asks about charges, so use gross_cost. Use net_cost only "
    "when the question is what was actually owed or paid (the bill, the invoice). "
    "This matters because a single one-off credit lands entirely in one month and "
    "nets against it, so net_cost shows that month as a spend collapse when "
    "nothing about the spend changed — a real AWS Redshift goodwill credit turns "
    "$68K of July charges into $10K net. Credits are never dropped, just kept out "
    "of the spend answer: they are itemized per credit line in the provider's own "
    "credits_month view, so plan a second step against it when the user asks about "
    "credits, discounts or the difference between the two figures.\n\n"
    "When the user asks to see, chart, graph, plot or visualise something, or "
    "asks for a trend or a breakdown, set two things on the query_metric step: "
    "measures=[the one relevant measure] (e.g. [\"gross_cost\"] — leaving it unset "
    "returns every measure on the view, which is too wide to chart), and "
    "chart={...} saying how to draw it. You choose the axis, because only you "
    "know which breakdown was asked for: \"spend by service\" means "
    "x=service_name, while \"the monthly trend\" means x=charge_month, even "
    "though both queries return the same columns. If a second dimension varies "
    "in the rows (e.g. you asked for several months of per-service spend), name "
    "it as series so each bar is split by it, rather than leaving the two "
    "dimensions to collide on one axis. Only the dimensions of the view you "
    "query are available as x/series.\n\n"
    "Whenever a single query_metric step answers the question on its own, also "
    "set summary={\"sentence\": ...} on it: one sentence answering the question, "
    "written with placeholders instead of numbers. The figures are computed from "
    "the rows and substituted in, so you never write a figure yourself and the "
    "answer needs no second round trip. Leave summary unset when the answer needs "
    "several steps compared against each other — that genuinely needs reasoning "
    "over the results, and you'll be asked to write it once the data is in."
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
    "Say which cost metric the figures are: gross_cost is charges only, net_cost "
    "has credits and adjustments applied. If the rows carry both and they differ "
    "materially for a period, that gap is a credit — name it rather than quoting "
    "one figure as though it were the whole story.\n\n"
    "Never draw a chart yourself: no ASCII art, no code block pretending to "
    "be a plot, no chart described in a markdown table — a chart renders "
    "automatically above your reply when the gathered data is one dimension "
    "+ one measure. State any other result in prose or a real markdown table.\n\n"
    "The full table and chart are already on screen above your reply, and the rows "
    "below may be a sample of a larger result (the row count is stated). So "
    "summarize — the totals, the movement, the outliers, what it means — rather "
    "than transcribing rows the user can already see, and never imply the sample is "
    "the whole result."
)


_EMPTY_PLAN_RETRY_INSTRUCTIONS = (
    "Your previous answer planned no steps at all. You have no spend data unless "
    "you plan for it: if this question is about cost, spend, usage or waste in "
    "any form, plan at least one query_metric step against a view listed above — "
    "do not reply that you lack the data. Leave steps empty only if the message "
    "genuinely needs none (a greeting, or thanks)."
)


def _plan_instructions(
    *,
    allow_explore: bool,
    allow_clarify: bool,
    explored: dict[str, tuple[ListDimensionValuesStep, Any]],
    tool_catalog: str,
    empty_plan_retry: bool = False,
    data_failures: list[str] | None = None,
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
    if empty_plan_retry:
        sections.append(_EMPTY_PLAN_RETRY_INSTRUCTIONS)
    if data_failures:
        sections.append(
            "Your previous plan produced no usable data. Fix the cause and plan "
            "again — do not tell the user the data is missing:\n"
            + "\n".join(f"- {f}" for f in data_failures)
        )
    return "\n\n".join(s for s in sections if s)


def _step_failure(step: ToolStep) -> str | None:
    """A one-line, *actionable* description of why a step yielded nothing, or
    None if it produced data (so a caller can tell "all steps failed" from
    "some worked").

    For an empty filtered query this looks up what the filtered dimensions
    really contain, because "no rows" alone is undiagnosable: a live model sent
    ``provider_name: "databricks"`` against data that says ``"Databricks"`` and
    concluded the data didn't exist. Values are matched exactly, so naming the
    real ones is the difference between a recoverable miss and a dead end.
    """
    if step.error:
        return f"{step.name}({step.arguments}) failed: {step.error}"
    if step.rows is None or step.rows:
        return None
    detail = f"{step.name}({step.arguments}) returned no rows"
    view = step.arguments.get("name")
    filters = step.arguments.get("filters")
    if not isinstance(view, str) or not isinstance(filters, dict):
        return detail
    hints = []
    for column in filters:
        try:
            values = distinct_values(view, str(column), limit=25)
        except Exception:  # noqa: BLE001 - a measure or unknown column: no hint to give
            continue
        if values:
            hints.append(f"{column} actually contains {[str(v) for v in values]}")
    if hints:
        detail += (
            f" — filter values are matched exactly and are case-sensitive; {'; '.join(hints)}"
        )
    return detail


# How many rows of a step's result reach the Synthesize prompt. Every row used to,
# as a Python repr: with QueryMetricStep.limit defaulting to 200 and MAX_PLAN_STEPS
# at 12, one ordinary query measured **24,348 tokens** of prompt — which is what
# produced the observed prompt-token p90 of 20.8k and max of 59.7k, not the 4.5k
# instruction prefix. Nothing user-visible is lost by capping it: ToolStep.rows is
# untouched, so the table and chart still render every row, and the model is told the
# count it didn't see (it's summarizing, not transcribing).
_SYNTH_ROW_SAMPLE = 30


def _describe_step(step: ToolStep) -> str:
    """One step's outcome, as compact text for the Synthesize prompt.

    Rows go out as header-plus-values lines rather than a list of dicts: a repr
    repeats every key on every row, which for 200 rows is most of the payload and
    none of the information.
    """
    if step.error:
        return f"- {step.name}({step.arguments}): error: {step.error}"
    rows = step.rows
    if rows is None:
        return f"- {step.name}({step.arguments}): ok"
    if not rows:
        return f"- {step.name}({step.arguments}): no rows"
    columns = list(rows[0])
    shown = rows[:_SYNTH_ROW_SAMPLE]
    lines = ["\t".join(columns)]
    lines += [
        "\t".join("" if row.get(c) is None else str(row.get(c)) for c in columns)
        for row in shown
    ]
    omitted = len(rows) - len(shown)
    tail = (
        f"\n  ... {omitted} more row{'s' if omitted != 1 else ''} not shown "
        f"(all {len(rows)} are already displayed to the user)"
        if omitted
        else ""
    )
    body = "\n  ".join(lines)
    return f"- {step.name}({step.arguments}): {len(rows)} rows\n  {body}{tail}"


# --- Graph --------------------------------------------------------------


@dataclass
class AssistantState:
    """Mutable state threaded through one turn's Plan -> [Explore -> re-Plan]
    -> Execute -> Synthesize graph run."""

    question: str
    history: list[ModelMessage]
    provider: str
    api_key: str
    model: str
    base_url: str | None
    allow_clarify: bool = True
    agent_model: Model | None = None  # built once per turn, see _agent_model
    empty_plan_retried: bool = False
    data_retried: bool = False  # one re-plan after every step came back empty/errored
    data_failures: list[str] = field(default_factory=list)
    pending_lookups: list[ListDimensionValuesStep] | None = None
    explored: dict[str, tuple[ListDimensionValuesStep, Any]] = field(default_factory=dict)
    plan: Plan | None = None
    steps: list[ToolStep] = field(default_factory=list)
    usage: RunUsage = field(default_factory=RunUsage)
    timing: TurnTiming = field(default_factory=TurnTiming)


@dataclass
class PlanNode(BaseNode[AssistantState, None, AssistantTurnResult]):
    """One structured-output Agent.run(), no tools. Commits to a ClarifyingQuestion,
    an ExploreRequest (first pass only), or a Plan.

    ClarifyingQuestion is dropped from the offered output types entirely when
    ``AssistantState.allow_clarify`` is False (the user's message is them clicking an
    option from our own last clarifying question). Enforced structurally, not
    just asked for in the prompt: a weak model was confirmed re-asking "which
    month?" right after the user picked "...for the previous month (default)",
    and a schema that has no room for a question can't produce one.
    """

    allow_explore: bool = True

    async def run(
        self, ctx: GraphRunContext[AssistantState, None]
    ) -> ExploreNode | ExecuteNode | End[AssistantTurnResult]:
        # Timed around the prompt build and the model round only — dispatching on
        # the output below is free, and this keeps plan_ms comparable across the
        # branches it can take.
        with _timed(ctx.state.timing, "PlanNode"):
            ctx.state.timing.plan_passes += 1
            tool_catalog = await _plan_tool_catalog()
            instructions = _plan_instructions(
                allow_explore=self.allow_explore,
                allow_clarify=ctx.state.allow_clarify,
                explored=ctx.state.explored,
                tool_catalog=tool_catalog,
                empty_plan_retry=ctx.state.empty_plan_retried,
                data_failures=ctx.state.data_failures,
            )
            output_types: list[type[BaseModel]] = [Plan]
            if self.allow_explore:
                output_types.insert(0, ExploreRequest)
            if ctx.state.allow_clarify:
                output_types.insert(0, ClarifyingQuestion)
            agent = Agent(
                _agent_model(ctx.state),
                instructions=instructions,
                output_type=output_types,
                retries={"output": _OUTPUT_RETRIES},
            )
            result = await agent.run(
                ctx.state.question,
                message_history=ctx.state.history,
                usage_limits=UsageLimits(request_limit=_OUTPUT_REQUEST_LIMIT),
            )
        ctx.state.usage.incr(result.usage)
        match result.output:
            case ClarifyingQuestion() as cq:
                ctx.state.timing.outcome = "clarify"
                return End(AssistantTurnResult(text=cq.question, options=cq.options))
            case ExploreRequest() as request:
                ctx.state.pending_lookups = request.lookups
                return ExploreNode()
            case Plan() as plan:
                if not plan.steps and not ctx.state.empty_plan_retried:
                    # An empty plan is legitimate for a greeting or a followup
                    # already answered, but a live gpt-oss-20b also returned one
                    # for "break down last month's spend" and then honestly
                    # reported "I don't have any data" — a useless answer from a
                    # perfectly answerable question. One bounded re-ask (the flag
                    # makes it exactly one, so a real greeting still settles) is
                    # cheaper than shipping that non-answer.
                    logger.info("assistant_plan_empty_retry", model=ctx.state.model)
                    ctx.state.empty_plan_retried = True
                    return PlanNode(allow_explore=False)
                ctx.state.plan = plan
                return ExecuteNode()
            case _:
                # output_type is built from a runtime list, not a literal, so mypy
                # can't prove exhaustiveness here the way assert_never wants — this
                # is a genuine "should never happen per the Agent's output_type
                # contract" guard, not a reachable branch.
                raise AssertionError(f"unexpected plan output: {result.output!r}")


@dataclass
class ExploreNode(BaseNode[AssistantState, None, AssistantTurnResult]):
    """No LLM call. Dedupes and runs the requested list_dimension_values
    lookups concurrently, then hands back to a must-commit PlanNode."""

    async def run(self, ctx: GraphRunContext[AssistantState, None]) -> PlanNode:
        assert ctx.state.pending_lookups is not None
        with _timed(ctx.state.timing, "ExploreNode"):
            by_key: dict[str, ListDimensionValuesStep] = {}
            for lookup in ctx.state.pending_lookups:
                by_key.setdefault(_dedup_key(lookup.tool, _step_args(lookup)), lookup)
            keys = list(by_key)
            outcomes = await asyncio.gather(*(_execute_step(by_key[key]) for key in keys))
            ctx.state.explored = {
                key: (by_key[key], outcome) for key, outcome in zip(keys, outcomes, strict=True)
            }
            ctx.state.pending_lookups = None
        return PlanNode(allow_explore=False)


@dataclass
class ExecuteNode(BaseNode[AssistantState, None, AssistantTurnResult]):
    """No LLM call. Dedupes Plan.steps by (tool, args) — reusing anything
    already fetched during an explore round — and runs the rest concurrently.
    This is the root-cause dedup: a repeated step in the committed plan never
    reaches DuckDB twice, dedeuped in code before execution rather than
    reactively after a duplicate call already happened."""

    async def run(
        self, ctx: GraphRunContext[AssistantState, None]
    ) -> SynthesizeNode | PlanNode | End[AssistantTurnResult]:
        assert ctx.state.plan is not None
        with _timed(ctx.state.timing, "ExecuteNode"):
            outcome = await self._execute(ctx)
        if not isinstance(outcome, SynthesizeNode):
            return outcome
        # The rows are in, so the answer is arithmetic — skip the Synthesize model
        # call, which on real turns cost 2.8-7.1s to narrate a chart the UI had
        # already drawn. The model's own SummarySpec supplies the wording (it rode
        # along on the plan, costing nothing); answer_caption fills every figure from
        # the rows and returns None for anything it can't state honestly, which
        # falls through to real synthesis.
        plan_summary = next(
            (
                step.summary
                for step in ctx.state.plan.steps
                if isinstance(step, QueryMetricStep) and step.summary
            ),
            None,
        )
        caption = caption_for(ctx.state.steps, plan_summary.sentence if plan_summary else None)
        if caption is None:
            return outcome
        ctx.state.timing.outcome = "answer"
        ctx.state.timing.answer_source = "caption" if plan_summary is None else "summary_spec"
        return End(AssistantTurnResult(text=caption, steps=ctx.state.steps))

    async def _execute(
        self, ctx: GraphRunContext[AssistantState, None]
    ) -> SynthesizeNode | PlanNode:
        """Split out so run() can time the whole thing without indenting it —
        this node has two exits and both need to be inside the timer."""
        assert ctx.state.plan is not None
        by_key: dict[str, PlanStep] = {}
        results: dict[str, Any] = {}
        for key, (lookup, outcome) in ctx.state.explored.items():
            by_key[key] = lookup
            results[key] = outcome
        for step in ctx.state.plan.steps:
            by_key.setdefault(_dedup_key(step.tool, _step_args(step)), step)

        to_run = [key for key in by_key if key not in results]
        if to_run:
            outcomes = await asyncio.gather(*(_execute_step(by_key[key]) for key in to_run))
            results.update(zip(to_run, outcomes, strict=True))

        ctx.state.steps = [_step_to_tool_step(by_key[key], results[key]) for key in by_key]
        if ctx.state.plan.steps and not ctx.state.data_retried:
            failures = [_step_failure(step) for step in ctx.state.steps]
            if all(failures) and any(failures):
                # Every step came back empty or errored, so there is nothing to
                # answer from — and the model can't see why. Confirmed live twice:
                # `order_by="net_cost DESC"` errored and it replied "please retry
                # without the order_by clause", and a `provider_name: "databricks"`
                # filter (wrong case — the data says "Databricks" — and redundant
                # on an already-provider-scoped view) returned zero rows and it
                # replied "no records were returned, I'm unable to visualize".
                # Both are recoverable, so hand the failures back for exactly one
                # more plan rather than reporting a dead end.
                ctx.state.data_retried = True
                ctx.state.data_failures = [f for f in failures if f]
                logger.info(
                    "assistant_data_empty_retry", model=ctx.state.model, steps=len(failures)
                )
                return PlanNode(allow_explore=False)
        return SynthesizeNode()


@dataclass
class SynthesizeNode(BaseNode[AssistantState, None, AssistantTurnResult]):
    """One Agent.run(), no tools, output_type=str. Writes the final answer
    from the gathered ToolStep rows/errors — charting is already decided at
    Plan time, this node only writes prose."""

    async def run(self, ctx: GraphRunContext[AssistantState, None]) -> End[AssistantTurnResult]:
        with _timed(ctx.state.timing, "SynthesizeNode"):
            agent = Agent(_agent_model(ctx.state), instructions=_SYNTHESIZE_INSTRUCTIONS)
            gathered = (
                "\n".join(_describe_step(step) for step in ctx.state.steps)
                or "(no data was gathered)"
            )
            prompt = f"User question: {ctx.state.question}\n\nData gathered:\n{gathered}"
            result = await agent.run(
                prompt,
                message_history=ctx.state.history,
                usage_limits=UsageLimits(request_limit=_OUTPUT_REQUEST_LIMIT),
            )
        ctx.state.usage.incr(result.usage)
        ctx.state.timing.outcome = "answer"
        ctx.state.timing.answer_source = "model"
        return End(AssistantTurnResult(text=result.output, steps=ctx.state.steps))


# ponytail: Graph/GraphBuilder are runtime-Generic (pydantic_graph.graph_builder
# uses typing_extensions' infer_variance=True TypeVars), but the installed mypy
# doesn't resolve that as subscriptable — left unparameterized rather than
# fighting a mypy/typing_extensions version gap; state_type/input_type/
# output_type below still give GraphBuilder everything it needs at runtime.
def _build_graph() -> Graph:
    g = GraphBuilder(
        name="assistant_turn",
        state_type=AssistantState,
        input_type=PlanNode,
        output_type=AssistantTurnResult,
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
) -> AssistantTurnResult:
    """Run one user turn to completion: Plan -> [Explore -> re-Plan, at most
    once] -> Execute -> Synthesize.

    Extends *messages* in place with exactly two new messages — the user
    question and the final answer — regardless of how many internal
    Plan/Explore/Execute/Synthesize steps ran this turn: none of this turn's
    planning/tool-call detail leaks into cross-turn history. Returns the
    final assistant text (or clarifying question) plus every tool call made
    along the way (so the UI can show what was actually queried, not just
    the model's prose summary of it), and logs exactly one row to
    ``meta/assistant_turns/`` per call, with token usage summed across every LLM
    call this turn made.

    Set *answering_clarification* when *question* is the user picking one of the
    options from this engine's own previous ClarifyingQuestion — that resolves
    the ambiguity by construction, so this turn may not ask another one (see
    PlanNode).
    """
    state = AssistantState(
        question=question,
        history=list(messages),
        provider=provider,
        api_key=api_key,
        model=model,
        base_url=base_url,
        allow_clarify=not answering_clarification,
    )
    reasoning: list[str] = []
    _reasoning_sink.set(reasoning)
    http_clients: list[httpx.AsyncClient] = []
    _http_clients.set(http_clients)
    _turn_timing.set(state.timing)
    started = time.perf_counter()

    def _log(result: AssistantTurnResult) -> AssistantTurnResult:
        timings = state.timing.columns(
            duration_ms=(time.perf_counter() - started) * 1000,
            # pydantic-ai's own count, so a structured-output retry shows up as
            # the extra request it really is.
            llm_requests=state.usage.requests,
        )
        record_assistant_turn(
            turn_id=str(uuid.uuid4()),
            session_id=session_id,
            model=model,
            prompt_tokens=state.usage.input_tokens or None,
            completion_tokens=state.usage.output_tokens or None,
            total_tokens=state.usage.total_tokens or None,
            tool_call_count=len(result.steps),
            occurred_at=datetime.now(UTC),
            timings=timings,
        )
        logger.info("assistant_turn_timing", model=model, **timings)
        return result

    try:
        return await _run_graph(state, messages, question, api_key, reasoning, _log)
    finally:
        for client in http_clients:
            await client.aclose()


async def _run_graph(
    state: AssistantState,
    messages: list[ModelMessage],
    question: str,
    api_key: str,
    reasoning: list[str],
    _log: Callable[[AssistantTurnResult], AssistantTurnResult],
) -> AssistantTurnResult:
    """The turn itself, split out so run_turn's ``finally`` can close the HTTP
    clients the turn built regardless of how it ended."""
    model = state.model
    try:
        # cast: _graph is left unparameterized (see _build_graph) so mypy can't
        # resolve .run()'s OutputT on its own — GraphBuilder(output_type=AssistantTurnResult)
        # already pins this at runtime.
        result = cast(AssistantTurnResult, await _graph.run(inputs=PlanNode(), state=state))
    except (UsageLimitExceeded, UnexpectedModelBehavior) as exc:
        # ponytail: two distinct pydantic-ai exceptions both mean "didn't get
        # a real structured output within budget" — UsageLimitExceeded is
        # _OUTPUT_REQUEST_LIMIT exhausted; a model stuck returning
        # invalid/empty output (e.g. every _QuirkTransport retry still empty)
        # hits pydantic-ai's own output-retry budget first and raises
        # UnexpectedModelBehavior instead. Same user-facing story either way,
        # but log which one and why: this message used to be a dead end, with
        # the actual cause (and the model's own reasoning) thrown away, making
        # a real, reproducible failure impossible to diagnose from the UI.
        logger.warning(
            "assistant_turn_no_final_answer",
            model=model,
            error_type=type(exc).__name__,
            error=_redact(_root_cause(exc), api_key),
            reasoning_traces=len(reasoning),
        )
        state.timing.outcome = "no_answer"
        return _log(
            AssistantTurnResult(
                text=f"{_ROUND_LIMIT_MESSAGE}\n\n_{type(exc).__name__}: "
                f"{_redact(str(exc), api_key)}_",
                # Whatever ExecuteNode already gathered before the turn fell over
                # is still real data — the tables and charts render, so a failed
                # synthesis costs the prose, not the answer. It also keeps the
                # turn log's tool_call_count honest for exactly the turns whose
                # cost most needs explaining.
                steps=state.steps,
                reasoning=reasoning,
            )
        )
    except Exception as exc:  # noqa: BLE001 - bad key/base_url/network: expected, not exceptional
        logger.warning("assistant_turn_request_failed", model=model, error=type(exc).__name__)
        return _log(
            AssistantTurnResult(
                text=f"Request failed: {_redact(_root_cause(exc), api_key)}",
                steps=state.steps,
                reasoning=reasoning,
            )
        )

    messages.append(ModelRequest.user_text_prompt(question))
    messages.append(ModelResponse(parts=[TextPart(result.text)]))
    return _log(replace(result, reasoning=reasoning))
