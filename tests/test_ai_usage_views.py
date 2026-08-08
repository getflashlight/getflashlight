"""The `ai_usage` GOLD group — tokens beside cost, and the cost-allocation honesty rules.

The central assertions here are the *negative* ones: a provisioned endpoint's cost is never
split by token share, an external model never gets a $/token, and an endpoint with no
telemetry never disappears. Those are the claims the whole feature turns on, so each gets a
test that fails loudly if someone "fixes" a NULL into a zero.

Read back through ``gold.reader.query_view`` rather than raw DuckDB, so a column missing from
a ViewSpec's dimensions/measures tuples fails here instead of being silently invisible to
MCP and the assistant.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from flashlight.core.settings import get_settings
from flashlight.focus.enums import ChargeCategory, ProviderName, ServiceCategory
from flashlight.focus.model import FocusRecord
from flashlight.ingest.base import IngestWindow
from flashlight.lake.ai_usage_schema import AiUsageRecord

_WINDOW = IngestWindow(date(2026, 5, 1), date(2026, 5, 31))
_MONTH = date(2026, 5, 1)


@pytest.fixture
def lake_home(tmp_path: object, monkeypatch: pytest.MonkeyPatch) -> Iterator[object]:
    monkeypatch.setenv("FLASHLIGHT_HOME", str(tmp_path))
    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


def _cost(
    endpoint_id: str,
    cost: str,
    *,
    sku_id: str = "PREMIUM_MODEL_SERVING",
    tags: dict[str, str] | None = None,
) -> FocusRecord:
    """A Databricks Model Serving cost row — resource_id IS the endpoint id."""
    return FocusRecord(
        provider_name=ProviderName.DATABRICKS,
        billing_account_id="acct",
        billing_period_start=date(2026, 5, 1),
        billing_period_end=date(2026, 5, 31),
        charge_period_start=datetime(2026, 5, 15, tzinfo=UTC),
        charge_period_end=datetime(2026, 5, 15, 1, tzinfo=UTC),
        billed_cost=Decimal(cost),
        effective_cost=Decimal(cost),
        list_cost=Decimal(cost),
        charge_category=ChargeCategory.USAGE,
        service_category=ServiceCategory.AI_AND_MACHINE_LEARNING,
        service_name="MODEL_SERVING",
        resource_id=endpoint_id,
        resource_name=endpoint_id,
        resource_type="Model Serving Endpoint",
        sku_id=sku_id,
        consumed_quantity=10.0,
        consumed_unit="DBU",
        tags=tags or {},
        x_source_connector="t",
    )


def _usage(
    endpoint_id: str,
    *,
    serving_mode: str = "pay_per_token",
    requester: str | None = "alice@example.com",
    project: str | None = None,
    model_name: str | None = "llama-3-70b",
    model_kind: str | None = "FOUNDATION_MODEL",
    input_tokens: int = 0,
    output_tokens: int = 0,
    requests: int = 1,
    error_requests: int = 0,
    error_input_tokens: int = 0,
    error_output_tokens: int = 0,
    scale_to_zero: bool | None = None,
    workload_type: str | None = None,
) -> AiUsageRecord:
    return AiUsageRecord(
        provider_name="Databricks",
        charge_month=_MONTH,
        endpoint_id=endpoint_id,
        endpoint_name=endpoint_id,
        served_entity_id=f"{endpoint_id}-se",
        model_name=model_name,
        model_version="1",
        model_kind=model_kind,
        serving_mode=serving_mode,
        requester=requester,
        usage_context_project=project,
        scale_to_zero_enabled=scale_to_zero,
        workload_type=workload_type,
        request_count=requests,
        error_request_count=error_requests,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        error_input_tokens=error_input_tokens,
        error_output_tokens=error_output_tokens,
        x_source_connector="databricks",
    )


def _build(
    usage: list[AiUsageRecord], cost_rows: list[FocusRecord]
) -> dict[str, list[dict[str, object]]]:
    from flashlight.gold.reader import query_view
    from flashlight.lake import bronze
    from flashlight.lake.ai_usage import write_ai_usage
    from flashlight.transform.runner import build_gold

    bronze.write_window("t", _WINDOW, cost_rows, ingest_run_id="r1")
    write_ai_usage(_WINDOW, usage)
    build_gold()
    return {
        v: list(query_view(f"ai_usage.{v}"))
        for v in ("endpoint_month", "model_month", "project_month", "requester_month")
    }


# ── Cost allocation: the honesty core ────────────────────────────────────────
def test_pay_per_token_cost_splits_by_token_share(lake_home: object) -> None:
    """Tokens ARE the meter here, so a token-share split of the charge is a proportional
    split of a per-token charge. $300 over 3M tokens split 2M/1M → $200/$100."""
    views = _build(
        [
            _usage("ep-ppt", requester="alice@example.com", input_tokens=1_500_000,
                   output_tokens=500_000),
            _usage("ep-ppt", requester="bob@example.com", input_tokens=750_000,
                   output_tokens=250_000),
        ],
        [_cost("ep-ppt", "300")],
    )
    by_requester = {r["requester_key"]: r for r in views["requester_month"]}
    assert by_requester["alice@example.com"]["allocated_cost"] == pytest.approx(200.0)
    assert by_requester["bob@example.com"]["allocated_cost"] == pytest.approx(100.0)
    assert {r["cost_allocation_basis"] for r in views["requester_month"]} == {"measured_tokens"}

    endpoint = views["endpoint_month"][0]
    assert endpoint["net_cost"] == pytest.approx(300.0)
    assert endpoint["total_tokens"] == 3_000_000
    # $300 / 3M tokens = $100 per 1M.
    assert endpoint["cost_per_million_tokens"] == pytest.approx(100.0)
    assert endpoint["unallocated_cost"] is None


def test_provisioned_endpoint_cost_is_never_split_by_tokens(lake_home: object) -> None:
    """THE central assertion. A provisioned endpoint bills by the hour, so splitting its
    charge by token share would hand its idle capacity's cost to whoever sent traffic.
    allocated_cost must be NULL — not 0, not the token share."""
    views = _build(
        [_usage("ep-prov", serving_mode="provisioned_throughput", input_tokens=1_000_000)],
        [_cost("ep-prov", "500")],
    )
    requester = views["requester_month"][0]
    assert requester["allocated_cost"] is None, "provisioned cost must not be token-allocated"
    assert requester["cost_allocation_basis"] == "unallocated"
    # Tokens are still fully reported — only the dollars are withheld.
    assert requester["total_tokens"] == 1_000_000

    endpoint = views["endpoint_month"][0]
    assert endpoint["net_cost"] == pytest.approx(500.0)
    assert endpoint["unallocated_cost"] == pytest.approx(500.0), "the whole charge is named"
    assert endpoint["cost_per_million_tokens"] is None


def test_external_model_reports_tokens_but_never_a_rate(lake_home: object) -> None:
    """Databricks bills the gateway hop; the vendor bills the tokens on a bill we never see.
    A $/token from the Databricks charge would be meaningless."""
    views = _build(
        [
            _usage(
                "ep-ext",
                serving_mode="external",
                model_kind="EXTERNAL_MODEL",
                input_tokens=2_000_000,
            )
        ],
        [_cost("ep-ext", "12")],
    )
    endpoint = views["endpoint_month"][0]
    assert endpoint["cost_allocation_basis"] == "external_passthrough"
    assert endpoint["total_tokens"] == 2_000_000, "tokens are real and reported"
    assert endpoint["cost_per_million_tokens"] is None
    assert views["requester_month"][0]["allocated_cost"] is None


def test_provisioned_throughput_pairs_with_either_billing_shape(lake_home: object) -> None:
    """Provisioned throughput is NOT cross-checked against the SKU shape, because both
    pairings are legitimate: normally a MODEL_SERVING/INFERENCE SKU, but the vendored FOCUS
    query documents it running on a classic cluster and billing as ENTERPRISE_ALL_PURPOSE_-
    COMPUTE. Treating either as a conflict would push ordinary provisioned endpoints to
    'unknown' and hide them from the named unallocated bucket.
    """
    views = _build(
        [
            _usage("ep-serverless", serving_mode="provisioned_throughput", input_tokens=1_000),
            _usage("ep-classic", serving_mode="provisioned_throughput", input_tokens=1_000),
        ],
        [
            _cost("ep-serverless", "100", sku_id="PREMIUM_SERVERLESS_INFERENCE"),
            _cost("ep-classic", "200", sku_id="ENTERPRISE_ALL_PURPOSE_COMPUTE"),
        ],
    )
    bases = {r["endpoint_id"]: r["cost_allocation_basis"] for r in views["endpoint_month"]}
    assert bases == {"ep-serverless": "unallocated", "ep-classic": "unallocated"}


def test_serving_mode_disagreeing_with_the_focus_sku_forfeits_the_rate(lake_home: object) -> None:
    """served_entities says pay-per-token, the bill says an hourly ALL_PURPOSE SKU. That is a
    shape we don't understand, so we lose the $/token claim rather than assert a wrong one."""
    views = _build(
        [_usage("ep-odd", serving_mode="pay_per_token", input_tokens=1_000_000)],
        [_cost("ep-odd", "400", sku_id="ENTERPRISE_ALL_PURPOSE_COMPUTE")],
    )
    endpoint = views["endpoint_month"][0]
    assert endpoint["cost_allocation_basis"] == "unknown"
    assert endpoint["cost_per_million_tokens"] is None
    assert views["requester_month"][0]["allocated_cost"] is None


# ── Coverage: unmeasured must never look like efficient ──────────────────────
def test_endpoint_with_cost_but_no_telemetry_still_appears(lake_home: object) -> None:
    """The FULL OUTER JOIN. An endpoint absent from the serving tables is unmeasured, not
    free and not efficient — dropping it would hide spend entirely."""
    views = _build(
        [_usage("ep-measured", input_tokens=1_000_000)],
        [_cost("ep-measured", "100"), _cost("ep-silent", "900")],
    )
    by_endpoint = {r["endpoint_id"]: r for r in views["endpoint_month"]}
    assert set(by_endpoint) == {"ep-measured", "ep-silent"}
    silent = by_endpoint["ep-silent"]
    assert silent["token_coverage_status"] == "no_token_telemetry"
    assert silent["net_cost"] == pytest.approx(900.0)
    assert silent["total_tokens"] is None, "unmeasured tokens are NULL, never 0"
    assert silent["cost_per_million_tokens"] is None
    assert by_endpoint["ep-measured"]["token_coverage_status"] == "measured"


def test_no_ai_usage_at_all_publishes_empty_views_not_missing_ones(lake_home: object) -> None:
    """A lake whose serving tables were never enabled: the group publishes, empty."""
    views = _build([], [_cost("ep-1", "100")])
    assert views["model_month"] == []
    assert views["project_month"] == []
    assert views["requester_month"] == []
    # The cost side still lands, via the FULL OUTER JOIN.
    assert [r["endpoint_id"] for r in views["endpoint_month"]] == ["ep-1"]
    assert views["endpoint_month"][0]["token_coverage_status"] == "no_token_telemetry"


# ── Attribution keys are never NULL ─────────────────────────────────────────
def test_project_key_is_never_null_and_prefers_usage_context(lake_home: object) -> None:
    """Unattributed spend is the finding, so it gets a named bucket. Request-level
    usage_context is the finer fact and wins over the endpoint's tag."""
    views = _build(
        [
            # Request-level project set — wins over the endpoint tag below.
            _usage("ep-tagged", project="from-request", input_tokens=1_000),
            # No request-level project → falls back to the endpoint's `project` tag.
            _usage("ep-tagged", requester="bob@example.com", input_tokens=2_000),
            # Neither → (unattributed).
            _usage("ep-bare", requester="carol@example.com", input_tokens=3_000),
        ],
        [_cost("ep-tagged", "10", tags={"project": "from-tag"}), _cost("ep-bare", "10")],
    )
    by_project = {r["project_key"]: r for r in views["project_month"]}
    assert set(by_project) == {"from-request", "from-tag", "(unattributed)"}
    assert by_project["from-request"]["project_source"] == "usage_context"
    assert by_project["from-tag"]["project_source"] == "endpoint_tag"
    assert by_project["(unattributed)"]["project_source"] == "none"
    assert None not in by_project


def test_requester_kind_separates_service_principals_from_people(lake_home: object) -> None:
    """A bare-UUID identity is an application, not a person — the same fold the owner
    leaderboard applies, so the two rank identities the same way."""
    views = _build(
        [
            _usage("ep-1", requester="Alice@Example.com", input_tokens=1_000),
            _usage("ep-1", requester="a1b2c3d4-1111-2222-3333-444455556666", input_tokens=2_000),
            _usage("ep-1", requester=None, input_tokens=3_000),
        ],
        [_cost("ep-1", "60")],
    )
    by_kind = {r["requester_kind"]: r for r in views["requester_month"]}
    assert set(by_kind) == {"user", "service_principal", "unattributed"}
    # Case-folded into the key, so one person is one row.
    assert by_kind["user"]["requester_key"] == "alice@example.com"
    assert str(by_kind["service_principal"]["requester_display"]).startswith("Service principal ")
    # The full identity survives in the key so an agent's filter stays exact.
    assert by_kind["service_principal"]["requester_key"] == "a1b2c3d4-1111-2222-3333-444455556666"
    assert by_kind["unattributed"]["requester_display"] == "(no requester recorded)"


# ── Errors and reconciliation ───────────────────────────────────────────────
def test_error_rate_and_error_tokens_are_reported(lake_home: object) -> None:
    """Tokens burned on a failed request are spend with no result."""
    views = _build(
        [
            _usage(
                "ep-1",
                input_tokens=1_000,
                output_tokens=0,
                requests=10,
                error_requests=2,
                error_input_tokens=200,
                error_output_tokens=0,
            )
        ],
        [_cost("ep-1", "100")],
    )
    endpoint = views["endpoint_month"][0]
    assert endpoint["request_count"] == 10
    assert endpoint["error_request_count"] == 2
    assert endpoint["error_rate_pct"] == pytest.approx(20.0)
    assert endpoint["error_tokens"] == 200


def test_endpoint_net_cost_reconciles_to_the_endpoint_shaped_focus_bill(
    lake_home: object,
) -> None:
    """The join must not invent or lose dollars.

    Reconciled against the *endpoint-shaped* slice of ai_spend_month, not all of it: that
    view also carries AI products with no serving endpoint (Genie and friends), which by
    design never enter the token join.
    """
    from flashlight.gold.reader import query_view

    views = _build(
        [_usage("ep-a", input_tokens=1_000), _usage("ep-b", input_tokens=2_000)],
        [_cost("ep-a", "120"), _cost("ep-b", "80")],
    )
    ai_usage_total = sum(float(str(r["net_cost"])) for r in views["endpoint_month"])
    focus_total = sum(
        float(str(r["net_cost"]))
        for r in query_view("databricks.ai_spend_month")
        if r["ai_product_family"] == "model_serving"
    )
    assert ai_usage_total == pytest.approx(focus_total)
    assert ai_usage_total == pytest.approx(200.0)


def test_ai_usage_is_not_a_provider_group(lake_home: object) -> None:
    """The fixed group must never become a nav entry / provider page."""
    from flashlight.transform.catalog import discover_provider_groups

    _build([_usage("ep-1", input_tokens=1)], [_cost("ep-1", "10")])
    assert "ai_usage" not in discover_provider_groups()
    assert "databricks" in discover_provider_groups()
