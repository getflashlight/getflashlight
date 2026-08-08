"""The profiler's own reporting, verified before it's pointed at a paid endpoint.

Its whole reason to exist is the verdict on whether a real model fills
``SummarySpec`` and whether that sentence was *used* or silently rejected. An
instrument that reported "not declared" for a turn that did declare one would send
someone off fixing a prompt that was already working — so the capture wrappers are
tested here rather than trusted.

The HTTP layer is not exercised (it's smoke-tested by running the script against a
closed port); what's covered is everything the trim-and-complete pass added: the
captured telemetry row and the SummarySpec verdict.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.usage import RequestUsage

from flashlight.core.settings import get_settings
from flashlight.focus.enums import ChargeCategory, ComputeClass, ProviderName, ServiceCategory
from flashlight.focus.model import FocusRecord
from flashlight.ingest.base import IngestWindow
from scripts import profile_assistant_turn as prof


@pytest.fixture
def lake_with_two_months(tmp_path, monkeypatch) -> Iterator[None]:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("FLASHLIGHT_HOME", str(tmp_path))
    get_settings.cache_clear()

    from flashlight.lake import bronze
    from flashlight.transform.runner import build_gold

    def record(day: int, month: int, cost: str) -> FocusRecord:
        when = datetime(2026, month, day, tzinfo=UTC)
        return FocusRecord(
            provider_name=ProviderName.AWS,
            billing_account_id="acct",
            billing_period_start=date(2026, month, 1),
            billing_period_end=date(2026, month, 28),
            charge_period_start=when,
            charge_period_end=when,
            billed_cost=Decimal(cost),
            effective_cost=Decimal(cost),
            list_cost=Decimal(cost),
            charge_category=ChargeCategory.USAGE,
            service_category=ServiceCategory.COMPUTE,
            service_name="AmazonEC2",
            tags={},
            x_compute_class=ComputeClass.NOT_APPLICABLE,
            x_source_connector="t",
        )

    bronze.write_window(
        "t",
        IngestWindow(date(2026, 4, 1), date(2026, 5, 31)),
        [record(15, 4, "100"), record(15, 5, "300")],
        ingest_run_id="r1",
    )
    build_gold()
    yield
    get_settings.cache_clear()


def _plan_with(summary: dict[str, Any] | str | None) -> ModelResponse:
    step: dict[str, Any] = {
        "tool": "query_metric",
        "name": "aws.monthly_bill",
        "measures": ["net_cost"],
        "limit": 10,
    }
    if summary is not None:
        step["summary"] = summary
    return ModelResponse(
        parts=[ToolCallPart("final_result_Plan", {"steps": [step]}, tool_call_id="c1")],
        usage=RequestUsage(input_tokens=10, output_tokens=5),
    )


def _run(monkeypatch: pytest.MonkeyPatch, responses: list[ModelResponse]) -> prof.Trace:
    """Drive the profiler with a fake model, bypassing only its transport wiring.

    ``_instrumented_model`` is the seam: _profile still installs every capture
    wrapper, so what's under test is the wrappers, not the HTTP plumbing.
    """
    monkeypatch.setattr(
        prof,
        "_instrumented_model",
        lambda *args, **kwargs: FunctionModel(lambda m, i: responses.pop(0)),
    )
    return asyncio.run(
        prof._profile(  # noqa: SLF001
            "how did spend change?",
            provider="openai",
            model="openai/gpt-4o",
            api_key="sk-test",
            base_url=None,
        )
    )


def test_reports_a_declared_sentence_as_used(monkeypatch, lake_with_two_months) -> None:  # type: ignore[no-untyped-def]
    trace = _run(
        monkeypatch,
        [_plan_with({"sentence": "Spend went {first_value} -> {last_value} ({change_pct})."})],
    )

    assert trace.summary_sentence is not None
    assert trace.summary_accepted is True
    assert trace.summary_verdict == "declared and used"
    assert trace.answer == "Spend went $100 -> $300 (+200%)."


def test_reports_a_rejected_sentence_as_rejected(monkeypatch, lake_with_two_months) -> None:  # type: ignore[no-untyped-def]
    """The distinction the turn log can't make: the model *did* declare a sentence,
    it was thrown away for naming a figure it cannot have read, and the fixed
    assembly answered instead. Reported as "REJECTED" so a prompt fix is aimed at
    the right problem."""
    trace = _run(monkeypatch, [_plan_with({"sentence": "Spend rose to $300 last month."})])

    assert trace.summary_sentence == "Spend rose to $300 last month."
    assert trace.summary_accepted is False
    assert "REJECTED" in trace.summary_verdict
    # The answer still landed, from the deterministic assembly.
    assert "total net cost across 2 charge months" in trace.answer


def test_reports_no_declaration_when_omitted(monkeypatch, lake_with_two_months) -> None:  # type: ignore[no-untyped-def]
    trace = _run(monkeypatch, [_plan_with(None)])

    assert trace.summary_sentence is None
    assert trace.summary_verdict == "not declared by the model"
    assert trace.logged["answer_source"] == "caption"


def test_accepts_a_bare_sentence_string(monkeypatch, lake_with_two_months) -> None:  # type: ignore[no-untyped-def]
    trace = _run(monkeypatch, [_plan_with("Spend ended at {last_value}.")])

    assert trace.summary_accepted is True
    assert trace.answer == "Spend ended at $300."


def test_captures_the_telemetry_row_without_writing(monkeypatch, lake_with_two_months) -> None:  # type: ignore[no-untyped-def]
    """Stubbing the writer keeps profiling runs out of the real usage log — but the
    row is kept, so the run still reports the per-phase timing /usage would show."""
    from flashlight.lake import paths

    trace = _run(monkeypatch, [_plan_with({"sentence": "Ended at {last_value}."})])

    assert trace.logged["outcome"] == "answer"
    assert trace.logged["answer_source"] == "summary_spec"
    assert trace.logged["llm_request_count"] == 1
    assert trace.logged["plan_ms"] > 0
    assert trace.logged["synthesize_ms"] == 0
    assert not list(paths.assistant_turns_dir().glob("*.parquet"))


def test_restores_every_patched_seam(monkeypatch, lake_with_two_months) -> None:  # type: ignore[no-untyped-def]
    """The script patches module globals; leaving one installed would corrupt any
    later turn in the same process."""
    from flashlight.dashboard import assistant_engine

    before = {
        name: getattr(assistant_engine, name)
        for name in ("_build_model", "record_assistant_turn", "caption_for")
    }

    _run(monkeypatch, [_plan_with(None)])

    for name, original in before.items():
        assert getattr(assistant_engine, name) is original


def test_falls_back_to_the_model_without_a_caption(monkeypatch, lake_with_two_months) -> None:  # type: ignore[no-untyped-def]
    """An unnarrowed query has several measures, so the caption declines and real
    synthesis answers — the profiler must report that as answered by the model."""
    plan = ModelResponse(
        parts=[
            ToolCallPart(
                "final_result_Plan",
                {"steps": [{"tool": "query_metric", "name": "aws.monthly_bill", "limit": 10}]},
                tool_call_id="c1",
            )
        ],
        usage=RequestUsage(input_tokens=10, output_tokens=5),
    )
    synth = ModelResponse(
        parts=[TextPart("You spent $400 across April and May.")],
        usage=RequestUsage(input_tokens=20, output_tokens=8),
    )

    trace = _run(monkeypatch, [plan, synth])

    assert trace.logged["answer_source"] == "model"
    assert trace.logged["synthesize_ms"] > 0
    assert trace.summary_sentence is None

