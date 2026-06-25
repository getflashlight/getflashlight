"""Catalog of GOLD metric views — the public, consumable surface.

GOLD is split into per-provider **groups**: each distinct ``provider_name`` present
in the data gets its own group (``aws``, ``databricks``, ``microsoft``, …), and the
cross-provider TCO metrics live in the fixed ``shared`` group. A group is both a
directory under ``gold/`` and a DuckDB schema, so a view's fully-qualified name is
``<group>.<view>`` (e.g. ``aws.monthly_bill``, ``shared.tco_summary_month``).

The set of provider groups is **data-driven** — it is whatever was published, not a
hard-coded list — so :func:`current_catalog` reads the published groups off disk and
expands the static base specs over them. The MCP server and the dashboard read this
so there is one description of what's queryable.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

from auralake.focus.enums import CostMetric
from auralake.lake import paths

# The fixed group holding cross-provider TCO metrics (Databricks DBU + the cloud
# compute it provisions) — the one place GOLD is intentionally not per-provider.
SHARED_GROUP = "shared"


@dataclass(frozen=True)
class ViewSpec:
    """A provider-agnostic metric definition, expanded into one GoldView per group."""

    view: str  # short name, e.g. "monthly_bill"
    title: str
    description: str
    cost_metric: CostMetric
    dimensions: tuple[str, ...]
    measures: tuple[str, ...]


@dataclass(frozen=True)
class GoldView:
    group: str  # provider group or SHARED_GROUP (== dir under gold/ and DuckDB schema)
    view: str  # short view name
    title: str
    description: str
    cost_metric: CostMetric
    dimensions: tuple[str, ...]
    measures: tuple[str, ...]

    @property
    def name(self) -> str:
        """Fully-qualified, queryable name: ``<group>.<view>``."""
        return f"{self.group}.{self.view}"

    @property
    def relpath(self) -> str:
        """Path of this view's Parquet relative to ``gold/``: ``<group>/<view>.parquet``."""
        return f"{self.group}/{self.view}.parquet"


# ── Base metric specs ────────────────────────────────────────────────────────
# Provider-scoped: materialized once per provider group (sliced by provider_name).
PROVIDER_BASE_VIEWS: tuple[ViewSpec, ...] = (
    ViewSpec(
        view="monthly_bill",
        title="Monthly bill",
        description="Headline spend per month: net/gross/credit, list cost, and savings "
        "(list − effective). Answers 'what is my monthly bill?'.",
        cost_metric=CostMetric.EFFECTIVE_COST,
        dimensions=("provider_name", "charge_month"),
        measures=("net_cost", "gross_cost", "credit_cost", "list_cost", "savings"),
    ),
    ViewSpec(
        view="spend_by_service_month",
        title="Spend by service / month",
        description="Net and gross spend per service category and service, by month.",
        cost_metric=CostMetric.EFFECTIVE_COST,
        dimensions=("provider_name", "service_category", "service_name", "charge_month"),
        measures=("net_cost", "gross_cost", "credit_cost"),
    ),
    ViewSpec(
        view="spend_by_sku_month",
        title="Spend by SKU / month",
        description="Net/gross spend and consumed quantity (e.g. DBUs) per SKU, by month.",
        cost_metric=CostMetric.EFFECTIVE_COST,
        dimensions=("provider_name", "service_name", "sku_id", "charge_month"),
        measures=("net_cost", "gross_cost", "consumed_quantity"),
    ),
    ViewSpec(
        view="spend_by_workspace_month",
        title="Spend by workspace / month",
        description="Net/gross spend per workspace (sub-account), by month.",
        cost_metric=CostMetric.EFFECTIVE_COST,
        dimensions=("provider_name", "sub_account_id", "charge_month"),
        measures=("net_cost", "gross_cost"),
    ),
    ViewSpec(
        view="spend_by_tag_month",
        title="Spend by tag / month",
        description="Net spend per cost-allocation tag (key/value), by month. Untagged spend "
        "is absent here; see the service/SKU views for totals.",
        cost_metric=CostMetric.EFFECTIVE_COST,
        dimensions=("tag_key", "tag_value", "provider_name", "charge_month"),
        measures=("net_cost",),
    ),
    ViewSpec(
        view="sku_month_over_month",
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
    ViewSpec(
        view="savings_summary_month",
        title="Savings summary",
        description="List vs effective cost and realized discount % per month. "
        "effective_is_list flags months priced at list (no negotiated rates).",
        cost_metric=CostMetric.EFFECTIVE_COST,
        dimensions=("provider_name", "charge_month"),
        measures=("list_cost", "effective_cost", "savings", "savings_pct"),
    ),
    ViewSpec(
        view="spend_trend_daily",
        title="Daily spend trend",
        description="Daily net/gross spend; use is_partial_period to dim today.",
        cost_metric=CostMetric.EFFECTIVE_COST,
        dimensions=("charge_day", "provider_name"),
        measures=("net_cost", "gross_cost"),
    ),
)

# Shared/TCO: cross-provider, materialized once into the `shared` group.
SHARED_BASE_VIEWS: tuple[ViewSpec, ...] = (
    ViewSpec(
        view="tco_by_cluster_month",
        title="TCO by Databricks cluster / month",
        description="Per-cluster DBU + attributed AWS infra; tco_basis flags double-count rule.",
        cost_metric=CostMetric.EFFECTIVE_COST,
        dimensions=("charge_month", "sub_account_id", "cluster_id", "compute_class", "tco_basis"),
        measures=("dbu_cost", "infra_cost", "tco_cost", "infra_pct_of_tco"),
    ),
    ViewSpec(
        view="tco_eks_by_cluster_month",
        title="TCO by EKS cluster / month",
        description="Per-EKS-cluster control-plane + AWS-attributed node (EC2/EBS) cost. Node "
        "spend is keyed on AWS-generated tags (aws:eks:cluster-name / kubernetes.io/cluster). "
        "nodes_attributed=false with control-plane cost present flags clusters whose node tags "
        "were not activated as cost-allocation tags upstream.",
        cost_metric=CostMetric.EFFECTIVE_COST,
        dimensions=("charge_month", "cluster_name", "nodes_attributed"),
        measures=("control_plane_cost", "node_ec2_cost", "node_ebs_cost", "node_cost", "eks_tco"),
    ),
    ViewSpec(
        view="tco_summary_month",
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


def provider_group(provider_name: str) -> str:
    """Slug a ``provider_name`` into a filesystem/DuckDB-safe group id.

    ``"AWS"`` → ``"aws"``, ``"Databricks"`` → ``"databricks"``,
    ``"Google Cloud"`` → ``"google_cloud"``.
    """
    return re.sub(r"[^a-z0-9]+", "_", provider_name.lower()).strip("_")


def _view(group: str, spec: ViewSpec) -> GoldView:
    return GoldView(
        group=group,
        view=spec.view,
        title=spec.title,
        description=spec.description,
        cost_metric=spec.cost_metric,
        dimensions=spec.dimensions,
        measures=spec.measures,
    )


def build_catalog(provider_groups: Iterable[str]) -> tuple[GoldView, ...]:
    """Expand the base specs over the given provider groups + the shared TCO group."""
    views: list[GoldView] = []
    for group in provider_groups:
        views.extend(_view(group, spec) for spec in PROVIDER_BASE_VIEWS)
    views.extend(_view(SHARED_GROUP, spec) for spec in SHARED_BASE_VIEWS)
    return tuple(views)


def catalog_by_name(catalog: Iterable[GoldView]) -> dict[str, GoldView]:
    return {v.name: v for v in catalog}


def discover_provider_groups() -> list[str]:
    """The provider groups actually published under ``gold/`` (excludes ``shared``)."""
    gold = paths.gold_dir()
    if not gold.exists():
        return []
    return sorted(
        p.name for p in gold.iterdir() if p.is_dir() and p.name != SHARED_GROUP
    )


def current_catalog() -> tuple[GoldView, ...]:
    """The catalog reflecting what's currently published on disk."""
    return build_catalog(discover_provider_groups())


def current_catalog_by_name() -> dict[str, GoldView]:
    return catalog_by_name(current_catalog())
