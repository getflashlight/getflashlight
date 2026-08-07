"""Catalog of GOLD metric views — the public, consumable surface.

GOLD is split into per-provider **groups**: each distinct ``provider_name`` present
in the data gets its own group (``aws``, ``databricks``, ``microsoft``, …), plus the
fixed cross-provider groups below (``efficiency``, ``driver_health``, ``policy``),
whose views carry ``provider_name`` as a column instead. A group is both a directory
under ``gold/`` and a DuckDB schema, so a view's fully-qualified name is
``<group>.<view>`` (e.g. ``aws.monthly_bill``, ``efficiency.waste_record``).

The set of provider groups is **data-driven** — it is whatever was published, not a
hard-coded list — so :func:`current_catalog` reads the published groups off disk and
expands the static base specs over them. The MCP server and the dashboard read this
so there is one description of what's queryable.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

from flashlight.focus.enums import CostMetric
from flashlight.lake import paths

# The fixed group holding the efficiency/waste views — not per-provider:
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

# The fixed group holding the AI serving-usage (token) views. Like EFFICIENCY, not
# per-provider — every view carries provider_name as a column. Fed by the AI-usage plane
# (metrics.ai_usage) joined to the FOCUS plane for dollars. Named for its Parquet root, so
# it can never collide with a provider slug. Carries a cost_metric on the views that report
# dollars, because those dollars ARE the FOCUS EffectiveCost — joined in, never recomputed.
AI_USAGE_GROUP = "ai_usage"

# The fixed group holding the backing-storage views — the cloud storage bill labelled
# with the data platform that Unity Catalog says sits on it.
#
# Cross-provider BY CONSTRUCTION, which is why it can't be a provider group: every row
# carries TWO provider columns — `billing_provider_name` (who invoices it: AWS) and
# `platform_provider_name` (whose metadata claims it: Databricks). Fanning it out per
# provider would publish a permanently-empty databricks/backing_storage_month.parquet
# beside a populated aws one, and reading `aws.*` from the Databricks page would couple
# the two provider groups.
#
# It's also what keeps CLAUDE.md's "No cross-provider cost join" enforceable rather than
# merely observed: nothing here writes into gold/databricks/, so databricks.monthly_bill
# and the Databricks KPIs are untouched by construction. The join is AWS *cost* to
# Databricks *metadata* (a bucket list) — never AWS cost to Databricks cost.
STORAGE_GROUP = "storage"

# The fixed group holding the backing-compute views — the cloud compute (EC2) bill
# labelled with the Databricks cluster that node_timeline says ran on it. The identical
# shape as STORAGE_GROUP, for compute instead of storage — cross-provider by construction
# for the same reason (billing_provider_name/platform_provider_name), and the same
# enforceable "No cross-provider cost join" guarantee: nothing here writes into
# gold/databricks/, and the join is AWS *cost* to Databricks *metadata* (an
# instance/cluster map) — never AWS cost to Databricks cost.
COMPUTE_GROUP = "compute"


class MeasureUnit(StrEnum):
    """What a measure is denominated in.

    Declared rather than inferred from the column name, because names lie in both
    directions: ``savings_pct``/``tagged_pct``/``cost_pct_change`` all contain
    "cost" or read like money but are ratios, while ``rate_effect`` and
    ``volume_effect`` are dollars with no money-ish word in them. A consumer that
    guesses formats a percentage as ``$12`` sooner or later.
    """

    CURRENCY = "currency"  # a dollar figure in FLASHLIGHT_BASE_CURRENCY
    RATE = "rate"  # currency per unit (e.g. $/DBU) — money, but not additive
    PERCENT = "percent"  # 0-100 ratio
    COUNT = "count"  # a cardinality
    QUANTITY = "quantity"  # provider-native units (DBUs, GB-hours)
    DAYS = "days"


# Every measure in the catalog, by unit. Kept beside the view specs (the one place
# the measure vocabulary is defined) so a consumer needing to format or aggregate a
# figure reads a declaration instead of pattern-matching a name.
# ``test_catalog`` asserts this covers every measure any view declares, so adding a
# measure without classifying it fails there rather than mis-rendering downstream.
MEASURE_UNITS: dict[str, MeasureUnit] = {
    # Currency — additive dollar figures.
    "actual_to_date": MeasureUnit.CURRENCY,
    "billed_cost": MeasureUnit.CURRENCY,
    "billed_cost_after": MeasureUnit.CURRENCY,
    "billed_cost_at_last_seen": MeasureUnit.CURRENCY,
    "cost_delta": MeasureUnit.CURRENCY,
    "credit_cost": MeasureUnit.CURRENCY,
    "effective_cost": MeasureUnit.CURRENCY,
    "forecast_cost": MeasureUnit.CURRENCY,
    "gross_cost": MeasureUnit.CURRENCY,
    "list_cost": MeasureUnit.CURRENCY,
    "net_cost": MeasureUnit.CURRENCY,
    "rate_effect": MeasureUnit.CURRENCY,
    "realized_savings": MeasureUnit.CURRENCY,
    "recoverable_cost": MeasureUnit.CURRENCY,
    "recoverable_cost_at_last_seen": MeasureUnit.CURRENCY,
    "recoverable_cost_high_confidence": MeasureUnit.CURRENCY,
    "savings": MeasureUnit.CURRENCY,
    "tagged_cost": MeasureUnit.CURRENCY,
    "untagged_cost": MeasureUnit.CURRENCY,
    "volume_effect": MeasureUnit.CURRENCY,
    # AI serving. allocated_cost is a token-share split of an endpoint's FOCUS cost and is
    # NULL wherever tokens aren't the meter; unallocated_cost is the named complement, so
    # the two never overlap and must never be summed into a "total AI cost by project".
    "allocated_cost": MeasureUnit.CURRENCY,
    "unallocated_cost": MeasureUnit.CURRENCY,
    # Currency per native unit — a price, so summing it is meaningless.
    "cost_per_native_unit": MeasureUnit.RATE,
    "unit_rate": MeasureUnit.RATE,
    "cost_per_million_tokens": MeasureUnit.RATE,
    # Ratios — never additive, never currency-formatted.
    "cost_pct_change": MeasureUnit.PERCENT,
    "savings_pct": MeasureUnit.PERCENT,
    "tagged_pct": MeasureUnit.PERCENT,
    "utilization_pct": MeasureUnit.PERCENT,
    "error_rate_pct": MeasureUnit.PERCENT,
    # Cardinalities.
    "activity_count": MeasureUnit.COUNT,
    "commitment_count": MeasureUnit.COUNT,
    "entity_count": MeasureUnit.COUNT,
    "finding_count": MeasureUnit.COUNT,
    "line_count": MeasureUnit.COUNT,
    "query_count": MeasureUnit.COUNT,
    "tag_value_count": MeasureUnit.COUNT,
    "variant_count": MeasureUnit.COUNT,
    "request_count": MeasureUnit.COUNT,
    "error_request_count": MeasureUnit.COUNT,
    "endpoint_count": MeasureUnit.COUNT,
    "model_count": MeasureUnit.COUNT,
    "location_count": MeasureUnit.COUNT,
    "mapping_row_count": MeasureUnit.COUNT,
    # Provider-native quantities. Tokens are the metered billing unit for pay-per-token
    # serving — the same class as consumed_quantity (DBUs), not a cardinality of objects.
    "consumed_quantity": MeasureUnit.QUANTITY,
    "native_quantity": MeasureUnit.QUANTITY,
    "primary_signal_value": MeasureUnit.QUANTITY,
    "input_tokens": MeasureUnit.QUANTITY,
    "output_tokens": MeasureUnit.QUANTITY,
    "total_tokens": MeasureUnit.QUANTITY,
    "error_tokens": MeasureUnit.QUANTITY,
    "history_days": MeasureUnit.DAYS,
}

# The dimensions that are a *charge period* — the axis a trend runs along.
# An allowlist, not a "contains month/day" test: ``first_seen_month`` and
# ``last_seen_month`` are attributes of an entity (when it appeared, when it was last
# billed), and ``resolved_month`` is when a finding was fixed. Trending along any of
# those would silently answer a different question than the one asked. See CLAUDE.md,
# "charge-period grain only".
PERIOD_DIMENSIONS: frozenset[str] = frozenset({"charge_month", "charge_day"})


def measure_unit(measure: str) -> MeasureUnit | None:
    """The declared unit of *measure*, or None if it isn't a catalog measure (a
    ``run_sql`` result can return any column at all)."""
    return MEASURE_UNITS.get(measure)


def is_period_dimension(dimension: str) -> bool:
    return dimension in PERIOD_DIMENSIONS


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
    group: str  # provider group or a fixed group (== dir under gold/ and DuckDB schema)
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
        description="Net and gross spend per service category and service, by month, with "
        "list cost and savings (list − effective). This is monthly_bill at one finer grain "
        "over the same source rows, so summing it over every service reconciles to "
        "monthly_bill exactly — use it when you need a headline scoped to a subset of "
        "services rather than the whole provider.",
        cost_metric=CostMetric.EFFECTIVE_COST,
        dimensions=("provider_name", "service_category", "service_name", "charge_month"),
        measures=("net_cost", "gross_cost", "credit_cost", "list_cost", "savings"),
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
        view="ai_spend_month",
        title="AI/ML spend / month",
        description="The AI slice of the bill: net/gross spend and consumed quantity per (AI "
        "product, resource, SKU), by month. Scope is every row the provider categorizes as "
        "'AI and Machine Learning' PLUS AI products the Databricks FOCUS query files under "
        "another category — AI/BI Genie above all, which bills as warehouse-shaped usage. "
        "ai_product_family names the product ('model_serving', 'vector_search', 'ai_gateway', "
        "'ai_functions', 'foundation_model_training', 'agent_bricks', 'ai_runtime', "
        "'agent_evaluation', 'genie', 'ai_bi_dashboard', 'lakehouse_monitoring', "
        "'predictive_optimization'; NULL for a product not yet mapped). "
        "For the endpoint-shaped products resource_id IS the serving/vector-search endpoint "
        "id, which is the join key to the `ai` group's token views. project_tag is the "
        "endpoint's cost-allocation tag, NULL when it carries none — an untagged endpoint is "
        "the finding (see the endpoint_tagging policy rule), not a bucket. tags is the raw "
        "FOCUS Tags JSON so a consumer can attribute by any key, not only project. THIS VIEW "
        "IS COST ONLY: token counts, model identity and the requesting user are not in the "
        "bill at all — they live in ai.token_usage_month, fed by a separate telemetry pull. "
        "Rows here with none there mean the bill was read and the token telemetry was not, so "
        "never divide this cost by a token count from anywhere else.",
        cost_metric=CostMetric.EFFECTIVE_COST,
        dimensions=(
            "provider_name",
            "charge_month",
            "ai_product_family",
            "service_name",
            "resource_type",
            "resource_id",
            "resource_name",
            "sku_id",
            "consumed_unit",
            "project_tag",
            "tags",
        ),
        measures=("net_cost", "gross_cost", "consumed_quantity"),
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
        view="spend_by_tag_key_month",
        title="Spend by normalized tag key / month",
        description="Net spend per cost-allocation tag KEY, with case/separator variants folded "
        "together (epic/Epic, app-long/app_long) so one dimension ranks as one row. "
        "tag_key_variants/variant_count expose the collision itself — variant_count > 1 means "
        "the same dimension is being spelled several ways upstream. Complements "
        "spend_by_tag_month, which keeps the raw keys on purpose (that difference is a "
        "tagging-consistency finding, not noise). Measured over charges only (credits "
        "excluded) so it reconciles against spend_tag_coverage_month. DO NOT sum net_cost "
        "across keys for a total: a resource with two tags contributes its full cost to both, "
        "so the column total exceeds real tagged spend — use "
        "spend_tag_coverage_month.tagged_cost as the honest denominator.",
        cost_metric=CostMetric.EFFECTIVE_COST,
        dimensions=("provider_name", "charge_month", "tag_key_normalized"),
        measures=("net_cost", "tag_value_count", "variant_count"),
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
        view="spend_forecast_month",
        title="Spend forecast / month",
        description="Forward-looking spend. forecast_kind='run_rate' projects the current "
        "month from its completed-day average; 'trend' holds the mean of the trailing 3 "
        "complete months flat across the next 3 months and is NULL until 3 complete months "
        "exist (the month of the newest complete day never counts — it is still accruing). "
        "The newest day is excluded everywhere — billing exports land 24-48h late. "
        "ALWAYS CHECK history_days on a run_rate row before quoting it: a one-day mean "
        "extended over a month carries a ~30x error multiplier on a single day's delivery "
        "lag, so below 3 days the number is noise and below 7 it is indicative only (a week "
        "covers the weekday/weekend cycle). The figure is left un-NULLed because a 2-day mean "
        "is still a valid mean — it is the presentation that misleads, so compare it against "
        "actual_to_date rather than reporting it bare. On trend rows history_days is the day "
        "count inside the trailing-3 window (0 when the gate has not been met).",
        cost_metric=CostMetric.EFFECTIVE_COST,
        dimensions=("provider_name", "charge_month", "forecast_kind"),
        measures=("forecast_cost", "actual_to_date", "history_days"),
    ),
    ViewSpec(
        view="spend_tag_coverage_month",
        title="Tag coverage / month",
        description="How much of each month's spend carries at least one cost-allocation "
        "tag (tagged_cost/tagged_pct) versus none at all (untagged_cost). The honest "
        "denominator for the tag views, which drop untagged spend by construction. "
        "Measured over charges only (credits excluded), so tagged_pct is a real 0-100 "
        "share; net_cost reconciles against monthly_bill.",
        cost_metric=CostMetric.EFFECTIVE_COST,
        dimensions=("provider_name", "charge_month"),
        measures=("net_cost", "gross_cost", "tagged_cost", "untagged_cost", "tagged_pct"),
    ),
    ViewSpec(
        view="spend_untagged_by_service_month",
        title="Untagged spend by service / month",
        description="Charge-only tagged/untagged split per service (same definition as "
        "spend_tag_coverage_month, one grain finer). Answers 'which services lack "
        "cost-allocation tags?'. Fully-tagged services remain (tagged_pct=100); filter "
        "untagged_cost > 0 for the gap list. service_name is coalesced to '(no service)' "
        "when absent. Summing untagged_cost over services reconciles to "
        "spend_tag_coverage_month.untagged_cost for the same month.",
        cost_metric=CostMetric.EFFECTIVE_COST,
        dimensions=("provider_name", "service_name", "charge_month"),
        measures=("gross_cost", "tagged_cost", "untagged_cost", "tagged_pct"),
    ),
    ViewSpec(
        view="spend_untagged_by_resource_month",
        title="Untagged spend by resource / month",
        description="Untagged charges only (empty Tags, credits excluded), ranked per "
        "resource. Answers 'what do I open and tag?' under a service gap from "
        "spend_untagged_by_service_month — summing untagged_cost for one service "
        "reconciles to that service's untagged_cost. resource_id/name/type and "
        "workspace/region coalesced like resource_month so lines with no resource id "
        "stay visible as '(none)' / '(unattributed)'.",
        cost_metric=CostMetric.EFFECTIVE_COST,
        dimensions=(
            "provider_name",
            "service_name",
            "sku_id",
            "resource_type",
            "resource_id",
            "resource_name",
            "sub_account_id",
            "region_id",
            "charge_month",
        ),
        measures=("untagged_cost",),
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
        view="credits_month",
        title="Credits & adjustments",
        description="Credit and Adjustment lines only, at charge-description grain (the "
        "credit's identity — AWS puts the credit name and credit id there). net_cost is "
        "negative. Every other view nets these into its cost figure; this is where a "
        "one-off credit that swings a provider's month is identifiable on its own. Empty "
        "for providers whose bill carries no credits.",
        cost_metric=CostMetric.EFFECTIVE_COST,
        dimensions=(
            "provider_name",
            "charge_month",
            "charge_category",
            "service_name",
            "charge_description",
        ),
        measures=("net_cost", "line_count"),
    ),
    ViewSpec(
        view="spend_trend_daily",
        title="Daily spend trend",
        description="Daily net/gross spend per service; use is_partial_period to dim today. "
        "One row per (day, service), NOT per day — aggregate over service_name for a "
        "provider-wide daily series.",
        cost_metric=CostMetric.EFFECTIVE_COST,
        dimensions=("charge_day", "provider_name", "service_name"),
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
        view="waste_by_owner_month",
        title="Waste by owner / month",
        description="Recoverable spend ranked by owner, for two owner dimensions in one view "
        "(owner_dimension='owner_user' | 'owner_project') so their very different coverage is "
        "comparable — on real data owner_user lands on ~94% of findings and owner_project on "
        "~1%. owner_key is normalized (case-folded, whitespace-trimmed) so one human is one "
        "row, and it is NEVER NULL: unowned findings collapse into an '(unattributed)' key. "
        "That row is typically the LARGEST bucket and must never be filtered out — shared "
        "compute (SQL warehouses) has no owner by design, so this is not missing data. "
        "owner_kind flags 'service_principal' (a bare UUID, not a person) vs 'user'; "
        "owner_display is the human-readable label, owner_key the exact value to filter on. "
        "Never sum across lens — WASTE and OPPORTUNITY are different remedies.",
        cost_metric=CostMetric.EFFECTIVE_COST,
        dimensions=(
            "provider_name",
            "charge_month",
            "owner_dimension",
            "owner_kind",
            "owner_key",
            "owner_display",
            "lens",
        ),
        measures=(
            "recoverable_cost",
            "recoverable_cost_high_confidence",
            "billed_cost",
            "entity_count",
            "finding_count",
        ),
    ),
    ViewSpec(
        view="utilization_entity_month",
        title="Utilization & coverage by entity / month",
        description="One row per (entity, month) actually measured by an efficiency pull — "
        "the 'how well is my infra used?' surface, as opposed to waste_record's "
        "'what is wasteful?'. measurement_status separates 'measured' from "
        "'not_applicable' (shared compute, per-user shares, query shapes and tables have "
        "no per-entity utilization in principle) and 'unmeasured' (job/interactive/notebook "
        "where the pull ran but delivered no CPU telemetry) — so a consumer can tell "
        "'measured and fine' from 'never looked'. is_saturated_reading flags readings "
        "pegged at >=99.5%, a telemetry ceiling artifact rather than a verdict of "
        "perfectly right-sized. is_flagged_underutilized/waste_categories are semi-joined "
        "from efficiency.waste_record, so 'measured, low, and no rule fired' is "
        "expressible. COMPARABILITY: primary_signal_value is comparable only within one "
        "(primary_signal_name, primary_signal_unit) pair, and cost_per_native_unit only "
        "within one native_unit (DBU, MB and bytes all occur) — group before comparing; "
        "this view does no unit conversion on purpose.",
        cost_metric=CostMetric.EFFECTIVE_COST,
        dimensions=(
            "provider_name",
            "charge_month",
            "x_source_connector",
            "entity_type",
            "entity_id",
            "entity_name",
            "owner_user",
            "owner_project",
            "measurement_status",
            "activity_status",
            "is_saturated_reading",
            "is_flagged_underutilized",
            "primary_signal_name",
            "primary_signal_unit",
            "primary_signal_direction",
            "native_unit",
            "waste_categories",
        ),
        measures=(
            "utilization_pct",
            "activity_count",
            "billed_cost",
            "native_quantity",
            "cost_per_native_unit",
            "primary_signal_value",
        ),
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


# ── AI serving usage (the token plane) ───────────────────────────────────────
# Fixed group, fed by metrics.ai_usage joined to the FOCUS plane for dollars.
#
# THE ONE RULE EVERY VIEW HERE CARRIES: `cost_allocation_basis` says whether a dollar
# figure on the row is defensible, and `allocated_cost` is NULL wherever it isn't. Model
# serving bills two completely different ways — pay-per-token (metered per token, so a
# token-share split of the charge is a proportional split of a per-token charge) and
# provisioned throughput / provisioned compute (metered per provisioned HOUR, where an idle
# endpoint bills real money with zero tokens). Splitting a provisioned endpoint's cost by
# token share would hand the idle hours to whoever happened to send traffic. External models
# are a third case: Databricks bills the gateway hop, the model vendor bills the tokens, and
# that vendor's bill is not in this lake at all.
_BASIS_RULE = (
    "cost_allocation_basis is 'measured_tokens' | 'unallocated' | 'external_passthrough' | "
    "'unknown'. allocated_cost and cost_per_million_tokens are populated ONLY for "
    "'measured_tokens' (pay-per-token serving, where tokens are the meter) and are NULL — "
    "not zero — for every other basis: provisioned endpoints bill by the hour, so a "
    "token-share split would move idle capacity's cost onto whoever sent traffic, and for "
    "external models Databricks bills only the gateway hop while the vendor bills the "
    "tokens. NULL here means 'not allocatable by token', never '$0'. Token counts are "
    "honest for every basis — only the dollars are conditional."
)

AI_USAGE_BASE_VIEWS: tuple[ViewSpec, ...] = (
    ViewSpec(
        view="endpoint_month",
        title="AI endpoint usage / month",
        description="One row per (AI serving endpoint, month): the endpoint's FOCUS cost "
        "beside the token volume and request count it served. net_cost is joined from the "
        "bill (silver.focus_normalized on resource_id = endpoint_id), never recomputed, so "
        "it reconciles to <group>.ai_spend_month by construction. An endpoint with cost but "
        "no token rows still appears — that is an endpoint whose telemetry was never "
        "measured (or a genuinely silent provisioned endpoint), and hiding it would make "
        "unmeasured look like efficient. token_coverage_status distinguishes the two. "
        + _BASIS_RULE,
        cost_metric=CostMetric.EFFECTIVE_COST,
        dimensions=(
            "provider_name",
            "charge_month",
            "endpoint_id",
            "endpoint_name",
            "serving_mode",
            "workload_type",
            "scale_to_zero_enabled",
            "cost_allocation_basis",
            "token_coverage_status",
        ),
        measures=(
            "net_cost",
            "unallocated_cost",
            "input_tokens",
            "output_tokens",
            "total_tokens",
            "error_tokens",
            "request_count",
            "error_request_count",
            "error_rate_pct",
            "cost_per_million_tokens",
        ),
    ),
    ViewSpec(
        view="model_month",
        title="AI model unit economics / month",
        description="Token volume and (where allocatable) cost per served model per month — "
        "the input to 'is a cheaper model serving this traffic?'. Deliberately descriptive: "
        "a cheaper model is not necessarily a substitutable one, so this ranks unit "
        "economics and leaves the capability judgement to a human; there is no waste rule "
        "behind it. COMPARABILITY: compare cost_per_million_tokens only within one "
        "(serving_mode, model_kind) pair — a provisioned endpoint's implied rate and a "
        "pay-per-token rate answer different questions — and never sum it. " + _BASIS_RULE,
        cost_metric=CostMetric.EFFECTIVE_COST,
        dimensions=(
            "provider_name",
            "charge_month",
            "endpoint_id",
            "endpoint_name",
            "model_name",
            "model_version",
            "model_kind",
            "serving_mode",
            "cost_allocation_basis",
        ),
        measures=(
            "input_tokens",
            "output_tokens",
            "total_tokens",
            "request_count",
            "error_request_count",
            "allocated_cost",
            "cost_per_million_tokens",
        ),
    ),
    ViewSpec(
        view="project_month",
        title="AI token usage by project / month",
        description="Token volume and allocatable cost per project per month — 'how much is "
        "each project spending on AI?'. project_key is NEVER NULL: spend with no project "
        "attribution lands in '(unattributed)', which on real data is typically the largest "
        "bucket and must never be filtered out — that row IS the finding (see the "
        "endpoint_tagging policy rule). project_source says where the attribution came from: "
        "'usage_context' (request-level, client-supplied, usually sparse), 'endpoint_tag' "
        "(the endpoint's cost-allocation tag, the high-coverage source), or 'none'. "
        + _BASIS_RULE,
        cost_metric=CostMetric.EFFECTIVE_COST,
        dimensions=(
            "provider_name",
            "charge_month",
            "project_key",
            "project_source",
            "serving_mode",
            "cost_allocation_basis",
        ),
        measures=(
            "input_tokens",
            "output_tokens",
            "total_tokens",
            "request_count",
            "error_request_count",
            "allocated_cost",
            "endpoint_count",
            "model_count",
        ),
    ),
    ViewSpec(
        view="requester_month",
        title="AI token usage by user / month",
        description="Token volume and allocatable cost per requesting identity per month — "
        "'who is using AI, and how much?'. requester_key is NEVER NULL ('(unattributed)' "
        "when the telemetry recorded no requester). requester_kind separates 'user' from "
        "'service_principal' (a bare UUID identity) from 'unattributed', because a service "
        "principal's usage is an application's, not a person's, and ranking them together "
        "reads as one person doing an implausible amount of work. " + _BASIS_RULE,
        cost_metric=CostMetric.EFFECTIVE_COST,
        dimensions=(
            "provider_name",
            "charge_month",
            "requester_key",
            "requester_display",
            "requester_kind",
            "serving_mode",
            "cost_allocation_basis",
        ),
        measures=(
            "input_tokens",
            "output_tokens",
            "total_tokens",
            "request_count",
            "error_request_count",
            "allocated_cost",
            "endpoint_count",
            "model_count",
        ),
    ),
)


# ── Backing storage (the cloud storage bill behind a data platform) ──────────────
# Fixed group, fed by metrics.storage_location (Unity Catalog's bucket map) joined to
# the FOCUS plane's S3 rows.
#
# THE ONE RULE EVERY VIEW HERE CARRIES: these dollars are billed by AWS, live ONLY in
# this storage GOLD group (aws.* GOLD excludes Amazon S3 via silver.focus_provider_bill),
# and must never be added to databricks.monthly_bill — Databricks' own bill covers DBU
# compute only, and summing the two is exactly the TCO join CLAUDE.md removed. MCP and
# the assistant read these descriptions, so the rule has to live here.
_TWO_BILLS_RULE = (
    "This money is billed by AWS and is excluded from aws.* GOLD — storage.backing_storage_month "
    "is its only GOLD home (mapped rows are named Databricks Storage). It is NOT "
    "Databricks DBU spend: Databricks' own bill covers compute only, so these are two "
    "separate bills and must never be summed into one 'total Databricks cost'. Report "
    "them side by side, never added."
)

STORAGE_BASE_VIEWS: tuple[ViewSpec, ...] = (
    ViewSpec(
        view="storage_location",
        title="Storage location map",
        description="Unity Catalog's own map of which cloud object-storage URLs back the "
        "metastore, its catalogs and its external locations — metadata, no cost. "
        "location_kind matters: only 'metastore_root' is Databricks-MANAGED storage and "
        "therefore the only kind that costs anything in storage.backing_storage_month; "
        "'catalog' and 'external_location' rows are recorded purely as the audit trail that "
        "explains why a bucket is NOT counted. "
        "key_prefix='(bucket root)' means the location addresses the whole bucket; any "
        "other value means the platform holds data under a prefix only, so that bucket "
        "may hold unrelated data too. One snapshot per snapshot_month (current state as "
        "pulled, NOT a charge period — you cannot trend along it); older snapshots are "
        "kept as the audit trail for when a bucket became platform-backed.",
        cost_metric=None,
        dimensions=(
            "platform_provider_name",
            "snapshot_month",
            "location_kind",
            "location_name",
            "url",
            "scheme",
            "cloud_provider_name",
            "bucket_name",
            "key_prefix",
            "is_read_only",
            "credential_name",
            "x_source_connector",
        ),
        measures=(),
    ),
    ViewSpec(
        view="backing_storage_month",
        title="Backing storage cost / month",
        description="AWS-billed Amazon S3 cost per (bucket, month, subcategory), labelled "
        "by whether the bucket is Databricks-MANAGED storage. Mapped rows "
        "(mapping='databricks') use service_name 'Databricks Storage' — they are excluded "
        "from aws.* GOLD (silver.focus_provider_bill) and this view is their only GOLD home. "
        "EVERY S3 row is here, mapped or not, so mapping='databricks' is a numerator with "
        "an honest denominator — sum across all mapping values and you get the account's "
        "whole S3 bill. "
        f"{_TWO_BILLS_RULE} "
        "mapping: 'databricks' = the bucket holds Databricks-MANAGED storage — a Unity "
        "Catalog metastore root or a MANAGED_CATALOG's storage root, i.e. storage Databricks "
        "provisioned and whose lifecycle it owns; 'unmapped' = it does not; "
        "'no_resource_id' = S3 cost carrying no ResourceId at all (a cost_explorer-sourced "
        "AWS connection never has one), attributable to no bucket. "
        "EXTERNAL LOCATIONS AND FOREIGN CATALOGS ARE DELIBERATELY EXCLUDED and land in "
        "'unmapped': that data pre-existed and is merely registered for access (a federated "
        "Glue/Hive catalog, Delta Sharing), so the bucket exists whether or not Databricks "
        "reads it, and counting it would double-claim spend belonging to whoever owns that "
        "pipeline. Consequently this figure is a FLOOR on Databricks-owned storage, never a "
        "ceiling — per-workspace DBFS root buckets are managed storage but are not counted "
        "here, so do NOT present it as complete. Never widen it by adding 'unmapped' buckets "
        "back in. "
        "managed_name / managed_kind name WHICH Unity Catalog object owns the bucket — a "
        "catalog name (managed_kind='catalog') or a metastore name "
        "(managed_kind='metastore_root') — so GROUP BY managed_name gives storage cost per "
        "catalog. '(not managed)' on every unmapped row. A metastore root wins when a bucket "
        "carries both. '(shared by N catalogs)' means several catalogs live in one bucket and "
        "the AWS bill is bucket-grained, so their costs genuinely cannot be separated — do "
        "not attribute that figure to any single catalog. "
        "mapping_confidence: 'whole_bucket' = the metastore root addresses the bucket root, "
        "so the whole bucket is Databricks storage; 'prefix_scoped' = it claims a key prefix "
        "only (the usual case, since a metastore root is normally s3://bucket/<metastore-id>) "
        "and the AWS bill is per-bucket, so this bucket's cost is an UPPER BOUND on "
        "Databricks' share, never a Databricks-only figure.",
        cost_metric=CostMetric.EFFECTIVE_COST,
        dimensions=(
            "billing_provider_name",
            "service_name",
            "bucket_name",
            "mapping",
            "platform_provider_name",
            "managed_name",
            "managed_kind",
            "mapping_confidence",
            "cost_subcategory",
            "region_id",
            "charge_month",
        ),
        measures=("net_cost", "gross_cost", "location_count"),
    ),
)


# ── Backing compute (the cloud compute bill behind a data platform) ──────────────
# Fixed group, fed by metrics.compute_instance (Databricks' node_timeline-derived
# instance/cluster map) joined to the FOCUS plane's EC2 rows.
#
# THE ONE RULE EVERY VIEW HERE CARRIES: these dollars are billed by AWS, live ONLY in
# this compute GOLD group (aws.* GOLD excludes Amazon Elastic Compute Cloud via
# silver.focus_provider_bill), and must never be added to databricks.monthly_bill —
# Databricks' own bill covers DBU compute only, and summing the two is exactly the TCO
# join CLAUDE.md removed. MCP and the assistant read these descriptions, so the rule
# has to live here.
_TWO_BILLS_RULE_COMPUTE = (
    "This money is billed by AWS and is excluded from aws.* GOLD — compute.backing_compute_month "
    "is its only GOLD home (mapped rows are named Databricks Compute). It is NOT "
    "Databricks DBU spend: Databricks' own bill covers compute only, so these are two "
    "separate bills and must never be summed into one 'total Databricks cost'. Report "
    "them side by side, never added."
)

COMPUTE_BASE_VIEWS: tuple[ViewSpec, ...] = (
    ViewSpec(
        view="compute_instance",
        title="Compute instance map",
        description="Databricks' own map of which cloud VM instance backed which cluster, "
        "sourced from system.compute.node_timeline — metadata, no cost. CLASSIC compute "
        "only: serverless SQL warehouses, serverless jobs and DLT serverless pipelines "
        "have no rows here at all (no customer-visible instance to report), so absence "
        "here is not evidence a cluster had no cloud infra cost. One row per "
        "(cluster_id, instance_id, charge_month) — a real charge period, not a snapshot, "
        "unlike storage.storage_location.",
        cost_metric=None,
        dimensions=(
            "platform_provider_name",
            "charge_month",
            "cluster_id",
            "cluster_name",
            "owner_user",
            "instance_id",
            "is_driver",
            "node_type",
            "x_source_connector",
        ),
        measures=(),
    ),
    ViewSpec(
        view="backing_compute_month",
        title="Backing compute cost / month",
        description="AWS-billed Amazon EC2 cost per (instance, month), labelled by "
        "whether the instance backed a Databricks cluster. Mapped rows "
        "(mapping='databricks') use service_name 'Databricks Compute' — they are "
        "excluded from aws.* GOLD (silver.focus_provider_bill) and this view is their "
        "only GOLD home. EVERY EC2 row is here, mapped or not, so mapping='databricks' "
        "is a numerator with an honest denominator — sum across all mapping values and "
        "you get the account's whole EC2 bill. "
        f"{_TWO_BILLS_RULE_COMPUTE} "
        "mapping: 'databricks' = this EC2 instance backed a Databricks cluster (matched "
        "by instance id AND charge_month against system.compute.node_timeline); "
        "'unmapped' = it did not (includes non-instance EC2-service resources such as "
        "EBS volumes, which carry a ResourceId but never match an instance map); "
        "'no_resource_id' = EC2 cost carrying no ResourceId at all (a cost_explorer-sourced "
        "AWS connection never has one), attributable to no instance. "
        "CLASSIC COMPUTE ONLY, SO THIS FIGURE IS A FLOOR, never a ceiling: serverless "
        "compute has no customer-visible instance for Databricks to report, so it can "
        "never appear as mapped here even though it may carry real DBU cost. The map is "
        "also bounded by node_timeline's ~90-day retention — an instance whose activity "
        "predates the retention window at the time of ingest can never be recovered. "
        "cluster_id names WHICH Databricks cluster owns the instance — '(not managed)' "
        "on every unmapped row; cluster_name/owner_user are the human-readable name and "
        "owner from system.compute.clusters, falling back to the bare cluster_id/'(unknown)' "
        "when that table has no row for it (aged out, or the token can't read it) — never "
        "dropped as a row, just less readable. instance_role is 'driver'/'worker'/'n/a' "
        "(unmapped). pricing_category is FOCUS's own column, carried straight from the AWS "
        "bill (no Databricks-side join): 'Dynamic' is FOCUS's term for Spot (and other "
        "provider-variable pricing) — there is no separate 'spot' value — 'Committed' means "
        "an existing Reserved Instance/Savings Plan discounted the charge, 'Standard' is "
        "on-demand/negotiated-rate; '(unknown)' means the AWS export carried no value for "
        "this row (older exports, or a charge FOCUS itself allows to be null). "
        "Unlike storage.backing_storage_month, this join is per (instance_id, "
        "charge_month) rather than a present-tense snapshot applied to all history — "
        "node_timeline itself reports bounded historical activity, not current state.",
        cost_metric=CostMetric.EFFECTIVE_COST,
        dimensions=(
            "billing_provider_name",
            "service_name",
            "instance_id",
            "mapping",
            "platform_provider_name",
            "cluster_id",
            "cluster_name",
            "owner_user",
            "instance_role",
            "node_type",
            "region_id",
            "pricing_category",
            "charge_month",
        ),
        measures=("net_cost", "gross_cost", "mapping_row_count"),
    ),
)


def provider_group(provider_name: str) -> str:
    """Slug a ``provider_name`` into a filesystem/DuckDB-safe group id.

    ``"AWS"`` → ``"aws"``, ``"Databricks"`` → ``"databricks"``,
    ``"Google Cloud"`` → ``"google_cloud"``.
    """
    return re.sub(r"[^a-z0-9]+", "_", provider_name.lower()).strip("_")


_PROVIDER_BASE_BY_VIEW: dict[str, ViewSpec] = {spec.view: spec for spec in PROVIDER_BASE_VIEWS}


def provider_view_dimensions(view: str) -> tuple[str, ...]:
    """Declared dimensions of a provider-scoped base view — ``()`` if unknown.

    The one place a consumer asks "can I filter this view by X?" without restating a
    hard-coded list. These are the same tuples ``gold/reader.py`` builds its SELECT and
    its filter allowlist from, so a column absent here is invisible to every consumer
    even when it's present in the Parquet — which makes them the honest answer to that
    question rather than documentation of it.
    """
    spec = _PROVIDER_BASE_BY_VIEW.get(view)
    return spec.dimensions if spec else ()


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
    """Expand the base specs over the provider groups + the fixed efficiency/
    driver_health/policy/ai_usage/storage/compute groups."""
    views: list[GoldView] = []
    for group in provider_groups:
        views.extend(_view(group, spec) for spec in PROVIDER_BASE_VIEWS)
    views.extend(_view(EFFICIENCY_GROUP, spec) for spec in EFFICIENCY_BASE_VIEWS)
    views.extend(_view(DRIVER_HEALTH_GROUP, spec) for spec in DRIVER_HEALTH_BASE_VIEWS)
    views.extend(_view(POLICY_GROUP, spec) for spec in POLICY_BASE_VIEWS)
    views.extend(_view(AI_USAGE_GROUP, spec) for spec in AI_USAGE_BASE_VIEWS)
    views.extend(_view(STORAGE_GROUP, spec) for spec in STORAGE_BASE_VIEWS)
    views.extend(_view(COMPUTE_GROUP, spec) for spec in COMPUTE_BASE_VIEWS)
    return tuple(views)


#: Every non-provider group under ``gold/``. A fixed group missing from this set becomes a
#: **phantom provider**: :func:`discover_provider_groups` hands it to the nav and to
#: ``router._provider_page``, which then renders ``provider_focus`` against a
#: ``<group>.monthly_bill`` that doesn't exist. Add a new fixed group here in the same
#: commit that creates it.
FIXED_GROUPS: frozenset[str] = frozenset(
    {
        EFFICIENCY_GROUP,
        DRIVER_HEALTH_GROUP,
        POLICY_GROUP,
        AI_USAGE_GROUP,
        STORAGE_GROUP,
        COMPUTE_GROUP,
    }
)


def discover_provider_groups() -> list[str]:
    """Provider groups published under ``gold/`` (excludes every :data:`FIXED_GROUPS`
    one: efficiency/driver_health/policy/ai_usage/storage/compute)."""
    gold = paths.gold_dir()
    if not gold.exists():
        return []
    return sorted(
        p.name for p in gold.iterdir() if p.is_dir() and p.name not in FIXED_GROUPS
    )


def current_catalog() -> tuple[GoldView, ...]:
    """The catalog reflecting what's currently published on disk."""
    return build_catalog(discover_provider_groups())


def current_catalog_by_name() -> dict[str, GoldView]:
    return {v.name: v for v in current_catalog()}
