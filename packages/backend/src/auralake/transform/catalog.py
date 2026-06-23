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
        name="gold.spend_by_service_month",
        title="Spend by service / month",
        description="Net and gross spend per provider, service category, and service, by month.",
        cost_metric=CostMetric.EFFECTIVE_COST,
        dimensions=("provider_name", "service_category", "service_name", "charge_month"),
        measures=("net_cost", "gross_cost", "credit_cost"),
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
