from __future__ import annotations

import asyncio
import calendar
import json
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from litellm.types.utils import (
    ChatCompletionMessageToolCall,
    Choices,
    Function,
    Message,
    ModelResponse,
    Usage,
)

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
    chat_engine._tool_schemas_cache = None  # noqa: SLF001 - reset the module-level cache between tests
    yield
    get_settings.cache_clear()


def _text_response(text: str, *, prompt: int = 10, completion: int = 5) -> ModelResponse:
    return ModelResponse(
        choices=[Choices(message=Message(role="assistant", content=text))],
        usage=Usage(
            prompt_tokens=prompt, completion_tokens=completion, total_tokens=prompt + completion
        ),
    )


def _tool_call_response(name: str, arguments: dict[str, object]) -> ModelResponse:
    call = ChatCompletionMessageToolCall(
        id="call_1", type="function", function=Function(name=name, arguments=json.dumps(arguments))
    )
    return ModelResponse(
        choices=[Choices(message=Message(role="assistant", content=None, tool_calls=[call]))],
        usage=Usage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
    )


def _focus_record(day: int, *, month: int = 5) -> FocusRecord:
    when = datetime(2026, month, day, tzinfo=UTC)
    period_end = date(2026, month, calendar.monthrange(2026, month)[1])
    return FocusRecord(
        provider_name=ProviderName.AWS,
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


def _chat_turn_rows() -> list[dict[str, object]]:
    con = duck.connect()
    try:
        duck.register_chat_turns(con)
        return con.execute("SELECT * FROM telemetry.chat_turn").fetchdf().to_dict(  # type: ignore[no-any-return]
            "records"
        )
    finally:
        con.close()


def test_tool_schemas_cover_the_seven_mcp_tools() -> None:
    schemas = asyncio.run(chat_engine.tool_schemas())
    names = {s["function"]["name"] for s in schemas}
    assert names == {
        "list_metrics",
        "describe_metric",
        "query_metric",
        "list_dimension_values",
        "list_optimization_rules",
        "list_policy_rules",
        "run_sql",
    }
    assert all(s["type"] == "function" for s in schemas)
    assert all("parameters" in s["function"] for s in schemas)


def test_run_turn_inserts_system_prompt_once_across_turns(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    async def fake_acompletion(**kwargs):  # type: ignore[no-untyped-def]
        return _text_response("ok")

    monkeypatch.setattr(chat_engine, "_acompletion", fake_acompletion)

    messages = [{"role": "user", "content": "hello"}]
    asyncio.run(
        chat_engine.run_turn(
            messages, api_key="sk-test", model="openai/gpt-4o", base_url=None, session_id="sp1"
        )
    )
    asyncio.run(
        chat_engine.run_turn(
            messages, api_key="sk-test", model="openai/gpt-4o", base_url=None, session_id="sp1"
        )
    )

    system_messages = [m for m in messages if m.get("role") == "system"]
    assert len(system_messages) == 1
    assert messages[0]["role"] == "system"


def test_run_turn_grounds_the_system_prompt_in_actually_connected_providers(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Regression test: the model has been observed offering "GCP"/"Azure" as
    clarifying-question options in an instance that only has AWS connected —
    it pattern-matches common cloud names instead of using real data. The
    system prompt must name only what's actually published, sourced the same
    live way the nav sidebar is (discover_provider_groups)."""

    async def fake_acompletion(**kwargs):  # type: ignore[no-untyped-def]
        return _text_response("ok")

    monkeypatch.setattr(chat_engine, "_acompletion", fake_acompletion)

    messages = [{"role": "user", "content": "hello"}]
    asyncio.run(
        chat_engine.run_turn(
            messages, api_key="sk-test", model="openai/gpt-4o", base_url=None, session_id="s3a"
        )
    )
    assert "No providers are connected yet" in messages[0]["content"]

    from flashlight.lake import bronze
    from flashlight.transform.runner import build_gold

    window = IngestWindow(date(2026, 5, 1), date(2026, 5, 31))
    bronze.write_window("t", window, [_focus_record(15)], ingest_run_id="r1")
    build_gold()

    asyncio.run(
        chat_engine.run_turn(
            messages, api_key="sk-test", model="openai/gpt-4o", base_url=None, session_id="s3a"
        )
    )
    connected_line = messages[0]["content"].splitlines()[-1]
    assert connected_line == "Connected providers: AWS. Never mention or offer any other provider."


def test_run_turn_without_tool_call_logs_one_row(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    async def fake_acompletion(**kwargs):  # type: ignore[no-untyped-def]
        return _text_response("The answer is 42.")

    monkeypatch.setattr(chat_engine, "_acompletion", fake_acompletion)

    messages = [{"role": "user", "content": "hello"}]
    result = asyncio.run(
        chat_engine.run_turn(
            messages, api_key="sk-test", model="openai/gpt-4o", base_url=None, session_id="s1"
        )
    )

    assert result.text == "The answer is 42."
    assert result.steps == []
    rows = _chat_turn_rows()
    assert len(rows) == 1
    assert rows[0]["session_id"] == "s1"
    assert rows[0]["model"] == "openai/gpt-4o"
    assert rows[0]["prompt_tokens"] == 10
    assert rows[0]["completion_tokens"] == 5
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


def test_run_turn_executes_real_tool_call_in_process(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    responses = [
        _tool_call_response("list_metrics", {}),
        _text_response("You have several metrics available.", prompt=20, completion=8),
    ]

    async def fake_acompletion(**kwargs):  # type: ignore[no-untyped-def]
        return responses.pop(0)

    monkeypatch.setattr(chat_engine, "_acompletion", fake_acompletion)

    messages = [{"role": "user", "content": "what metrics do you have?"}]
    turn = asyncio.run(
        chat_engine.run_turn(
            messages, api_key="sk-test", model="openai/gpt-4o", base_url=None, session_id="s2"
        )
    )

    assert turn.text == "You have several metrics available."
    # The tool result actually came from the real mcp.call_tool, not a stub —
    # confirmed by unwrapping the real list_metrics() catalog it returned (the
    # fixed groups, since no GOLD has been built in this temp lake).
    tool_message = next(m for m in messages if m.get("role") == "tool")
    parsed = json.loads(tool_message["content"])
    assert "shared.tco_summary_month" in {m["name"] for m in parsed["result"]}

    # list_metrics returns a wrapped list, not a "rows" dict — no chartable data.
    assert turn.steps == [ToolStep(name="list_metrics", arguments={}, rows=None, error=None)]

    rows = _chat_turn_rows()
    assert len(rows) == 1
    assert rows[0]["tool_call_count"] == 1
    assert rows[0]["prompt_tokens"] == 30  # accumulated across both round trips
    assert rows[0]["completion_tokens"] == 13


def test_run_turn_captures_rows_from_a_real_query_metric_call(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from flashlight.lake import bronze
    from flashlight.transform.runner import build_gold

    window = IngestWindow(date(2026, 5, 1), date(2026, 5, 31))
    bronze.write_window("t", window, [_focus_record(15)], ingest_run_id="r1")
    build_gold()

    responses = [
        _tool_call_response("query_metric", {"name": "aws.monthly_bill", "limit": 10}),
        _text_response("You spent $10 in May."),
    ]

    async def fake_acompletion(**kwargs):  # type: ignore[no-untyped-def]
        return responses.pop(0)

    monkeypatch.setattr(chat_engine, "_acompletion", fake_acompletion)

    messages = [{"role": "user", "content": "what did I spend in May?"}]
    turn = asyncio.run(
        chat_engine.run_turn(
            messages, api_key="sk-test", model="openai/gpt-4o", base_url=None, session_id="s2b"
        )
    )

    assert len(turn.steps) == 1
    step = turn.steps[0]
    assert step.name == "query_metric"
    assert step.error is None
    assert step.rows is not None
    assert len(step.rows) == 1
    assert step.rows[0]["net_cost"] == 10.0


def test_run_turn_accepts_order_by_as_a_list(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Regression test: models commonly send order_by as a list (e.g.
    ["charge_month"]) even though sorting is single-column — the real
    MCP-backed call must still succeed instead of a Pydantic validation error."""
    from flashlight.lake import bronze
    from flashlight.transform.runner import build_gold

    window = IngestWindow(date(2026, 5, 1), date(2026, 5, 31))
    bronze.write_window("t", window, [_focus_record(15)], ingest_run_id="r1")
    build_gold()

    responses = [
        _tool_call_response(
            "query_metric",
            {"name": "aws.monthly_bill", "limit": 10, "order_by": ["charge_month"]},
        ),
        _text_response("You spent $10 in May."),
    ]

    async def fake_acompletion(**kwargs):  # type: ignore[no-untyped-def]
        return responses.pop(0)

    monkeypatch.setattr(chat_engine, "_acompletion", fake_acompletion)

    messages = [{"role": "user", "content": "what did I spend in May, sorted by month?"}]
    turn = asyncio.run(
        chat_engine.run_turn(
            messages, api_key="sk-test", model="openai/gpt-4o", base_url=None, session_id="s2g"
        )
    )

    assert turn.steps[0].error is None
    assert turn.steps[0].rows is not None
    assert len(turn.steps[0].rows) == 1


def test_run_turn_narrows_columns_with_measures_param(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """monthly_bill has five measures (net/gross/credit/list/savings) — without
    narrowing, query_metric returns all of them, which is too wide for
    _render_rows' one-dimension/one-measure chart heuristic. `measures` lets
    the model ask for just one."""
    from flashlight.lake import bronze
    from flashlight.transform.runner import build_gold

    window = IngestWindow(date(2026, 5, 1), date(2026, 5, 31))
    bronze.write_window("t", window, [_focus_record(15)], ingest_run_id="r1")
    build_gold()

    responses = [
        _tool_call_response(
            "query_metric",
            {"name": "aws.monthly_bill", "measures": ["net_cost"]},
        ),
        _text_response("You spent $10 in May."),
    ]

    async def fake_acompletion(**kwargs):  # type: ignore[no-untyped-def]
        return responses.pop(0)

    monkeypatch.setattr(chat_engine, "_acompletion", fake_acompletion)

    messages = [{"role": "user", "content": "chart my spend"}]
    turn = asyncio.run(
        chat_engine.run_turn(
            messages, api_key="sk-test", model="openai/gpt-4o", base_url=None, session_id="s2i"
        )
    )

    assert turn.steps[0].error is None
    assert turn.steps[0].rows is not None
    row = turn.steps[0].rows[0]
    assert "net_cost" in row
    assert "gross_cost" not in row and "savings" not in row


def test_run_turn_accepts_a_list_filter_value_as_an_in_match(monkeypatch) -> None:  # type: ignore[no-untyped-def]
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
        _tool_call_response(
            "query_metric",
            {
                "name": "aws.monthly_bill",
                "measures": ["net_cost"],
                "filters": {"charge_month": ["2026-05-01", "2026-06-01"]},
            },
        ),
        _text_response("You spent $20 across May and June."),
    ]

    async def fake_acompletion(**kwargs):  # type: ignore[no-untyped-def]
        return responses.pop(0)

    monkeypatch.setattr(chat_engine, "_acompletion", fake_acompletion)

    messages = [{"role": "user", "content": "what did I spend in May and June?"}]
    turn = asyncio.run(
        chat_engine.run_turn(
            messages, api_key="sk-test", model="openai/gpt-4o", base_url=None, session_id="s2j"
        )
    )

    assert turn.steps[0].error is None
    assert turn.steps[0].rows is not None
    assert len(turn.steps[0].rows) == 2


def test_run_turn_strips_leaked_harmony_channel_marker_from_tool_name(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Regression test: Databricks-served gpt-oss has been observed leaking
    Harmony format markers (e.g. "query_metric<|channel|>analysis") into
    tool_call.function.name instead of a clean tool name. The real tool must
    still get called rather than erroring as unknown."""
    responses = [
        _tool_call_response("list_metrics<|channel|>analysis", {}),
        _text_response("You have several metrics available."),
    ]

    async def fake_acompletion(**kwargs):  # type: ignore[no-untyped-def]
        return responses.pop(0)

    monkeypatch.setattr(chat_engine, "_acompletion", fake_acompletion)

    messages = [{"role": "user", "content": "what metrics do you have?"}]
    turn = asyncio.run(
        chat_engine.run_turn(
            messages, api_key="sk-test", model="databricks/gpt-oss", base_url=None, session_id="s2c"
        )
    )

    assert turn.steps == [ToolStep(name="list_metrics", arguments={}, rows=None, error=None)]
    assert turn.text == "You have several metrics available."


def test_run_turn_retries_when_a_round_has_no_content_and_no_tool_call(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Regression test: Databricks-served gpt-oss has been observed dropping a
    round entirely — empty `content` and no tool_calls, with only its internal
    reasoning trace surviving in `reasoning_content`. That trace must never be
    shown as if it were a considered answer; the round should retry instead."""

    def _empty_round() -> ModelResponse:
        return ModelResponse(
            choices=[
                Choices(
                    message=Message(
                        role="assistant",
                        content=None,
                        reasoning_content="Thus call query_metric with name='aws.monthly_bill'...",
                    )
                )
            ],
            usage=Usage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        )

    responses = [_empty_round(), _text_response("Here's the real answer.")]

    async def fake_acompletion(**kwargs):  # type: ignore[no-untyped-def]
        return responses.pop(0)

    monkeypatch.setattr(chat_engine, "_acompletion", fake_acompletion)

    messages = [{"role": "user", "content": "hi"}]
    result = asyncio.run(
        chat_engine.run_turn(
            messages, api_key="sk-test", model="databricks/gpt-oss", base_url=None, session_id="s2d"
        )
    )

    assert result.text == "Here's the real answer."
    assert "query_metric" not in result.text  # the raw reasoning trace never surfaces


def test_run_turn_gives_up_after_repeated_empty_rounds(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    async def fake_acompletion(**kwargs):  # type: ignore[no-untyped-def]
        return ModelResponse(
            choices=[Choices(message=Message(role="assistant", content=None))],
            usage=Usage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        )

    monkeypatch.setattr(chat_engine, "_acompletion", fake_acompletion)

    messages = [{"role": "user", "content": "hi"}]
    result = asyncio.run(
        chat_engine.run_turn(
            messages, api_key="sk-test", model="openai/gpt-4o", base_url=None, session_id="s2e"
        )
    )

    assert "round limit" in result.text


def test_run_turn_handles_unknown_tool_gracefully(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    responses = [
        _tool_call_response("not_a_real_tool", {}),
        _text_response("Sorry, I couldn't find that."),
    ]

    async def fake_acompletion(**kwargs):  # type: ignore[no-untyped-def]
        return responses.pop(0)

    monkeypatch.setattr(chat_engine, "_acompletion", fake_acompletion)

    messages = [{"role": "user", "content": "do something weird"}]
    result = asyncio.run(
        chat_engine.run_turn(
            messages, api_key="sk-test", model="openai/gpt-4o", base_url=None, session_id="s3"
        )
    )

    assert result.text == "Sorry, I couldn't find that."
    assert result.steps[0].error is not None
    tool_message = next(m for m in messages if m.get("role") == "tool")
    assert "error" in tool_message["content"]


def test_run_turn_returns_options_from_ask_clarifying_question(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """The model asks via a structured tool call, not free text with a '?' —
    the UI renders result.options as clickable chips instead of parsing prose."""
    responses = [
        _tool_call_response(
            "ask_clarifying_question",
            {
                "question": "Which time window?",
                "options": ["Last month", "Year to date"],
            },
        ),
    ]

    async def fake_acompletion(**kwargs):  # type: ignore[no-untyped-def]
        return responses.pop(0)

    monkeypatch.setattr(chat_engine, "_acompletion", fake_acompletion)

    messages = [{"role": "user", "content": "what did I spend?"}]
    result = asyncio.run(
        chat_engine.run_turn(
            messages, api_key="sk-test", model="openai/gpt-4o", base_url=None, session_id="s2f"
        )
    )

    assert result.text == "Which time window?"
    assert result.options == ["Last month", "Year to date"]
    assert result.steps == []  # not surfaced as a debug tool-call step
    # History stays structurally valid: the tool_call has a matching tool reply.
    tool_message = next(m for m in messages if m.get("role") == "tool")
    assert tool_message["content"] == "Waiting for the user's answer."


def test_run_turn_stops_at_max_tool_rounds(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    async def always_calls_a_tool(**kwargs):  # type: ignore[no-untyped-def]
        return _tool_call_response("list_metrics", {})

    monkeypatch.setattr(chat_engine, "_acompletion", always_calls_a_tool)

    messages = [{"role": "user", "content": "loop forever?"}]
    result = asyncio.run(
        chat_engine.run_turn(
            messages, api_key="sk-test", model="openai/gpt-4o", base_url=None, session_id="s4"
        )
    )

    assert "round limit" in result.text
    assert len(result.steps) == chat_engine.MAX_TOOL_ROUNDS
    rows = _chat_turn_rows()
    assert rows[0]["tool_call_count"] == chat_engine.MAX_TOOL_ROUNDS


def test_run_turn_returns_error_text_on_request_failure(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    async def raises(**kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("bad api key")

    monkeypatch.setattr(chat_engine, "_acompletion", raises)

    messages = [{"role": "user", "content": "hi"}]
    result = asyncio.run(
        chat_engine.run_turn(
            messages, api_key="sk-bad", model="openai/gpt-4o", base_url=None, session_id="s5"
        )
    )

    assert "bad api key" in result.text
    assert len(_chat_turn_rows()) == 1


def test_run_turn_redacts_the_api_key_from_a_leaky_provider_error(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Regression test: OpenAI's real AuthenticationError echoes the submitted
    key back verbatim (e.g. "Incorrect API key provided: sk-bad-secret...").
    That must never reach the chat UI or the conversation history."""

    async def raises(**kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError(
            "litellm.AuthenticationError: Incorrect API key provided: sk-bad-secret. "
            "You can find your API key at https://platform.openai.com/account/api-keys."
        )

    monkeypatch.setattr(chat_engine, "_acompletion", raises)

    messages = [{"role": "user", "content": "hi"}]
    result = asyncio.run(
        chat_engine.run_turn(
            messages,
            api_key="sk-bad-secret",
            model="openai/gpt-4o",
            base_url=None,
            session_id="s6",
        )
    )

    assert "sk-bad-secret" not in result.text
    assert "[REDACTED]" in result.text
    assert "Incorrect API key provided" in result.text  # the useful part survives
    assert not any("sk-bad-secret" in str(m) for m in messages)
