"""The caption must be right or absent — never a plausible wrong number.

Every test is either "states the arithmetic correctly" or "declines to state
anything", because the fallback (model synthesis) is merely slower while a wrong
caption is a wrong figure in a spend tool.

Rows here use real catalog view names and real column names, because that's the
contract the caption reads: it takes dimensions, measures and units from
:mod:`flashlight.transform.catalog` rather than inferring them from the values, so
a test with invented column names would exercise nothing.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from flashlight.core.settings import get_settings
from flashlight.dashboard.answer_caption import caption_for, facts_for, render_sentence
from flashlight.dashboard.assistant_engine import ToolStep


@pytest.fixture(autouse=True)
def catalog_with_aws(tmp_path, monkeypatch) -> Iterator[None]:  # type: ignore[no-untyped-def]
    """An isolated lake, plus an ``aws`` provider group in the catalog.

    FLASHLIGHT_HOME is redirected so these tests never read the developer's real
    lake (which would make them depend on whatever happens to be published), and
    the provider group is injected rather than ingested — the caption only needs
    the catalog's *declarations*, not any data on disk.
    """
    monkeypatch.setenv("FLASHLIGHT_HOME", str(tmp_path))
    get_settings.cache_clear()
    monkeypatch.setattr(
        "flashlight.transform.catalog.discover_provider_groups", lambda: ["aws"]
    )
    yield
    get_settings.cache_clear()


def _two_months() -> list[dict[str, Any]]:
    return [
        {"charge_month": "2024-04", "net_cost": 1.0},
        {"charge_month": "2024-05", "net_cost": 2.0},
    ]


def _step(view: str, rows: list[dict[str, Any]] | None, **overrides: Any) -> ToolStep:
    kwargs: dict[str, Any] = {
        "name": "query_metric",
        "arguments": {"name": view},
        "rows": rows,
    }
    kwargs.update(overrides)
    return ToolStep(**kwargs)


def test_captions_a_monthly_trend_with_first_to_last_change() -> None:
    caption = caption_for(
        [
            _step(
                "aws.monthly_bill",
                [
                    {"provider_name": "AWS", "charge_month": "2024-04", "net_cost": 12001.0},
                    {"provider_name": "AWS", "charge_month": "2024-09", "net_cost": 18940.0},
                ],
            )
        ]
    )
    assert caption is not None
    assert "$30,941 total net cost across 2 charge months" in caption
    assert "2024-04 $12,001 -> 2024-09 $18,940 (+58%)" in caption
    # No "top month": one of two months being larger is what the trend just said.
    assert "top:" not in caption


def test_trend_reads_forward_even_when_rows_arrive_newest_first() -> None:
    """A plan can carry any order_by, so row order is not chronology — a descending
    result would otherwise render the trend backwards."""
    caption = caption_for(
        [
            _step(
                "aws.monthly_bill",
                [
                    {"charge_month": "2024-09", "net_cost": 18940.0},
                    {"charge_month": "2024-04", "net_cost": 12001.0},
                ],
            )
        ]
    )
    assert caption is not None
    assert "2024-04 $12,001 -> 2024-09 $18,940" in caption


def test_captions_a_service_by_month_cross_tab_ranking_the_finest_dimension() -> None:
    """spend_by_service_month declares (provider_name, service_category,
    service_name, charge_month) — coarse to fine. The period trends and the finest
    category ranks, so service_category is not a third axis and "AmazonEC2" is the
    label, not "Compute".

    Both clauses aggregate every row, which is what makes them exact: EC2 appears
    in both months here, so reading a single row would understate it.
    """
    caption = caption_for(
        [
            _step(
                "aws.spend_by_service_month",
                [
                    {
                        "service_category": "Compute",
                        "service_name": "AmazonEC2",
                        "charge_month": "2024-04",
                        "net_cost": 100.0,
                    },
                    {
                        "service_category": "Storage",
                        "service_name": "AmazonS3",
                        "charge_month": "2024-04",
                        "net_cost": 100.0,
                    },
                    {
                        "service_category": "Compute",
                        "service_name": "AmazonEC2",
                        "charge_month": "2024-05",
                        "net_cost": 800.0,
                    },
                ],
            )
        ]
    )
    assert caption is not None
    assert "$1,000 total net cost across 2 service names" in caption
    assert "2024-04 $200 -> 2024-05 $800 (+300%)" in caption
    assert "top: AmazonEC2 $900 (90%)" in caption


def test_a_count_measure_is_not_formatted_as_currency() -> None:
    caption = caption_for(
        [
            _step(
                "efficiency.waste_summary_month",
                [
                    {"charge_month": "2024-04", "entity_count": 12},
                    {"charge_month": "2024-05", "entity_count": 8},
                ],
            )
        ]
    )
    assert caption is not None
    assert "$" not in caption
    assert "20 total entity count across 2 charge months" in caption


def test_declines_a_percentage_measure_because_a_total_would_be_meaningless() -> None:
    """``utilization_pct`` reads like a number to sum and is not one. The old
    name-sniffing version would have summed it happily."""
    assert (
        caption_for(
            [
                _step(
                    "efficiency.utilization_entity_month",
                    [
                        {"entity_id": "job-1", "charge_month": "2024-04", "utilization_pct": 30.0},
                        {"entity_id": "job-2", "charge_month": "2024-04", "utilization_pct": 40.0},
                    ],
                )
            ]
        )
        is None
    )


def test_declines_when_several_measures_leave_the_subject_ambiguous() -> None:
    """Which number is the answer? The chart renderer refuses to plot this for the
    same reason and falls back to a table."""
    assert (
        caption_for(
            [
                _step(
                    "aws.monthly_bill",
                    [
                        {"charge_month": "2024-04", "net_cost": 1.0, "gross_cost": 2.0},
                        {"charge_month": "2024-05", "net_cost": 3.0, "gross_cost": 4.0},
                    ],
                )
            ]
        )
        is None
    )


def test_declines_a_multi_step_comparison() -> None:
    """A cross-provider total is planned as one step per provider (see
    _PLAN_INSTRUCTIONS) precisely because it needs reasoning, not a sum."""
    rows = _two_months()
    assert caption_for([_step("aws.monthly_bill", rows), _step("aws.monthly_bill", rows)]) is None


def test_declines_run_sql_and_unknown_views() -> None:
    """Freeform SQL can alias any expression, so no declaration covers its columns —
    and the caption states figures as fact, so it never guesses about one."""
    rows = _two_months()
    assert caption_for([_step("aws.monthly_bill", rows, name="run_sql")]) is None
    assert caption_for([_step("aws.no_such_view", rows)]) is None


def test_declines_on_error_or_no_rows() -> None:
    assert caption_for([_step("aws.monthly_bill", None, error="boom")]) is None
    assert caption_for([_step("aws.monthly_bill", [])]) is None
    assert caption_for([]) is None


def test_a_single_figure_is_the_answer_when_nothing_varies() -> None:
    """"What did I spend last month?" returns one row with no breakdown. There's
    nothing to rank or trend, but the figure *is* the answer — declining it sent the
    most ordinary question in the product to a synthesis call."""
    caption = caption_for(
        [_step("aws.monthly_bill", [{"charge_month": "2024-04", "net_cost": 12431.0}])]
    )
    # The period the total covers is named, so the figure says what it's for.
    assert caption == "$12,431 net cost in 2024-04"


def test_rows_with_no_value_for_the_measure_are_skipped_not_refused() -> None:
    """``spend_forecast_month`` carries forecast_cost on its forecast rows and NULL
    on the actuals. Summing over the non-null rows is what SQL's SUM does; refusing
    the whole result made every forecast view decline."""
    caption = caption_for(
        [
            _step(
                "aws.spend_forecast_month",
                [
                    {"charge_month": "2024-04", "forecast_kind": "actual", "forecast_cost": None},
                    {"charge_month": "2024-05", "forecast_kind": "trend", "forecast_cost": 400.0},
                    {"charge_month": "2024-06", "forecast_kind": "trend", "forecast_cost": 600.0},
                ],
            )
        ]
    )
    assert caption is not None
    assert "$1,000 total forecast cost" in caption
    # The NULL row is gone, so it neither counts as a period nor drags the total.
    assert "2024-04" not in caption
    assert "2024-05 $400 -> 2024-06 $600" in caption


def test_omits_the_share_when_the_total_is_a_net_credit() -> None:
    """A percentage share of a zero-or-negative total is meaningless, and a real
    goodwill credit produces exactly that."""
    caption = caption_for(
        [
            _step(
                "aws.credits_month",
                [
                    {"charge_description": "Refund", "net_cost": -46000.0},
                    {"charge_description": "Usage", "net_cost": 1000.0},
                ],
            )
        ]
    )
    assert caption is not None
    assert "top:" not in caption
    assert "%" not in caption
    assert "-$45,000" in caption


def _monthly_facts() -> dict[str, str]:
    facts = facts_for(
        [
            _step(
                "aws.monthly_bill",
                [
                    {"charge_month": "2024-04", "net_cost": 100.0},
                    {"charge_month": "2024-05", "net_cost": 300.0},
                ],
            )
        ]
    )
    assert facts is not None
    return facts


def test_a_declared_sentence_is_filled_from_the_rows() -> None:
    rendered = render_sentence(
        "Spend went {first_value} -> {last_value} ({change_pct}).", _monthly_facts()
    )
    assert rendered == "Spend went $100 -> $300 (+200%)."


@pytest.mark.parametrize(
    ("sentence", "why"),
    [
        ("Spend rose to $18,940 last month.", "a currency figure it cannot have read"),
        ("Spend rose 58% over the period.", "a percentage it cannot have computed"),
        ("Spend reached 18940 dollars.", "a bare multi-digit figure"),
        ("Spend rose sharply.", "no placeholders at all — prose written before the data"),
        ("Spend was {nonexistent}.", "a placeholder nobody computed"),
        ("Spend was {total.__class__}.", "an attribute lookup, not a fact name"),
        ("Spend was {total", "malformed braces"),
    ],
)
def test_a_sentence_that_would_state_an_unverifiable_figure_is_refused(
    sentence: str, why: str
) -> None:
    """The sentence is authored at *plan* time, before any row is read, so a literal
    figure in it is fabricated by construction. Refusing costs the wording and falls
    back to the fixed assembly; accepting would cost the guarantee that every number
    in an answer came from the data."""
    assert render_sentence(sentence, _monthly_facts()) is None, why


def test_small_numbers_in_prose_are_still_allowed() -> None:
    """"the top 5" and "over 3 months" are legitimate wording, not claims about
    figures — the guard must not be so blunt that no sentence survives it."""
    rendered = render_sentence(
        "Across 6 months the top 3 services drove {total}.", _monthly_facts()
    )
    assert rendered == "Across 6 months the top 3 services drove $400."


def test_a_null_dimension_value_is_named_not_rendered_as_the_word_none() -> None:
    """Untagged spend is a real group and CLAUDE.md says it's surfaced, never
    dropped — but "top: None" reads as a bug rather than as "not set"."""
    caption = caption_for(
        [
            _step(
                "aws.spend_by_tag_month",
                [
                    {"tag_key_normalized": "team", "tag_value": None, "net_cost": 900.0},
                    {"tag_key_normalized": "team", "tag_value": "data", "net_cost": 100.0},
                ],
            )
        ]
    )
    assert caption is not None
    assert "top: (not set) $900 (90%)" in caption
    assert "None" not in caption


def test_a_period_lookalike_dimension_is_not_trended() -> None:
    """``first_seen_month`` is when an entity appeared, not the charge period —
    trending along it answers a different question than the one asked."""
    caption = caption_for(
        [
            _step(
                "efficiency.waste_resolution_month",
                [
                    {"first_seen_month": "2024-04", "recoverable_cost_at_last_seen": 100.0},
                    {"first_seen_month": "2024-05", "recoverable_cost_at_last_seen": 900.0},
                ],
            )
        ]
    )
    assert caption is not None
    # Ranked as a category, never rendered as a trend.
    assert "->" not in caption
    assert "top: 2024-05 $900 (90%)" in caption
