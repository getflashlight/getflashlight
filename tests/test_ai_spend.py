"""`gold.ai_spend_month` — the AI slice of the FOCUS bill.

Read back through ``gold.reader.query_view`` rather than raw DuckDB, so a column missing
from the ViewSpec's dimensions/measures tuples fails here instead of being silently
invisible to MCP and the assistant.
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

_WINDOW = IngestWindow(date(2026, 5, 1), date(2026, 5, 31))


@pytest.fixture
def lake_home(tmp_path: object, monkeypatch: pytest.MonkeyPatch) -> Iterator[object]:
    monkeypatch.setenv("FLASHLIGHT_HOME", str(tmp_path))
    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


def _row(
    *,
    service_name: str,
    cost: str,
    service_category: ServiceCategory = ServiceCategory.AI_AND_MACHINE_LEARNING,
    resource_id: str | None = None,
    resource_name: str | None = None,
    resource_type: str | None = None,
    sku_id: str | None = "PREMIUM_SERVERLESS_REAL_TIME_INFERENCE",
    consumed_quantity: float | None = 10.0,
    consumed_unit: str | None = "DBU",
    tags: dict[str, str] | None = None,
    charge_category: ChargeCategory = ChargeCategory.USAGE,
) -> FocusRecord:
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
        charge_category=charge_category,
        service_category=service_category,
        service_name=service_name,
        resource_id=resource_id,
        resource_name=resource_name,
        resource_type=resource_type,
        sku_id=sku_id,
        consumed_quantity=consumed_quantity,
        consumed_unit=consumed_unit,
        tags=tags or {},
        x_source_connector="t",
    )


def _build(rows: list[FocusRecord]) -> list[dict[str, object]]:
    from flashlight.gold.reader import query_view
    from flashlight.lake import bronze
    from flashlight.transform.runner import build_gold

    bronze.write_window("t", _WINDOW, rows, ingest_run_id="r1")
    build_gold()
    return list(query_view("databricks.ai_spend_month"))


def test_only_ai_categorized_spend_lands(lake_home: object) -> None:
    """A non-AI row is the control: the view is scoped by service_category, not by name."""
    rows = _build(
        [
            _row(
                service_name="MODEL_SERVING",
                cost="120",
                resource_id="ep-1",
                resource_name="chat-endpoint",
                resource_type="Model Serving Endpoint",
            ),
            # Analytics — must not appear even though Databricks SQL is a Databricks product.
            _row(
                service_name="SQL",
                cost="900",
                service_category=ServiceCategory.ANALYTICS,
            ),
        ]
    )
    assert [r["service_name"] for r in rows] == ["MODEL_SERVING"]
    assert rows[0]["net_cost"] == pytest.approx(120.0)
    assert rows[0]["resource_id"] == "ep-1"
    assert rows[0]["resource_name"] == "chat-endpoint"


def test_explicit_ai_products_are_included_but_monitoring_and_predictive_optimization_are_not(
    lake_home: object,
) -> None:
    """AI/BI Genie bills as warehouse-shaped usage, so the vendored FOCUS query files it
    under a NON-AI service_category — its eight-product AI list doesn't include it. Anyone
    asking "what is AI costing me?" means Genie, AI/BI Dashboard, and Model Training too, so
    the view unions those in by service_name. Monitoring/data-quality and Predictive
    Optimization are not AI products a user runs, so they — and a plain SQL warehouse — must
    stay out.
    """
    rows = _build(
        [
            # Genie: categorized as Databases by the vendored query, not AI.
            _row(
                service_name="AI_BI_GENIE",
                cost="90",
                service_category=ServiceCategory.DATABASES,
                resource_id="genie-space-1",
            ),
            _row(
                service_name="GENIE",
                cost="30",
                service_category=ServiceCategory.DATABASES,
                resource_id="genie-space-2",
            ),
            _row(
                service_name="AI_BI_DASHBOARD",
                cost="40",
                service_category=ServiceCategory.DATABASES,
                resource_id="dashboard-1",
            ),
            _row(
                service_name="AI_BI_DASHBOARDS",
                cost="50",
                service_category=ServiceCategory.DATABASES,
                resource_id="dashboard-2",
            ),
            _row(
                service_name="MODEL_TRAINING",
                cost="60",
                service_category=ServiceCategory.DATABASES,
                resource_id="training-1",
            ),
            _row(
                service_name="LAKEHOUSE_MONITORING",
                cost="70",
                resource_id="monitor-1",
            ),
            _row(
                service_name="DATA_QUALITY_MONITORING",
                cost="80",
                resource_id="quality-1",
            ),
            _row(
                service_name="PREDICTIVE_OPTIMIZATION",
                cost="20",
                resource_id="po-1",
            ),
            # The control: an ordinary SQL warehouse is not AI spend.
            _row(
                service_name="SQL",
                cost="5000",
                service_category=ServiceCategory.DATABASES,
                resource_id="wh-1",
            ),
        ]
    )
    by_service = {r["service_name"]: r for r in rows}
    assert {
        service: row["ai_product_family"] for service, row in by_service.items()
    } == {
        "AI_BI_GENIE": "genie",
        "GENIE": "genie",
        "AI_BI_DASHBOARD": "ai_bi_dashboard",
        "AI_BI_DASHBOARDS": "ai_bi_dashboard",
        "MODEL_TRAINING": "foundation_model_training",
    }
    assert {
        service: row["net_cost"] for service, row in by_service.items()
    } == {
        "AI_BI_GENIE": pytest.approx(90.0),
        "GENIE": pytest.approx(30.0),
        "AI_BI_DASHBOARD": pytest.approx(40.0),
        "AI_BI_DASHBOARDS": pytest.approx(50.0),
        "MODEL_TRAINING": pytest.approx(60.0),
    }
    assert {
        "PREDICTIVE_OPTIMIZATION",
        "LAKEHOUSE_MONITORING",
        "DATA_QUALITY_MONITORING",
        "SQL",
    }.isdisjoint(
        by_service
    )


def test_ai_product_family_maps_each_product_and_leaves_unknown_null(lake_home: object) -> None:
    """Unmapped products stay NULL — "not applicable", never forced into a bucket."""
    rows = _build(
        [
            _row(service_name="MODEL_SERVING", cost="10", resource_id="ep-1"),
            _row(service_name="VECTOR_SEARCH", cost="20", resource_id="vs-1"),
            _row(service_name="AI_GATEWAY", cost="30", resource_id="gw-1"),
            _row(service_name="AI_FUNCTIONS", cost="40", resource_id="fn-1"),
            _row(service_name="FOUNDATION_MODEL_TRAINING", cost="50", resource_id="run-1"),
            _row(service_name="AGENT_BRICKS", cost="60", resource_id="ab-1"),
            _row(service_name="AI_RUNTIME", cost="70", resource_id="rt-1"),
            _row(service_name="AGENT_EVALUATION", cost="80", resource_id="ae-1"),
            # A product Databricks has not shipped yet / we have not mapped.
            _row(service_name="SOME_FUTURE_AI_PRODUCT", cost="90", resource_id="x-1"),
        ]
    )
    by_service = {r["service_name"]: r["ai_product_family"] for r in rows}
    assert by_service == {
        "MODEL_SERVING": "model_serving",
        "VECTOR_SEARCH": "vector_search",
        "AI_GATEWAY": "ai_gateway",
        "AI_FUNCTIONS": "ai_functions",
        "FOUNDATION_MODEL_TRAINING": "foundation_model_training",
        "AGENT_BRICKS": "agent_bricks",
        "AI_RUNTIME": "ai_runtime",
        "AGENT_EVALUATION": "agent_evaluation",
        "SOME_FUTURE_AI_PRODUCT": None,
    }


def test_project_tag_case_folds_and_stays_null_when_absent(lake_home: object) -> None:
    """Case is folded (the 036_gold_tag_keys fold); a *different* key is not a variant.

    `cost-project` folds to `cost_project`, which is not `project` — the fold is
    case/separator only, never a substring match. Asserted explicitly because the
    tempting-but-wrong version of this lookup would borrow that key's value and report a
    project the endpoint was never tagged with.
    """
    rows = _build(
        [
            _row(service_name="MODEL_SERVING", cost="10", resource_id="a", tags={"project": "rag"}),
            _row(service_name="MODEL_SERVING", cost="20", resource_id="b", tags={"Project": "cha"}),
            _row(service_name="MODEL_SERVING", cost="25", resource_id="f", tags={"PROJECT": "sql"}),
            # A different key that merely contains "project" — must NOT resolve.
            _row(
                service_name="MODEL_SERVING",
                cost="30",
                resource_id="c",
                tags={"cost-project": "search"},
            ),
            # Tagged, but not with a project key — must not borrow another key's value.
            _row(service_name="MODEL_SERVING", cost="40", resource_id="d", tags={"team": "ml"}),
            # Untagged entirely.
            _row(service_name="MODEL_SERVING", cost="50", resource_id="e"),
        ]
    )
    by_resource = {r["resource_id"]: r["project_tag"] for r in rows}
    assert by_resource == {
        "a": "rag",
        "b": "cha",
        "f": "sql",
        "c": None,
        "d": None,
        "e": None,
    }
    # Raw tags survive aggregation so the dashboard can attribute by any key.
    by_tags = {r["resource_id"]: r["tags"] for r in rows}
    assert by_tags["a"] == {"project": "rag"} or '"project"' in str(by_tags["a"])
    assert by_tags["d"] == {"team": "ml"} or '"team"' in str(by_tags["d"])
    assert by_tags["e"] in (None, {}, "{}") or by_tags["e"] == {}


def test_gross_cost_excludes_credits_while_net_includes_them(lake_home: object) -> None:
    """The same charges-only/net split the rest of GOLD carries — a credit nets in net_cost."""
    rows = _build(
        [
            _row(service_name="MODEL_SERVING", cost="100", resource_id="ep-1"),
            _row(
                service_name="MODEL_SERVING",
                cost="-30",
                resource_id="ep-1",
                charge_category=ChargeCategory.CREDIT,
            ),
        ]
    )
    assert len(rows) == 1
    assert rows[0]["net_cost"] == pytest.approx(70.0)
    assert rows[0]["gross_cost"] == pytest.approx(100.0)


def test_consumed_unit_is_a_real_dimension(lake_home: object) -> None:
    """Two units on one endpoint stay two rows — the unit is what makes the quantity mean
    anything, so it is grouped on rather than collapsed by max()."""
    rows = _build(
        [
            _row(
                service_name="MODEL_SERVING",
                cost="10",
                resource_id="ep-1",
                consumed_quantity=5.0,
                consumed_unit="DBU",
                sku_id="SERVERLESS_INFERENCE",
            ),
            _row(
                service_name="MODEL_SERVING",
                cost="20",
                resource_id="ep-1",
                consumed_quantity=1_000_000.0,
                consumed_unit="TOKEN",
                sku_id="FOUNDATION_MODEL_PAY_PER_TOKEN",
            ),
        ]
    )
    by_unit = {r["consumed_unit"]: r["consumed_quantity"] for r in rows}
    assert by_unit == {"DBU": pytest.approx(5.0), "TOKEN": pytest.approx(1_000_000.0)}


def test_no_ai_spend_publishes_an_empty_view_not_a_missing_one(lake_home: object) -> None:
    """A lake with no AI rows must still publish the view — an empty table, not a crash."""
    rows = _build([_row(service_name="SQL", cost="10", service_category=ServiceCategory.ANALYTICS)])
    assert rows == []
