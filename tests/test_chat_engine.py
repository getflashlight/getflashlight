from __future__ import annotations

import asyncio
import calendar
import gzip
import json
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import httpx
import pytest
from openai import AsyncOpenAI
from pydantic import ValidationError
from pydantic_ai.messages import ModelResponse, SystemPromptPart, TextPart, ToolCallPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.usage import RequestUsage

from flashlight.core.settings import get_settings
from flashlight.dashboard import chat_engine
from flashlight.dashboard.chat_engine import ToolStep
from flashlight.focus.enums import ChargeCategory, ComputeClass, ProviderName, ServiceCategory
from flashlight.focus.model import FocusRecord
from flashlight.ingest.base import IngestWindow
from flashlight.lake import duck


@pytest.fixture(autouse=True)
def lake_home(tmp_path, monkeypatch):  # type: ignore[no-untyped-def]
    monkeypatch.setenv("FLASHLIGHT_HOME", str(tmp_path))
    get_settings.cache_clear()
    chat_engine._tool_schemas_cache = None  # noqa: SLF001 - reset the module-level caches between tests
    chat_engine._plan_tool_catalog_cache = None  # noqa: SLF001
    yield
    get_settings.cache_clear()


def _text_response(text: str, *, input_tokens: int = 10, output_tokens: int = 5) -> ModelResponse:
    return ModelResponse(
        parts=[TextPart(content=text)],
        usage=RequestUsage(input_tokens=input_tokens, output_tokens=output_tokens),
    )


def _plan_response(steps: list[dict[str, Any]], *, call_id: str = "call_1") -> ModelResponse:
    return ModelResponse(
        parts=[
            ToolCallPart(tool_name="final_result_Plan", args={"steps": steps}, tool_call_id=call_id)
        ],
        usage=RequestUsage(input_tokens=10, output_tokens=5),
    )


def _explore_response(lookups: list[dict[str, Any]], *, call_id: str = "call_1") -> ModelResponse:
    return ModelResponse(
        parts=[
            ToolCallPart(
                tool_name="final_result_ExploreRequest",
                args={"lookups": lookups},
                tool_call_id=call_id,
            )
        ],
        usage=RequestUsage(input_tokens=10, output_tokens=5),
    )


def _clarify_response(
    question: str, options: list[str], *, call_id: str = "call_1"
) -> ModelResponse:
    return ModelResponse(
        parts=[
            ToolCallPart(
                tool_name="final_result_ClarifyingQuestion",
                args={"question": question, "options": options},
                tool_call_id=call_id,
            )
        ],
        usage=RequestUsage(input_tokens=10, output_tokens=5),
    )


def _use_model(monkeypatch: pytest.MonkeyPatch, model: object) -> None:
    """Bypass real provider construction — the seam _build_model gives tests,
    in place of touching a real provider. One turn now constructs 2-3 Agents
    internally (Plan, maybe a retry Plan, Synthesize); all of them get this
    same fixed model, which is why a FunctionModel callable typically branches
    on info.output_tools to tell which node is calling (see _run_turn below)."""
    monkeypatch.setattr(chat_engine, "_build_model", lambda *args, **kwargs: model)


def _run_turn(messages: list[Any], question: str, **overrides: Any) -> chat_engine.ChatTurnResult:
    kwargs: dict[str, Any] = {
        "provider": "openai",
        "api_key": "sk-test",
        "model": "openai/gpt-4o",
        "base_url": None,
        "session_id": "s1",
    }
    kwargs.update(overrides)
    return asyncio.run(chat_engine.run_turn(messages, question, **kwargs))


def _focus_record(
    day: int, *, month: int = 5, provider: ProviderName = ProviderName.AWS
) -> FocusRecord:
    when = datetime(2026, month, day, tzinfo=UTC)
    period_end = date(2026, month, calendar.monthrange(2026, month)[1])
    return FocusRecord(
        provider_name=provider,
        billing_account_id="acct",
        billing_period_start=date(2026, month, 1),
        billing_period_end=period_end,
        charge_period_start=when,
        charge_period_end=when,
        billed_cost=Decimal("10"),
        effective_cost=Decimal("10"),
        list_cost=Decimal("12"),
        charge_category=ChargeCategory.USAGE,
        service_category=ServiceCategory.COMPUTE,
        service_name="AmazonEC2",
        tags={"team": "data"},
        x_compute_class=ComputeClass.NOT_APPLICABLE,
        x_source_connector="t",
    )


def _ingest_may_aws() -> None:
    from flashlight.lake import bronze
    from flashlight.transform.runner import build_gold

    window = IngestWindow(date(2026, 5, 1), date(2026, 5, 31))
    bronze.write_window("t", window, [_focus_record(15)], ingest_run_id="r1")
    build_gold()


def _chat_turn_rows() -> list[dict[str, object]]:
    con = duck.connect()
    try:
        duck.register_chat_turns(con)
        return con.execute("SELECT * FROM telemetry.chat_turn").fetchdf().to_dict(  # type: ignore[no-any-return]
            "records"
        )
    finally:
        con.close()


def _openai_style_body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "created": 1,
        "model": "fake-model",
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": None}, "finish_reason": "stop"}
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }
    body.update(overrides)
    return body


def _empty_body() -> dict[str, Any]:
    return _openai_style_body()


def _reasoning_only_body(reasoning_text: str) -> dict[str, Any]:
    return _openai_style_body(
        choices=[
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": [{"type": "reasoning", "text": reasoning_text}],
                },
                "finish_reason": "stop",
            }
        ]
    )


def _text_body(text: str) -> dict[str, Any]:
    return _openai_style_body(
        choices=[
            {"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}
        ]
    )


def _tool_call_body(name: str, arguments: dict[str, object], call_id: str) -> dict[str, Any]:
    return _openai_style_body(
        choices=[
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {"name": name, "arguments": json.dumps(arguments)},
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ]
    )


def _plan_body(steps: list[dict[str, Any]], call_id: str = "call_1") -> dict[str, Any]:
    return _tool_call_body("final_result_Plan", {"steps": steps}, call_id)


class _QueueTransport(httpx.AsyncBaseTransport):
    """Test-only stand-in for the real network transport: pops one canned
    response body per raw HTTP call, in order."""

    def __init__(self, bodies: list[dict[str, Any]]):
        self._bodies = list(bodies)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=self._bodies.pop(0), request=request)


def _openai_compatible_model_over(bodies: list[dict[str, Any]]) -> OpenAIChatModel:
    """A real OpenAIChatModel wired through the real _QuirkTransport (production
    code), with only the innermost "actual HTTP" layer replaced by a canned
    queue — exercises the real Harmony-marker-strip / empty-round-retry logic
    end to end, the same way run_turn's openai_compatible branch does. One
    turn draws from this same queue across every internal Agent.run() call
    (Plan, maybe a retry Plan, Synthesize), in order."""
    transport = chat_engine._QuirkTransport(inner=_QueueTransport(bodies))  # noqa: SLF001
    client = AsyncOpenAI(
        api_key="sk-test",
        base_url="http://fake.local/v1",
        http_client=httpx.AsyncClient(transport=transport),
    )
    return OpenAIChatModel("fake-model", provider=OpenAIProvider(openai_client=client))


def _run_turn_openai_compatible(
    monkeypatch: pytest.MonkeyPatch, bodies: list[dict[str, Any]], question: str = "hi"
) -> chat_engine.ChatTurnResult:
    _use_model(monkeypatch, _openai_compatible_model_over(bodies))

    async def _check() -> chat_engine.ChatTurnResult:
        messages: list[Any] = []
        return await chat_engine.run_turn(
            messages,
            question,
            provider="openai_compatible",
            api_key="sk-test",
            model="fake-model",
            base_url="http://fake.local/v1",
            session_id="s-oc",
        )

    return asyncio.run(_check())


def test_mcp_tools_exclude_list_metrics_but_cover_the_rest() -> None:
    schemas = asyncio.run(chat_engine._mcp_tool_schemas())  # noqa: SLF001
    names = {name for name, _description, _schema in schemas}
    assert names == {
        "describe_metric",
        "query_metric",
        "list_dimension_values",
        "list_optimization_rules",
        "list_policy_rules",
        "run_sql",
    }
    assert all(description for _name, description, _schema in schemas)


@pytest.mark.parametrize(
    ("label", "raw"),
    [
        # The two shapes a live Databricks gpt-oss-20b actually produced, which
        # failed 3 of 4 identical runs before _normalize_step existed.
        ("nested under tool name", {"query_metric": {"name": "aws.monthly_bill"}}),
        ("tag plus nested", {"tool": "query_metric", "query_metric": {"name": "aws.monthly_bill"}}),
        # Same instinct, generic wrapper keys.
        ("tag plus args", {"tool": "query_metric", "args": {"name": "aws.monthly_bill"}}),
        (
            "tag plus parameters",
            {"tool": "query_metric", "parameters": {"name": "aws.monthly_bill"}},
        ),
        # Already correct — must pass through untouched.
        ("already flat", {"tool": "query_metric", "name": "aws.monthly_bill"}),
    ],
)
def test_plan_accepts_the_step_shapes_models_actually_emit(label: str, raw: dict) -> None:  # type: ignore[type-arg]
    """Regression test: gpt-oss-20b nests a step's arguments under the tool's own
    name instead of inlining them beside a `tool` tag, which failed Plan's
    discriminated union outright ("Unable to extract tag using discriminator
    'tool'") and killed the turn. All of these mean the same call, so parsing is
    liberal while the internal type stays a strict union."""
    step = chat_engine.Plan(steps=[raw]).steps[0]
    assert step.tool == "query_metric"
    assert isinstance(step, chat_engine.QueryMetricStep)
    assert step.name == "aws.monthly_bill", label


def test_plan_still_rejects_an_unrecognizable_step() -> None:
    """The normalizer must not paper over a genuinely unknown step by inventing
    one — pydantic's own validation error is the right outcome so the model gets
    told, and retries."""
    with pytest.raises(ValidationError):
        chat_engine.Plan(steps=[{"not_a_tool": {"foo": 1}}])
    with pytest.raises(ValidationError):
        # Ambiguous: two tool-named keys, no way to know which was meant.
        chat_engine.Plan(steps=[{"query_metric": {}, "run_sql": {}}])


def test_explore_request_accepts_nested_lookups() -> None:
    nested = {"name": "aws.monthly_bill", "dimension": "charge_month"}
    request = chat_engine.ExploreRequest(lookups=[{"list_dimension_values": nested}])
    assert request.lookups[0].dimension == "charge_month"


def test_run_turn_re_asks_once_when_the_plan_comes_back_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression test: a live gpt-oss-20b answered "break down last month's
    spend" with an empty plan and then said "I don't have any data" — a useless
    non-answer to an answerable question. An empty plan gets exactly one re-ask
    (bounded, so a real greeting still settles) with an explicit nudge."""
    _ingest_may_aws()
    instructions: list[str | None] = []
    plans = [
        _plan_response([]),  # first attempt: nothing planned
        _plan_response([{"tool": "query_metric", "name": "aws.monthly_bill"}]),  # after the nudge
    ]

    def fn(messages: Any, info: Any) -> ModelResponse:
        instructions.append(messages[-1].instructions)
        if not info.output_tools:
            return _text_response("You spent $10.")
        return plans.pop(0)

    _use_model(monkeypatch, FunctionModel(fn))

    result = _run_turn([], "what did I spend?", session_id="sempty")

    assert result.text == "You spent $10."
    assert [s.name for s in result.steps] == ["query_metric"]
    assert "planned no steps at all" in (instructions[1] or "")


def test_run_turn_accepts_a_second_empty_plan_without_looping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A genuine greeting needs no data — the empty-plan re-ask is capped at one,
    so a model that legitimately plans nothing twice still gets an answer out
    rather than looping."""
    plan_calls = 0

    def fn(messages: Any, info: Any) -> ModelResponse:
        nonlocal plan_calls
        if not info.output_tools:
            return _text_response("Hi — ask me about your spend.")
        plan_calls += 1
        return _plan_response([])

    _use_model(monkeypatch, FunctionModel(fn))

    result = _run_turn([], "hello", session_id="sgreet")

    assert result.text == "Hi — ask me about your spend."
    assert plan_calls == 2  # the original plus exactly one re-ask, then it settles
    assert result.steps == []


def test_plan_step_models_match_real_mcp_tool_signatures() -> None:
    """Drift guard: each typed PlanStep model hand-mirrors a real MCP tool's
    signature (see the comment above QueryMetricStep for why) — if a tool's
    params ever change in mcp/server.py, this must fail loudly rather than
    silently leaving the plan schema stale."""
    tool_schemas = asyncio.run(chat_engine._mcp_tool_schemas())  # noqa: SLF001
    schemas = {name: schema for name, _description, schema in tool_schemas}
    for model_cls in chat_engine.PLAN_STEP_MODELS:
        tool_name = model_cls.model_fields["tool"].default
        schema_fields = set(schemas[tool_name].get("properties", {}))
        # `chart` is presentation the model attaches for the UI (see ChartSpec),
        # not an argument any MCP tool accepts — excluded here for the same
        # reason _step_args excludes it before dispatch.
        step_fields = set(model_cls.model_fields) - {"tool", "chart"}
        assert step_fields == schema_fields, tool_name


def test_connected_providers_and_catalog_lines_ground_in_live_data() -> None:
    """Regression test: the model has been observed offering "GCP"/"Azure" as
    clarifying-question options in an instance that only has AWS connected —
    it pattern-matches common cloud names instead of using real data. These
    two lines (folded into every Plan-phase call's instructions) must name
    only what's actually published, sourced the same live way the nav
    sidebar is (discover_provider_groups)."""
    assert "No providers are connected yet" in chat_engine._connected_providers_line()  # noqa: SLF001

    _ingest_may_aws()

    assert chat_engine._connected_providers_line() == (  # noqa: SLF001
        "Connected providers: AWS. Never mention or offer any other provider."
    )
    catalog_line = chat_engine._catalog_line()  # noqa: SLF001
    assert "Available metric views" in catalog_line
    assert "aws.monthly_bill: dimensions=" in catalog_line


def test_run_turn_never_persists_instructions_into_message_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """instructions= is recomputed fresh on every Agent.run() call and never
    baked into message_history. messages grows by exactly 2 entries per turn
    (the user question, the final answer) regardless of how many internal
    Plan/Synthesize calls this turn made — no tool-call noise leaks in."""

    def fn(messages: Any, info: Any) -> ModelResponse:
        if not info.output_tools:
            return _text_response("ok")
        return _plan_response([{"tool": "list_optimization_rules"}])

    _use_model(monkeypatch, FunctionModel(fn))

    messages: list[Any] = []
    _run_turn(messages, "hello", session_id="sp1")
    _run_turn(messages, "again", session_id="sp1")

    assert not any(isinstance(p, SystemPromptPart) for m in messages for p in m.parts)
    assert len(messages) == 4  # 2 user turns + 2 assistant replies, nothing extra


def test_run_turn_without_tool_call_logs_one_row(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty-steps Plan (no new data needed — a greeting, a followup
    already answered) reaches Synthesize with nothing gathered. Two plan
    responses because an empty plan gets exactly one re-ask first (see
    test_run_turn_accepts_a_second_empty_plan_without_looping)."""
    responses = [_plan_response([]), _plan_response([]), _text_response("The answer is 42.")]
    _use_model(monkeypatch, FunctionModel(lambda messages, info: responses.pop(0)))

    messages: list[Any] = []
    result = _run_turn(messages, "hello", session_id="s1")

    assert result.text == "The answer is 42."
    assert result.steps == []
    rows = _chat_turn_rows()
    assert len(rows) == 1
    assert rows[0]["session_id"] == "s1"
    assert rows[0]["model"] == "openai/gpt-4o"
    assert rows[0]["prompt_tokens"] == 30  # summed across both Plan calls + Synthesize
    assert rows[0]["completion_tokens"] == 15
    assert rows[0]["tool_call_count"] == 0
    # No message text or API key ever land in the telemetry row.
    assert set(rows[0]) == {
        "turn_id",
        "session_id",
        "model",
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "tool_call_count",
        "occurred_at",
    }


def test_run_turn_executes_real_tool_call_in_process(monkeypatch: pytest.MonkeyPatch) -> None:
    responses = [
        _plan_response([{"tool": "list_optimization_rules"}]),
        _text_response(
            "You have several optimization rules available.", input_tokens=20, output_tokens=8
        ),
    ]
    _use_model(monkeypatch, FunctionModel(lambda messages, info: responses.pop(0)))

    messages: list[Any] = []
    turn = _run_turn(messages, "what optimization rules exist?", session_id="s2")

    assert turn.text == "You have several optimization rules available."
    # list_optimization_rules returns a wrapped list, not a "rows" dict — no chartable data.
    assert turn.steps == [
        ToolStep(name="list_optimization_rules", arguments={}, rows=None, error=None)
    ]
    assert len(messages) == 2  # only the clean [user question, final answer] pair persists

    rows = _chat_turn_rows()
    assert len(rows) == 1
    assert rows[0]["tool_call_count"] == 1
    assert rows[0]["prompt_tokens"] == 30  # accumulated across both LLM calls
    assert rows[0]["completion_tokens"] == 13


def test_run_turn_dedups_a_repeated_identical_step_in_one_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression test: a live Databricks gpt-oss-20b response has been
    observed calling query_metric with the exact same arguments several times
    in a row instead of reusing data it already has. The fix is now
    architectural — ExecuteNode dedupes the *committed* plan by (tool, args)
    before a single real dispatch happens, rather than catching a repeat
    reactively after it already ran. Verified by counting real dispatches,
    not by inspecting message history (which no longer carries tool-call
    parts at all)."""
    _ingest_may_aws()  # so describe_metric resolves; an all-errored plan re-plans
    calls: list[tuple[str, dict[str, Any]]] = []
    original = chat_engine._call_mcp_tool

    async def counting(name: str, kwargs: dict[str, Any]) -> Any:
        calls.append((name, kwargs))
        return await original(name, kwargs)

    monkeypatch.setattr(chat_engine, "_call_mcp_tool", counting)

    step = {"tool": "describe_metric", "name": "aws.monthly_bill"}
    responses = [_plan_response([step, step]), _text_response("done")]
    _use_model(monkeypatch, FunctionModel(lambda messages, info: responses.pop(0)))

    messages: list[Any] = []
    result = _run_turn(messages, "describe it twice", session_id="s2m")

    assert result.text == "done"
    assert len(result.steps) == 1  # consolidated -- the repeat isn't shown again
    assert result.steps[0].name == "describe_metric"
    assert len(calls) == 1  # deduped before dispatch, not after


def test_run_turn_captures_rows_from_a_real_query_metric_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ingest_may_aws()

    responses = [
        _plan_response([{"tool": "query_metric", "name": "aws.monthly_bill", "limit": 10}]),
        _text_response("You spent $10 in May."),
    ]
    _use_model(monkeypatch, FunctionModel(lambda messages, info: responses.pop(0)))

    messages: list[Any] = []
    turn = _run_turn(messages, "what did I spend in May?", session_id="s2b")

    assert len(turn.steps) == 1
    step = turn.steps[0]
    assert step.name == "query_metric"
    assert step.error is None
    assert step.rows is not None
    assert len(step.rows) == 1
    assert step.rows[0]["net_cost"] == 10.0


def test_run_turn_accepts_order_by_as_a_list(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression test: models commonly send order_by as a list (e.g.
    ["charge_month"]) even though sorting is single-column — the real
    MCP-backed call must still succeed instead of a Pydantic validation error."""
    _ingest_may_aws()

    responses = [
        _plan_response(
            [
                {
                    "tool": "query_metric",
                    "name": "aws.monthly_bill",
                    "limit": 10,
                    "order_by": ["charge_month"],
                }
            ]
        ),
        _text_response("You spent $10 in May."),
    ]
    _use_model(monkeypatch, FunctionModel(lambda messages, info: responses.pop(0)))

    messages: list[Any] = []
    turn = _run_turn(messages, "what did I spend in May, sorted by month?", session_id="s2g")

    assert turn.steps[0].error is None
    assert turn.steps[0].rows is not None
    assert len(turn.steps[0].rows) == 1


def test_run_turn_narrows_columns_with_measures_param(monkeypatch: pytest.MonkeyPatch) -> None:
    """monthly_bill has five measures (net/gross/credit/list/savings) — without
    narrowing, query_metric returns all of them, which is too wide for
    _render_rows' one-dimension/one-measure chart heuristic. `measures` lets
    the model ask for just one."""
    _ingest_may_aws()

    responses = [
        _plan_response(
            [{"tool": "query_metric", "name": "aws.monthly_bill", "measures": ["net_cost"]}]
        ),
        _text_response("You spent $10 in May."),
    ]
    _use_model(monkeypatch, FunctionModel(lambda messages, info: responses.pop(0)))

    messages: list[Any] = []
    turn = _run_turn(messages, "chart my spend", session_id="s2i")

    assert turn.steps[0].error is None
    assert turn.steps[0].rows is not None
    row = turn.steps[0].rows[0]
    assert "net_cost" in row
    assert "gross_cost" not in row and "savings" not in row


def test_run_turn_accepts_a_list_filter_value_as_an_in_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression test: a model asking for "a few months" naturally sends a list
    filter value (e.g. {"charge_month": ["2026-05-01", "2026-06-01"]}) wanting an
    IN-match. Binding that list as one DATE-column equality parameter raised
    "Conversion Error: Unimplemented type for cast (DATE -> VARCHAR[])" — filters
    must support a list value, not just strict scalar equality."""
    from flashlight.lake import bronze
    from flashlight.transform.runner import build_gold

    window = IngestWindow(date(2026, 5, 1), date(2026, 6, 30))
    bronze.write_window(
        "t", window, [_focus_record(15), _focus_record(15, month=6)], ingest_run_id="r1"
    )
    build_gold()

    responses = [
        _plan_response(
            [
                {
                    "tool": "query_metric",
                    "name": "aws.monthly_bill",
                    "measures": ["net_cost"],
                    "filters": {"charge_month": ["2026-05-01", "2026-06-01"]},
                }
            ]
        ),
        _text_response("You spent $20 across May and June."),
    ]
    _use_model(monkeypatch, FunctionModel(lambda messages, info: responses.pop(0)))

    messages: list[Any] = []
    turn = _run_turn(messages, "what did I spend in May and June?", session_id="s2j")

    assert turn.steps[0].error is None
    assert turn.steps[0].rows is not None
    assert len(turn.steps[0].rows) == 2


def test_run_turn_executes_a_multi_step_plan_concurrently(monkeypatch: pytest.MonkeyPatch) -> None:
    """A plan spanning several tools/providers is committed once and its steps
    run concurrently, each producing its own ToolStep in plan order — the
    efficiency win over the old N-round improvised loop."""
    _ingest_may_aws()

    responses = [
        _plan_response(
            [
                {"tool": "query_metric", "name": "aws.monthly_bill", "limit": 10},
                {"tool": "list_optimization_rules"},
                {"tool": "list_policy_rules"},
            ]
        ),
        _text_response("Here's everything."),
    ]
    _use_model(monkeypatch, FunctionModel(lambda messages, info: responses.pop(0)))

    messages: list[Any] = []
    turn = _run_turn(messages, "give me spend and rules", session_id="s2multi")

    assert [s.name for s in turn.steps] == [
        "query_metric",
        "list_optimization_rules",
        "list_policy_rules",
    ]
    assert turn.steps[0].rows is not None and len(turn.steps[0].rows) == 1
    assert len(messages) == 2  # still just the clean [question, answer] pair


def test_run_turn_recovers_from_an_invalid_plan_step_via_output_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bad step (an unknown discriminator value) fails Plan's own pydantic
    validation before it ever reaches ExecuteNode — pydantic-ai's structured
    output retry gives the model another attempt within the same Plan call,
    the same self-correction behavior the old native-tool-calling loop relied
    on, now happening at the schema-validation layer instead."""
    responses = [
        ModelResponse(
            parts=[
                ToolCallPart(
                    tool_name="final_result_Plan",
                    args={"steps": [{"tool": "not_a_real_tool"}]},
                    tool_call_id="c1",
                )
            ],
            usage=RequestUsage(input_tokens=10, output_tokens=5),
        ),
        _plan_response([{"tool": "list_optimization_rules"}]),
        _text_response("recovered"),
    ]
    _use_model(monkeypatch, FunctionModel(lambda messages, info: responses.pop(0)))

    messages: list[Any] = []
    result = _run_turn(messages, "hi", session_id="sretry")

    assert result.text == "recovered"


def test_run_turn_returns_options_from_a_clarifying_question_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The model resolves via the structured ClarifyingQuestion output type,
    not free text with a '?' — the UI renders result.options as clickable
    chips instead of parsing prose."""
    _use_model(
        monkeypatch,
        FunctionModel(
            lambda messages, info: _clarify_response(
                "Which time window?", ["Last month", "Year to date"]
            )
        ),
    )

    messages: list[Any] = []
    result = _run_turn(messages, "what did I spend?", session_id="s2f")

    assert result.text == "Which time window?"
    assert result.options == ["Last month", "Year to date"]
    assert result.steps == []  # not surfaced as a debug tool-call step


def test_run_turn_cannot_ask_a_second_clarification_when_answering_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression test: a live Databricks gpt-oss-20b answered the option
    "Total spend across AWS & Databricks for the previous month (default)" —
    which the user had just clicked, and which is already unambiguous — with
    *another* clarifying question ("which exact month?"), leaving the user
    stuck in a clarify loop. answering_clarification=True removes
    ClarifyingQuestion from the offered output types entirely, so the schema
    has no room to ask again."""
    offered: list[list[str]] = []

    def fn(messages: Any, info: Any) -> ModelResponse:
        names = [t.name for t in info.output_tools]
        offered.append(names)
        if not names:
            return _text_response("You spent $10.")
        return _plan_response([{"tool": "list_optimization_rules"}])

    _use_model(monkeypatch, FunctionModel(fn))

    messages: list[Any] = []
    result = _run_turn(
        messages,
        "Total spend across AWS & Databricks for the previous month (default)",
        session_id="sclar",
        answering_clarification=True,
    )

    assert result.text == "You spent $10."
    assert result.options == []
    assert "final_result_ClarifyingQuestion" not in offered[0]
    assert "final_result_Plan" in offered[0]


def test_plan_instructions_ground_the_model_in_today_and_the_real_month_range() -> None:
    """Regression test: a model has no idea what day it is, so "the previous
    month" is unanswerable to it — confirmed live, one asked "if today is
    August 2026, do you mean July 2026?" instead of just answering. Stating
    today's date plus the charge months actually present removes the whole
    class of question at the root."""
    _ingest_may_aws()

    window_line = chat_engine._data_window_line()  # noqa: SLF001
    assert "Today is" in window_line
    assert "2026-05" in window_line  # the one month the fixture publishes
    assert "never ask the user which month they meant" in window_line

    instructions = chat_engine._plan_instructions(  # noqa: SLF001
        allow_explore=True, allow_clarify=True, explored={}, tool_catalog=""
    )
    assert window_line in instructions


def test_data_window_line_works_before_any_data_is_published() -> None:
    """No GOLD published yet — the date still grounds the model, and the
    month-range sentence is simply absent rather than raising."""
    line = chat_engine._data_window_line()  # noqa: SLF001
    assert "Today is" in line
    assert "Charge months present" not in line


def test_run_turn_runs_an_explore_round_before_committing_to_a_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the model doesn't know a valid filter value, it returns an
    ExploreRequest instead of guessing — ExploreNode resolves it
    deterministically (no LLM round to decide what to look up) via a real
    list_dimension_values call, and the discovered values get threaded into a
    second, must-commit PlanNode call (ExploreRequest is no longer offered)."""
    from flashlight.transform.catalog import current_catalog

    _ingest_may_aws()
    view = next(v for v in current_catalog() if v.name == "aws.monthly_bill")
    dimension = view.dimensions[0]

    captured_instructions: list[str | None] = []

    def fn(messages: Any, info: Any) -> ModelResponse:
        captured_instructions.append(messages[-1].instructions)
        names = {t.name for t in info.output_tools}
        if not names:
            return _text_response("done")
        if "final_result_ExploreRequest" in names:
            return _explore_response([{"name": "aws.monthly_bill", "dimension": dimension}])
        assert "final_result_ExploreRequest" not in names  # capped at one explore round
        return _plan_response([{"tool": "list_optimization_rules"}])

    _use_model(monkeypatch, FunctionModel(fn))

    messages: list[Any] = []
    result = _run_turn(messages, f"what {dimension} values are there?", session_id="sx")

    assert result.text == "done"
    # explore-eligible plan call, must-commit replan call, synthesize call
    assert len(captured_instructions) == 3
    assert "Discovered dimension values" in (captured_instructions[1] or "")
    assert len(messages) == 2  # explore + replan still collapse to one clean [question, answer]


def test_run_turn_stops_at_round_limit_on_a_stalling_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """A model that never produces a valid structured output (PlanNode's
    output_type never includes plain str) exhausts pydantic-ai's own
    output-validation retry budget and/or _OUTPUT_REQUEST_LIMIT — run_turn
    catches both exception shapes identically."""
    _use_model(monkeypatch, FunctionModel(lambda messages, info: _text_response("stalling")))

    messages: list[Any] = []
    result = _run_turn(messages, "loop forever?", session_id="s4")

    assert "round limit" in result.text
    assert result.steps == []
    rows = _chat_turn_rows()
    assert rows[0]["tool_call_count"] == 0


def test_run_turn_survives_an_empty_round_via_the_quirk_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression test: Databricks-served gpt-oss has been observed dropping a
    round entirely under real conversational load — no content, no tool call.
    _QuirkTransport retries it silently at the wire level; the turn still
    produces a real answer, exercised end to end through run_turn's real
    openai_compatible branch (only the innermost HTTP call is faked)."""
    bodies = [
        _empty_body(),
        _plan_body([{"tool": "list_optimization_rules"}]),
        _text_body("Here's the real answer."),
    ]
    result = _run_turn_openai_compatible(monkeypatch, bodies)
    assert result.text == "Here's the real answer."


def test_run_turn_does_not_let_empty_rounds_starve_real_tool_call_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression test: before decoupling, a flaky provider dropping a few
    rounds in a row could burn the same fixed budget a legitimate multi-step
    turn needed. 3 empty rounds are absorbed silently by _QuirkTransport
    (invisible to UsageLimits) before the Plan call — both planned steps must
    still execute."""
    bodies = (
        [_empty_body(), _empty_body(), _empty_body()]
        + [
            _plan_body(
                [
                    {"tool": "list_optimization_rules"},
                    {"tool": "list_policy_rules"},
                ]
            )
        ]
        + [_text_body("Here's the comparison.")]
    )
    result = _run_turn_openai_compatible(monkeypatch, bodies, question="compare growth")
    assert result.text == "Here's the comparison."
    assert len(result.steps) == 2  # both planned steps survived the 3 empty retries


def test_run_turn_gives_up_after_repeated_empty_rounds(monkeypatch: pytest.MonkeyPatch) -> None:
    """Once _QuirkTransport's own retry budget (EMPTY_ROUND_RETRY_LIMIT) is
    exhausted for a round, it hands back the still-empty response — pydantic-ai
    treats that as an invalid output and gives up via UnexpectedModelBehavior
    (its own, much smaller output-retry budget), not UsageLimitExceeded.
    run_turn catches both the same way."""
    result = _run_turn_openai_compatible(monkeypatch, [_empty_body()] * 20)
    assert "round limit" in result.text


def test_quirk_transport_strips_leaked_harmony_channel_marker_from_tool_name() -> None:
    """Regression test: Databricks-served gpt-oss has been observed leaking
    Harmony format markers (e.g. "query_metric<|channel|>analysis") into a
    tool call's name — fixed at the wire level before pydantic-ai/the OpenAI
    SDK ever parses the response."""
    body = _tool_call_body("query_metric<|channel|>analysis", {}, "call_1")
    transport = chat_engine._QuirkTransport(inner=_QueueTransport([body]))  # noqa: SLF001

    async def _check() -> httpx.Response:
        return await transport.handle_async_request(
            httpx.Request("POST", "http://fake.local/v1/chat/completions", json={})
        )

    response = asyncio.run(_check())
    parsed = json.loads(response.content)
    assert parsed["choices"][0]["message"]["tool_calls"][0]["function"]["name"] == "query_metric"


def test_quirk_transport_strips_stale_content_encoding_after_rewriting_the_body() -> None:
    """Regression test: a real gzip-compressing endpoint (confirmed against a
    live Databricks workspace) sends "content-encoding: gzip" — reading the
    response transparently decompresses it, but the *headers* still claim
    gzip. Carrying that stale header onto the rewritten (already-plain) body
    made a downstream reader try to gunzip already-plain JSON and fail with
    "Error -3 while decompressing data: incorrect header check". No local test
    fixture compressed its canned bodies before this, so the bug never surfaced
    until it hit a real server."""
    compressed = gzip.compress(json.dumps(_text_body("hi")).encode())

    class _GzipTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-encoding": "gzip", "content-type": "application/json"},
                content=compressed,
                request=request,
            )

    transport = chat_engine._QuirkTransport(inner=_GzipTransport())  # noqa: SLF001

    async def _check() -> httpx.Response:
        return await transport.handle_async_request(
            httpx.Request("POST", "http://fake.local/v1/chat/completions", json={})
        )

    response = asyncio.run(_check())
    assert "content-encoding" not in response.headers
    # Reading it back must not try to decompress already-plain bytes.
    assert json.loads(response.content)["choices"][0]["message"]["content"] == "hi"


def test_quirk_transport_gives_up_and_returns_the_last_empty_response() -> None:
    """Once EMPTY_ROUND_RETRY_LIMIT is exceeded, the transport stops retrying
    and hands back whatever it last got, rather than retrying forever."""
    bodies = [_empty_body()] * (chat_engine.EMPTY_ROUND_RETRY_LIMIT + 1)
    transport = chat_engine._QuirkTransport(inner=_QueueTransport(bodies))  # noqa: SLF001

    async def _check() -> httpx.Response:
        return await transport.handle_async_request(
            httpx.Request("POST", "http://fake.local/v1/chat/completions", json={})
        )

    response = asyncio.run(_check())
    parsed = json.loads(response.content)
    message = parsed["choices"][0]["message"]
    assert not message.get("content")
    assert not message.get("tool_calls")


def test_quirk_transport_flattens_reasoning_only_content_parts() -> None:
    """Regression test: a live Databricks gpt-oss response has been observed
    sending `content` as a list of typed parts (e.g. `[{"type": "reasoning",
    "text": "..."}]`) instead of a plain string — pydantic-ai's OpenAI-compatible
    parser expects `content: str | None` and warned/choked on the list. A
    reasoning-only part list also isn't a real answer, so it must be treated
    the same as an empty round (retried), not passed through unusable."""
    body = _reasoning_only_body("thinking about it, keep options simple.")
    transport = chat_engine._QuirkTransport(inner=_QueueTransport([body] * 4))  # noqa: SLF001

    async def _check() -> httpx.Response:
        return await transport.handle_async_request(
            httpx.Request("POST", "http://fake.local/v1/chat/completions", json={})
        )

    response = asyncio.run(_check())
    message = json.loads(response.content)["choices"][0]["message"]
    # content is a plain (empty, since only a reasoning part was ever sent) string,
    # never the raw list pydantic-ai's OpenAI-compatible parser can't validate.
    assert message["content"] == ""
    assert not message.get("tool_calls")


def test_run_turn_survives_a_reasoning_only_round_via_the_quirk_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: a reasoning-only round is retried the same way an empty one
    is, and a real answer on a later round still comes through."""
    bodies = [
        _reasoning_only_body("still deciding..."),
        _plan_body([{"tool": "list_optimization_rules"}]),
        _text_body("Here's the real answer."),
    ]
    result = _run_turn_openai_compatible(monkeypatch, bodies)
    assert result.text == "Here's the real answer."


def test_run_turn_returns_error_text_on_request_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def raises(messages: Any, info: Any) -> ModelResponse:
        raise RuntimeError("bad api key")

    _use_model(monkeypatch, FunctionModel(raises))

    messages: list[Any] = []
    result = _run_turn(messages, "hi", api_key="sk-bad", session_id="s5")

    assert "bad api key" in result.text
    assert len(_chat_turn_rows()) == 1


def test_run_turn_redacts_the_api_key_from_a_leaky_provider_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression test: OpenAI's real AuthenticationError echoes the submitted
    key back verbatim (e.g. "Incorrect API key provided: sk-bad-secret...").
    That must never reach the chat UI or the conversation history."""

    def raises(messages: Any, info: Any) -> ModelResponse:
        raise RuntimeError(
            "litellm.AuthenticationError: Incorrect API key provided: sk-bad-secret. "
            "You can find your API key at https://platform.openai.com/account/api-keys."
        )

    _use_model(monkeypatch, FunctionModel(raises))

    messages: list[Any] = []
    result = _run_turn(messages, "hi", api_key="sk-bad-secret", session_id="s6")

    assert "sk-bad-secret" not in result.text
    assert "[REDACTED]" in result.text
    assert "Incorrect API key provided" in result.text  # the useful part survives
    assert not any("sk-bad-secret" in str(m) for m in messages)


def test_run_turn_replans_once_when_every_step_returns_no_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression test: a live gpt-oss-20b filtered `provider_name: "databricks"`
    on data that says "Databricks" — exact, case-sensitive, so zero rows — and
    reported "no records were returned, I'm unable to produce a visualization".
    The same dead end happened with an errored step. One bounded re-plan gets
    the real values handed back so it can fix the filter instead."""
    _ingest_may_aws()
    instructions: list[str | None] = []
    plans = [
        # Wrong case, and redundant on an already-provider-scoped view.
        _plan_response(
            [
                {
                    "tool": "query_metric",
                    "name": "aws.monthly_bill",
                    "filters": {"provider_name": "aws"},
                }
            ]
        ),
        _plan_response([{"tool": "query_metric", "name": "aws.monthly_bill"}]),
    ]

    def fn(messages: Any, info: Any) -> ModelResponse:
        instructions.append(messages[-1].instructions)
        if not info.output_tools:
            return _text_response("You spent $10 in May.")
        return plans.pop(0)

    _use_model(monkeypatch, FunctionModel(fn))

    result = _run_turn([], "what did I spend?", session_id="sdata")

    assert result.text == "You spent $10 in May."
    # The retry's instructions name the real value, not just "no rows".
    retry_instructions = instructions[1] or ""
    assert "produced no usable data" in retry_instructions
    assert "provider_name actually contains ['AWS']" in retry_instructions
    # The second plan's rows are what got answered from.
    assert [s.name for s in result.steps] == ["query_metric"]
    assert result.steps[0].rows


def test_run_turn_does_not_replan_when_some_data_came_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The re-plan is only for a total dead end — a partially useful plan must go
    straight to the answer rather than paying for another round."""
    _ingest_may_aws()
    plan_calls = 0

    def fn(messages: Any, info: Any) -> ModelResponse:
        nonlocal plan_calls
        if not info.output_tools:
            return _text_response("Here you go.")
        plan_calls += 1
        return _plan_response(
            [
                {"tool": "query_metric", "name": "aws.monthly_bill"},
                {"tool": "query_metric", "name": "aws.monthly_bill", "filters": {"nope": "x"}},
            ]
        )

    _use_model(monkeypatch, FunctionModel(fn))
    result = _run_turn([], "what did I spend?", session_id="spartial")

    assert result.text == "Here you go."
    assert plan_calls == 1  # no re-plan: one step returned rows
    assert len(result.steps) == 2
    assert any(s.error for s in result.steps)  # the bad filter still surfaces


def test_run_turn_accepts_a_second_dead_end_without_looping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bounded at one: if the re-planned query is also empty, answer honestly
    rather than looping and burning the user's tokens."""
    _ingest_may_aws()
    plan_calls = 0

    def fn(messages: Any, info: Any) -> ModelResponse:
        nonlocal plan_calls
        if not info.output_tools:
            return _text_response("I couldn't find matching data.")
        plan_calls += 1
        return _plan_response(
            [
                {
                    "tool": "query_metric",
                    "name": "aws.monthly_bill",
                    "filters": {"charge_month": "1999-01-01"},
                }
            ]
        )

    _use_model(monkeypatch, FunctionModel(fn))
    result = _run_turn([], "spend in 1999?", session_id="sdead")

    assert result.text == "I couldn't find matching data."
    assert plan_calls == 2  # the original plus exactly one re-plan
