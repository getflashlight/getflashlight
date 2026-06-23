"""Catalog of GOLD metric views — the public, consumable surface.

Both the HTTP API and the MCP server read this so there is one description of
what's queryable. Keeping it in code (not just SQL comments) lets agents discover
metrics and lets the API expose a schema without introspecting the database.
"""

from __future__ import annotations

from dataclasses import dataclass

from auralake.focus.enums import CostMetric


@dataclass(frozen=True)
class GoldView:
    name: str  # fully-qualified view name
    title: str
    description: str
    cost_metric: CostMetric
    dimensions: tuple[str, ...]
    measures: tuple[str, ...]


CATALOG: tuple[GoldView, ...] = (
    GoldView(
        name="gold.monthly_bill",
        title="Monthly bill",
        description="Headline spend per provider per month: net/gross/credit, list cost, "
        "and savings (list − effective). Answers 'what is my monthly bill?'.",
        cost_metric=CostMetric.EFFECTIVE_COST,
        dimensions=("provider_name", "charge_month"),
        measures=("net_cost", "gross_cost", "credit_cost", "list_cost", "savings"),
    ),
    GoldView(
        name="gold.spend_by_service_month",
        title="Spend by service / month",
        description="Net and gross spend per provider, service category, and service, by month.",
        cost_metric=CostMetric.EFFECTIVE_COST,
        dimensions=("provider_name", "service_category", "service_name", "charge_month"),
        measures=("net_cost", "gross_cost", "credit_cost"),
    ),
    GoldView(
        name="gold.spend_by_sku_month",
        title="Spend by SKU / month",
        description="Net/gross spend and consumed quantity (e.g. DBUs) per SKU, by month.",
        cost_metric=CostMetric.EFFECTIVE_COST,
        dimensions=("provider_name", "service_name", "sku_id", "charge_month"),
        measures=("net_cost", "gross_cost", "consumed_quantity"),
    ),
    GoldView(
        name="gold.spend_by_workspace_month",
        title="Spend by workspace / month",
        description="Net/gross spend per workspace (sub-account), by month.",
        cost_metric=CostMetric.EFFECTIVE_COST,
        dimensions=("provider_name", "sub_account_id", "charge_month"),
        measures=("net_cost", "gross_cost"),
    ),
    GoldView(
        name="gold.spend_by_tag_month",
        title="Spend by tag / month",
        description="Net spend per cost-allocation tag (key/value), by month. Untagged spend "
        "is absent here; see the service/SKU views for totals.",
        cost_metric=CostMetric.EFFECTIVE_COST,
        dimensions=("tag_key", "tag_value", "provider_name", "charge_month"),
        measures=("net_cost",),
    ),
    GoldView(
        name="gold.sku_month_over_month",
        title="SKU month-over-month variance",
        description="Per-SKU cost change decomposed into volume_effect (Δusage × prior rate) "
        "and rate_effect (price/mix), which sum to cost_delta. Answers whether a SKU's cost "
        "moved because of more usage or a higher per-unit rate.",
        cost_metric=CostMetric.EFFECTIVE_COST,
        dimensions=("provider_name", "sku_id", "charge_month"),
        measures=(
            "net_cost",
            "consumed_quantity",
            "unit_rate",
            "cost_delta",
            "cost_pct_change",
            "volume_effect",
            "rate_effect",
        ),
    ),
    GoldView(
        name="gold.savings_summary_month",
        title="Savings summary",
        description="List vs effective cost and realized discount % per provider per month. "
        "effective_is_list flags months priced at list (no negotiated rates).",
        cost_metric=CostMetric.EFFECTIVE_COST,
        dimensions=("provider_name", "charge_month"),
        measures=("list_cost", "effective_cost", "savings", "savings_pct"),
    ),
    GoldView(
        name="gold.spend_trend_daily",
        title="Daily spend trend",
        description="Daily net/gross spend per provider; use is_partial_period to dim today.",
        cost_metric=CostMetric.EFFECTIVE_COST,
        dimensions=("charge_day", "provider_name"),
        measures=("net_cost", "gross_cost"),
    ),
    GoldView(
        name="gold.tco_by_cluster_month",
        title="TCO by Databricks cluster / month",
        description="Per-cluster DBU + attributed AWS infra; tco_basis flags double-count rule.",
        cost_metric=CostMetric.EFFECTIVE_COST,
        dimensions=("charge_month", "sub_account_id", "cluster_id", "compute_class", "tco_basis"),
        measures=("dbu_cost", "infra_cost", "tco_cost", "infra_pct_of_tco"),
    ),
    GoldView(
        name="gold.tco_summary_month",
        title="Monthly TCO summary",
        description="DBU vs attributed infra vs the unattributed AWS bucket, plus total, by month.",
        cost_metric=CostMetric.EFFECTIVE_COST,
        dimensions=("charge_month",),
        measures=(
            "dbu_cost",
            "attributed_infra_cost",
            "unattributed_infra_cost",
            "total_cost",
        ),
    ),
)

CATALOG_BY_NAME = {v.name: v for v in CATALOG}
