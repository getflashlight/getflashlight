"""`AiUsageRecord` ↔ Arrow round-trip and the partition-replace writer.

The plane-level counterpart to tests/test_ai_usage_views.py (which covers GOLD): this pins
the on-disk contract — every field survives, the month becomes a ``YYYY-MM`` partition, and
re-running a window replaces rather than appends.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date

import pytest

from flashlight.core.settings import get_settings
from flashlight.ingest.base import IngestWindow
from flashlight.lake.ai_usage_schema import (
    AI_USAGE_SCHEMA,
    AiUsageRecord,
    build_table,
    charge_month_of,
    empty_table,
)

_WINDOW = IngestWindow(date(2026, 5, 1), date(2026, 5, 31))


@pytest.fixture
def lake_home(tmp_path: object, monkeypatch: pytest.MonkeyPatch) -> Iterator[object]:
    monkeypatch.setenv("FLASHLIGHT_HOME", str(tmp_path))
    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


def _rec(**kw: object) -> AiUsageRecord:
    base: dict[str, object] = {
        "provider_name": "Databricks",
        "charge_month": date(2026, 5, 1),
        "endpoint_id": "ep-1",
        "x_source_connector": "databricks",
    }
    return AiUsageRecord(**{**base, **kw})


def test_charge_month_is_normalized_to_the_first_of_the_month() -> None:
    """Mid-month dates must not fan one endpoint's month into several partitions."""
    assert _rec(charge_month=date(2026, 5, 17)).charge_month == date(2026, 5, 1)
    assert charge_month_of(_rec(charge_month=date(2026, 5, 17))) == "2026-05"


def test_unknown_serving_mode_is_normalized_rather_than_passed_through() -> None:
    """A mode we don't understand must not reach GOLD claiming to be understood."""
    assert _rec(serving_mode="pay_per_token").serving_mode == "pay_per_token"
    assert _rec(serving_mode="something_new").serving_mode == "unknown"
    assert _rec().serving_mode == "unknown"


def test_build_table_round_trips_every_field() -> None:
    rec = _rec(
        endpoint_name="chat",
        served_entity_id="se-1",
        model_name="llama-3-70b",
        model_version="3",
        model_kind="FOUNDATION_MODEL",
        serving_mode="pay_per_token",
        requester="alice@example.com",
        usage_context_project="rag",
        scale_to_zero_enabled=True,
        workload_size="Small",
        workload_type="GPU_MEDIUM",
        min_provisioned_throughput=100.0,
        max_provisioned_throughput=400.0,
        request_count=12,
        error_request_count=2,
        input_tokens=1_000,
        output_tokens=500,
        error_input_tokens=40,
        error_output_tokens=10,
        total_duration_ms=9_876,
    )
    table = build_table([rec])
    assert table.schema.names == AI_USAGE_SCHEMA.names
    row = table.to_pylist()[0]
    assert row["endpoint_id"] == "ep-1"
    assert row["model_name"] == "llama-3-70b"
    assert row["serving_mode"] == "pay_per_token"
    assert row["requester"] == "alice@example.com"
    assert row["usage_context_project"] == "rag"
    assert row["scale_to_zero_enabled"] is True
    assert row["workload_type"] == "GPU_MEDIUM"
    assert row["min_provisioned_throughput"] == 100.0
    assert row["input_tokens"] == 1_000
    assert row["error_output_tokens"] == 10
    assert row["total_duration_ms"] == 9_876
    # Partition keys last, and the month as a YYYY-MM string.
    assert row["provider_name"] == "Databricks"
    assert row["charge_month"] == "2026-05"


def test_empty_table_is_typed_like_a_full_one() -> None:
    """The no-data fallback duck.register_ai_usage installs before the first pull — SQL
    against it must resolve, so the column set has to match exactly."""
    assert empty_table().schema.names == build_table([]).schema.names
    assert empty_table().num_rows == 0


def test_write_is_partition_replace_not_append(lake_home: object) -> None:
    """Re-running a window is idempotent and self-purging — the plane-wide invariant."""
    from flashlight.lake import duck
    from flashlight.lake.ai_usage import write_ai_usage

    assert write_ai_usage(_WINDOW, [_rec(input_tokens=100)]) == 1
    # A second run for the same window with different data replaces it.
    assert write_ai_usage(_WINDOW, [_rec(input_tokens=999)]) == 1

    con = duck.connect()
    try:
        duck.register_ai_usage(con)
        rows = con.execute("SELECT input_tokens FROM metrics.ai_usage").fetchall()
    finally:
        con.close()
    assert rows == [(999,)], "the window was replaced, not appended to"


def test_an_empty_pull_leaves_existing_rows_alone(lake_home: object) -> None:
    """An empty write is a no-op, NOT a purge — and here that is the safe direction.

    The purge is keyed on the provider_names present in the batch, so an empty batch names
    no provider to purge (identical in lake/metrics.py and lake/driver_health.py — this
    plane deliberately doesn't diverge). For AI usage that behaviour is what we want:
    ``fetch_ai_usage`` yields nothing whenever ``system.serving`` isn't enabled or its probe
    fails, so purging on empty would erase months of token history the first time a
    permission lapsed. Purging still happens normally on any window a real pull covers.
    """
    from flashlight.lake import duck
    from flashlight.lake.ai_usage import write_ai_usage

    write_ai_usage(_WINDOW, [_rec(input_tokens=100)])
    assert write_ai_usage(_WINDOW, []) == 0

    con = duck.connect()
    try:
        duck.register_ai_usage(con)
        rows = con.execute("SELECT input_tokens FROM metrics.ai_usage").fetchall()
    finally:
        con.close()
    assert rows == [(100,)], "a degraded/absent pull must not erase measured history"


def test_the_view_resolves_before_any_pull(lake_home: object) -> None:
    """duck.register_ai_usage's typed-empty fallback: the GOLD SQL must compile on a lake
    where the serving tables were never enabled."""
    from flashlight.lake import duck

    con = duck.connect()
    try:
        duck.register_ai_usage(con)
        count = con.execute("SELECT count(*) FROM metrics.ai_usage").fetchone()
    finally:
        con.close()
    assert count is not None and count[0] == 0
