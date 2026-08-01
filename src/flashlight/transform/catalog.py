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

from flashlight.focus.enums import CostMetric
from flashlight.lake import paths

# The fixed group holding cross-provider TCO metrics (Databricks DBU + the cloud
# compute it provisions) — the one place GOLD is intentionally not per-provider.
SHARED_GROUP = "shared"

# The fixed group holding the efficiency/waste views. Like SHARED, not per-provider:
# `waste_record` already carries provider_name as a column, so one group spans every
# platform. Fed by the metrics plane (metrics.efficiency_record), not BRONZE.
EFFICIENCY_GROUP = "efficiency"

# The fixed group holding the client-driver fleet-health view. Like EFFICIENCY, not
# per-provider. Fed by the metrics plane (metrics.driver_health), not BRONZE. No
# cost_metric — this is a compliance/fleet-health signal, not spend or waste.
DRIVER_HEALTH_GROUP = "driver_health"

# The fixed group holding the policy-compliance view. Like EFFICIENCY, not
# per-provider — policy_record already carries provider_name as a column. Fed by the
# same metrics plane (metrics.efficiency_record) as efficiency/waste, classified by a
# separate rule pool (flashlight.efficiency.policy_rules). No cost_metric — a
# governance signal (are auto-terminate/autoscaling/tagging guardrails in place), not
# spend or waste.
POLICY_GROUP = "policy"


@dataclass(frozen=True)
class ViewSpec:
    """A provider-agnostic metric definition, expanded into one GoldView per group."""

    view: str  # short name, e.g. "monthly_bill"
    title: str
    description: str
    cost_metric: CostMetric | None  # None for views with no dollar figure (e.g. driver_health)
    dimensions: tuple[str, ...]
    measures: tuple[str, ...]


@dataclass(frozen=True)
class GoldView:
    group: str  # provider group or SHARED_GROUP (== dir under gold/ and DuckDB schema)
    view: str  # short view name
    title: str
    description: str
    cost_metric: CostMetric | None  # None for views with no dollar figure (e.g. driver_health)
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
        description="Net/gross spend and consumed quantity (e.g. DBUs) per SKU, by month. "
        "compute_family (Databricks only: job/interactive/sql_warehouse/endpoint) is derived "
        "from Databricks' own service_name/billing_origin_product, NULL where not applicable. "
        "sku_description is the pricing text off the SKU's highest-cost line — for providers "
        "whose sku_id is opaque (e.g. AWS), it's the human-readable label.",
        cost_metric=CostMetric.EFFECTIVE_COST,
        dimensions=(
            "provider_name",
            "service_name",
            "compute_family",
            "sku_id",
            "sku_description",
            "charge_month",
        ),
        measures=("net_cost", "gross_cost", "consumed_quantity"),
    ),
    ViewSpec(
        view="spend_by_cost_subcategory_month",
        title="Spend by cost subcategory / month",
        description="Net spend below SKU granularity, where a connector stamps "
        "x_cost_subcategory (currently: Redshift compute/concurrency-scaling/storage/"
        "spectrum-scan/serverless, derived from AWS UsageType). Rows without a "
        "subcategory are absent; reconcile against spend_by_service_month for the total.",
        cost_metric=CostMetric.EFFECTIVE_COST,
        dimensions=("provider_name", "service_name", "cost_subcategory", "charge_month"),
        measures=("net_cost",),
    ),
    ViewSpec(
        view="resource_month",
        title="Spend by resource / month",
        description="Finest consumer grain: net spend and consumed quantity per (SKU, resource, "
        "resource_type, workspace, region), by month — drives the SKU→resource drill-down "
        "(e.g. which SQL warehouse moved). consumed_quantity is the billable usage unit "
        "(e.g. DBUs), not an operation/query count.",
        cost_metric=CostMetric.EFFECTIVE_COST,
        dimensions=(
            "provider_name",
            "service_name",
            "compute_family",
            "sku_id",
            "sku_description",
            "resource_type",
            "resource_id",
            "resource_name",
            "sub_account_id",
            "region_id",
            "charge_month",
        ),
        measures=("net_cost", "consumed_quantity"),
    ),
    ViewSpec(
        view="spend_by_sku_tag_month",
        title="Spend by SKU × tag / month",
        description="Net spend per (SKU, cost-allocation tag key/value), by month — attributes a "
        "SKU's spend to a project/team tag. Untagged spend is absent; reconcile against "
        "spend_by_sku_month for the unattributed remainder.",
        cost_metric=CostMetric.EFFECTIVE_COST,
        dimensions=("provider_name", "sku_id", "tag_key", "tag_value", "charge_month"),
        measures=("net_cost",),
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
    ViewSpec(
        view="commitment_summary_month",
        title="Commitment coverage",
        description="RI/Savings-Plan commitment spend per month, split by type/category/"
        "status. commitment_discount_status='Unused' is the direct wasted-commitment "
        "signal. Empty for providers with no commitment data (e.g. Databricks — no "
        "system table exposes reservation/savings-plan data).",
        cost_metric=CostMetric.EFFECTIVE_COST,
        dimensions=(
            "provider_name",
            "charge_month",
            "commitment_discount_type",
            "commitment_discount_category",
            "commitment_discount_status",
        ),
        measures=("effective_cost", "billed_cost", "commitment_count"),
    ),
    ViewSpec(
        view="invoice_reconciliation_month",
        title="Invoice reconciliation",
        description="Billed spend per (billing account, invoice, month) — verifies GOLD's "
        "total ties to a specific invoice and groups a multi-invoice billing account. "
        "Empty for providers with no invoice data (e.g. Databricks).",
        cost_metric=CostMetric.BILLED_COST,
        dimensions=("provider_name", "billing_account_id", "invoice_id", "charge_month"),
        measures=("billed_cost",),
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


# Efficiency/waste: the standardized waste record + its KPI rollup, materialized once
# into the `efficiency` group (provider_name is a column, not a group).
EFFICIENCY_BASE_VIEWS: tuple[ViewSpec, ...] = (
    ViewSpec(
        view="waste_record",
        title="Waste record",
        description="One row per (entity, month, waste_category): the classified, "
        "standardized waste record across platforms. recoverable_cost is the estimated "
        "recoverable spend; lens splits WASTE (tune it) from OPPORTUNITY (move it). "
        "confidence is high vs candidate. underutilized is never emitted for shared compute.",
        cost_metric=CostMetric.EFFECTIVE_COST,
        dimensions=(
            "provider_name",
            "charge_month",
            "entity_type",
            "entity_id",
            "entity_name",
            "owner_user",
            "owner_project",
            "waste_category",
            "lens",
            "confidence",
        ),
        measures=("billed_cost", "recoverable_cost"),
    ),
    ViewSpec(
        view="waste_summary_month",
        title="Waste summary",
        description="Recoverable spend per month × waste_category × lens × confidence — "
        "drives the KPI bar (total WASTE $ vs OPPORTUNITY $).",
        cost_metric=CostMetric.EFFECTIVE_COST,
        dimensions=("charge_month", "waste_category", "lens", "confidence"),
        measures=("recoverable_cost", "billed_cost", "entity_count"),
    ),
    ViewSpec(
        view="waste_resolution_month",
        title="Waste resolution tracking",
        description="Did a flagged (entity, waste_category) go away, and did cost drop? "
        "Pure re-detection over waste_record history — no user input. is_resolved means "
        "it did not reappear in the most recent month of data; realized_savings compares "
        "billed_cost the month it was last flagged vs. the month after (a terminated "
        "entity with no further data counts as a full recovery).",
        cost_metric=CostMetric.EFFECTIVE_COST,
        dimensions=(
            "provider_name",
            "entity_type",
            "entity_id",
            "entity_name",
            "owner_user",
            "owner_project",
            "waste_category",
            "lens",
            "first_seen_month",
            "last_seen_month",
            "is_resolved",
            "resolved_month",
        ),
        measures=("recoverable_cost_at_last_seen", "billed_cost_at_last_seen",
                  "billed_cost_after", "realized_savings"),
    ),
    ViewSpec(
        view="efficiency_entity_month",
        title="Efficiency entity coverage",
        description="One row per (entity, month) actually measured by an efficiency "
        "connector pull — distinct from waste_record, which only contains rows that "
        "MATCHED a waste rule. Lets a consumer tell 'rule evaluated, found nothing' "
        "apart from 'this entity_type's telemetry never arrived this window'.",
        cost_metric=None,
        dimensions=("provider_name", "x_source_connector", "entity_type", "entity_id",
                    "charge_month"),
        measures=(),
    ),
)


# Driver health: raw (driver, application, user, month) query counts — a fleet-health/
# compliance leaderboard, not waste. Materialized once into the `driver_health` group.
DRIVER_HEALTH_BASE_VIEWS: tuple[ViewSpec, ...] = (
    ViewSpec(
        view="driver_health",
        title="Client driver health",
        description="Query volume per (client_driver, client_application, executed_by, "
        "month) — which JDBC/ODBC driver versions and applications are hitting the "
        "warehouse, and who's running them. No dollar figure and no automated "
        "'stale version' verdict — there's no reference table of current versions in "
        "this data; humans read the leaderboard and judge.",
        cost_metric=None,
        dimensions=("provider_name", "charge_month", "client_driver", "client_application",
                    "executed_by"),
        measures=("query_count",),
    ),
)


# Policy compliance: pass/fail governance findings (auto-terminate, autoscaling,
# cluster-policy assignment, tagging) + their KPI rollup, materialized once into the
# `policy` group (provider_name is a column, not a group). Every ACTIVE rule in
# policy_rules.py emits one row per applicable entity per month regardless of status —
# a real coverage denominator, unlike waste_record's violations-only shape.
POLICY_BASE_VIEWS: tuple[ViewSpec, ...] = (
    ViewSpec(
        view="policy_record",
        title="Policy compliance record",
        description="One row per (entity, month, policy_category): compliant, "
        "non_compliant, or not_applicable (telemetry unmeasured for this entity). "
        "Covers cost-guardrail policies (auto-terminate, autoscaling, cluster-policy "
        "assignment) and attribution tagging (cluster/warehouse-level). No dollar "
        "figure — see efficiency.waste_record for recoverable spend.",
        cost_metric=None,
        dimensions=(
            "provider_name",
            "charge_month",
            "entity_type",
            "entity_id",
            "entity_name",
            "owner_user",
            "owner_project",
            "policy_category",
            "status",
        ),
        measures=(),
    ),
    ViewSpec(
        view="policy_summary_month",
        title="Policy compliance summary",
        description="Entity count per month × policy_category × status — drives the "
        "compliance-rate KPI (e.g. '80% of clusters have auto-terminate set').",
        cost_metric=None,
        dimensions=("charge_month", "policy_category", "status"),
        measures=("entity_count",),
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
    """Expand the base specs over the provider groups + the fixed shared/efficiency/
    driver_health/policy groups."""
    views: list[GoldView] = []
    for group in provider_groups:
        views.extend(_view(group, spec) for spec in PROVIDER_BASE_VIEWS)
    views.extend(_view(SHARED_GROUP, spec) for spec in SHARED_BASE_VIEWS)
    views.extend(_view(EFFICIENCY_GROUP, spec) for spec in EFFICIENCY_BASE_VIEWS)
    views.extend(_view(DRIVER_HEALTH_GROUP, spec) for spec in DRIVER_HEALTH_BASE_VIEWS)
    views.extend(_view(POLICY_GROUP, spec) for spec in POLICY_BASE_VIEWS)
    return tuple(views)


def discover_provider_groups() -> list[str]:
    """Provider groups published under ``gold/`` (excludes shared/efficiency/
    driver_health/policy)."""
    gold = paths.gold_dir()
    if not gold.exists():
        return []
    fixed = {SHARED_GROUP, EFFICIENCY_GROUP, DRIVER_HEALTH_GROUP, POLICY_GROUP}
    return sorted(p.name for p in gold.iterdir() if p.is_dir() and p.name not in fixed)


def current_catalog() -> tuple[GoldView, ...]:
    """The catalog reflecting what's currently published on disk."""
    return build_catalog(discover_provider_groups())


def current_catalog_by_name() -> dict[str, GoldView]:
    return {v.name: v for v in current_catalog()}
