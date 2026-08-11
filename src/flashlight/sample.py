"""Deterministic, schema-driven data for ``flashlight sample``.

The sample is a coherent organization, not a downloaded CSV.  FOCUS is the
authoritative cost plane; Redshift and Databricks records add entity metadata
and telemetry so every dashboard drill-down uses the normal read path.
"""

from __future__ import annotations

import runpy
import shutil
from calendar import monthrange
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pyarrow.parquet as pq
import typer
from pydantic import BaseModel, Field, field_validator, model_validator

from flashlight.efficiency.model import EfficiencyRecord, EntityType
from flashlight.focus.enums import ChargeCategory, ComputeClass, PricingCategory, ServiceCategory
from flashlight.focus.model import FocusRecord
from flashlight.ingest.base import IngestWindow
from flashlight.lake import (
    ai_usage,
    bronze,
    compute_instances,
    driver_health,
    metrics,
    paths,
    redshift_policy_config,
    runlog,
    storage_locations,
)
from flashlight.lake.ai_usage_schema import AiUsageRecord
from flashlight.lake.compute_instance_schema import ComputeInstanceRecord
from flashlight.lake.driver_health_schema import DriverHealthRecord
from flashlight.lake.redshift_policy_config_schema import RedshiftPolicyConfigRecord
from flashlight.lake.storage_location_schema import StorageLocationRecord
from flashlight.transform.runner import build_gold

SAMPLE_CONNECTOR = "flashlight_demo_focus"
REDSHIFT_CONNECTOR = "flashlight_demo_redshift"
DATABRICKS_CONNECTOR = "flashlight_demo_databricks"
_SNOWFLAKE_SYNTHETIC_DIR = Path(__file__).resolve().parents[2] / "snowflake" / "synthetic_data"


def generate_snowflake_dashboard_demo() -> None:
    """Generate synthetic ACCOUNT_USAGE and install it under FLASHLIGHT_HOME.

    The generator remains beside its source data specification in the repository, but
    ``fl sample`` installs the Parquet into ``account_usage_dir()`` so the dashboard
    reads the same lake layout as a live ingest.
    """
    from flashlight.lake import account_usage, paths

    generator = _SNOWFLAKE_SYNTHETIC_DIR / "generate.py"
    namespace = runpy.run_path(str(generator))
    # Write flat Parquet next to the generator, then copy into the lake Hive layout.
    namespace["main"]()
    paths.ensure_layout()
    # Demo window ends 2026-08-08 — stamp partitions under that month.
    account_usage.install_flat_parquets(
        _SNOWFLAKE_SYNTHETIC_DIR,
        charge_month="2026-08",
    )


def cleanup_snowflake_dashboard_demo() -> None:
    """Remove Snowflake demo Parquet (repo + lake ACCOUNT_USAGE), never the generator.

    Clears ``FLASHLIGHT_HOME/account_usage/`` entirely — that root holds both sample
    installs and live ingest dumps, so re-run ``flashlight ingest`` afterward if you
    still need live Visibility data.
    """
    from flashlight.lake import account_usage

    for parquet in _SNOWFLAKE_SYNTHETIC_DIR.glob("*.parquet"):
        parquet.unlink()
    account_usage.clear_account_usage()


@dataclass(frozen=True)
class DemoSku:
    """A production-shaped Databricks SKU family and its relative direct-cost weight."""

    service: str
    sku_id: str
    weight: Decimal
    category: ServiceCategory
    compute_class: ComputeClass


_DATABRICKS_SKUS: tuple[DemoSku, ...] = (
    DemoSku(
        "JOBS",
        "ENTERPRISE_JOBS_COMPUTE",
        Decimal("80459"),
        ServiceCategory.ANALYTICS,
        ComputeClass.CLASSIC,
    ),
    DemoSku(
        "SQL",
        "ENTERPRISE_SERVERLESS_SQL_COMPUTE_US_WEST_OREGON",
        Decimal("40701"),
        ServiceCategory.ANALYTICS,
        ComputeClass.SERVERLESS,
    ),
    DemoSku(
        "ALL_PURPOSE",
        "ENTERPRISE_ALL_PURPOSE_COMPUTE",
        Decimal("27383"),
        ServiceCategory.ANALYTICS,
        ComputeClass.CLASSIC,
    ),
    DemoSku(
        "JOBS",
        "ENTERPRISE_JOBS_COMPUTE_(PHOTON)",
        Decimal("14429"),
        ServiceCategory.ANALYTICS,
        ComputeClass.CLASSIC,
    ),
    DemoSku(
        "MODEL_SERVING",
        "ENTERPRISE_SERVERLESS_REAL_TIME_INFERENCE_US_WEST_OREGON",
        # Keep the sample's AI page substantial enough to demonstrate endpoint,
        # requester, token, and remediation drill-throughs (about $14K over its window).
        Decimal("32829"),
        ServiceCategory.AI_AND_MACHINE_LEARNING,
        ComputeClass.SERVERLESS,
    ),
    DemoSku(
        "ALL_PURPOSE",
        "ENTERPRISE_ALL_PURPOSE_SERVERLESS_COMPUTE_US_WEST_OREGON",
        Decimal("11020"),
        ServiceCategory.ANALYTICS,
        ComputeClass.SERVERLESS,
    ),
    DemoSku(
        "SQL",
        "ENTERPRISE_SQL_PRO_COMPUTE_US_WEST_OREGON",
        Decimal("10353"),
        ServiceCategory.ANALYTICS,
        ComputeClass.SERVERLESS,
    ),
    DemoSku(
        "JOBS",
        "ENTERPRISE_JOBS_SERVERLESS_COMPUTE_US_WEST_OREGON",
        Decimal("7251"),
        ServiceCategory.ANALYTICS,
        ComputeClass.SERVERLESS,
    ),
    DemoSku(
        "DATABASE",
        "ENTERPRISE_DATABASE_SERVERLESS_COMPUTE_US_WEST_OREGON",
        Decimal("4791"),
        ServiceCategory.DATABASES,
        ComputeClass.SERVERLESS,
    ),
    DemoSku(
        "ALL_PURPOSE",
        "ENTERPRISE_ALL_PURPOSE_COMPUTE_(PHOTON)",
        Decimal("890"),
        ServiceCategory.ANALYTICS,
        ComputeClass.CLASSIC,
    ),
    DemoSku(
        "STORAGE",
        "ENTERPRISE_DATABRICKS_STORAGE_US_WEST_OREGON",
        Decimal("328"),
        ServiceCategory.STORAGE,
        ComputeClass.NOT_APPLICABLE,
    ),
    DemoSku(
        "NETWORKING",
        "PUBLIC_CONNECTIVITY_DATA_PROCESSED_US_WEST_OREGON",
        Decimal("69"),
        ServiceCategory.NETWORKING,
        ComputeClass.NOT_APPLICABLE,
    ),
    DemoSku(
        "NETWORKING",
        "INTER_AVAILABILITY_ZONE_EGRESS",
        Decimal("3"),
        ServiceCategory.NETWORKING,
        ComputeClass.NOT_APPLICABLE,
    ),
    DemoSku(
        "NETWORKING",
        "INTERNET_EGRESS_FROM_US_WEST_OREGON",
        Decimal("1"),
        ServiceCategory.NETWORKING,
        ComputeClass.NOT_APPLICABLE,
    ),
    DemoSku(
        "GENIE",
        "GENIE_FREE_USAGE",
        Decimal("1"),
        ServiceCategory.AI_AND_MACHINE_LEARNING,
        ComputeClass.SERVERLESS,
    ),
    # Keep the visible attribution table production-shaped: the long tail matters
    # when demonstrating filtering/exporting, even though it is small in dollar terms.
    DemoSku(
        "INTERACTIVE",
        "ENTERPRISE_INTERACTIVE_COMPUTE",
        Decimal("6844"),
        ServiceCategory.ANALYTICS,
        ComputeClass.CLASSIC,
    ),
    DemoSku(
        "PREDICTIVE_OPTIMIZATION",
        "PREDICTIVE_OPTIMIZATION",
        Decimal("4937"),
        ServiceCategory.ANALYTICS,
        ComputeClass.SERVERLESS,
    ),
    DemoSku(
        "APPS",
        "DATABRICKS_APPS",
        Decimal("2401"),
        ServiceCategory.ANALYTICS,
        ComputeClass.SERVERLESS,
    ),
    DemoSku(
        "ONLINE_TABLES",
        "ONLINE_TABLES",
        Decimal("1178"),
        ServiceCategory.DATABASES,
        ComputeClass.SERVERLESS,
    ),
    DemoSku(
        "LAKEBASE",
        "LAKEBASE_COMPUTE",
        Decimal("707"),
        ServiceCategory.DATABASES,
        ComputeClass.SERVERLESS,
    ),
    DemoSku(
        "DATA_SHARING",
        "DELTA_SHARING",
        Decimal("70"),
        ServiceCategory.ANALYTICS,
        ComputeClass.SERVERLESS,
    ),
    DemoSku(
        "FINE_GRAINED_ACCESS_CONTROL",
        "FINE_GRAINED_ACCESS_CONTROL",
        Decimal("1"),
        ServiceCategory.ANALYTICS,
        ComputeClass.NOT_APPLICABLE,
    ),
    DemoSku(
        "AI_GATEWAY",
        "AI_GATEWAY",
        Decimal("1"),
        ServiceCategory.AI_AND_MACHINE_LEARNING,
        ComputeClass.SERVERLESS,
    ),
)


class DemoPerson(BaseModel):
    name: str
    email: str
    team: str

    @field_validator("email")
    @classmethod
    def valid_email(cls, value: str) -> str:
        if "@" not in value or value.startswith("@") or value.endswith("@"):
            raise ValueError("email must be a complete address")
        return value.lower()


class DemoEntity(BaseModel):
    id: str
    name: str
    owner_email: str
    project: str

    @field_validator("owner_email")
    @classmethod
    def valid_owner_email(cls, value: str) -> str:
        if "@" not in value or value.startswith("@") or value.endswith("@"):
            raise ValueError("owner_email must be a complete address")
        return value.lower()


class DemoScenario(BaseModel):
    """Closed vocabulary used by the generator; invalid references fail at startup."""

    people: list[DemoPerson]
    redshift_clusters: list[DemoEntity]
    databricks_clusters: list[DemoEntity]
    endpoints: list[DemoEntity]
    months: list[date] = Field(min_length=1)

    @model_validator(mode="after")
    def references_are_canonical(self) -> DemoScenario:
        emails = {str(person.email) for person in self.people}
        ids: set[str] = set()
        for entity in (*self.redshift_clusters, *self.databricks_clusters, *self.endpoints):
            if str(entity.owner_email) not in emails:
                raise ValueError(f"{entity.name} references unknown owner {entity.owner_email}")
            if entity.id in ids:
                raise ValueError(f"duplicate demo entity id {entity.id}")
            ids.add(entity.id)
        return self


def scenario() -> DemoScenario:
    """The fixed, human-readable organization shown by the demo."""
    return DemoScenario(
        people=[
            DemoPerson(
                name="Avery Chen", email="avery.chen@northstar.example", team="Data Platform"
            ),
            DemoPerson(
                name="Jordan Patel", email="jordan.patel@northstar.example", team="Analytics"
            ),
            DemoPerson(
                name="Morgan Reyes", email="morgan.reyes@northstar.example", team="ML Platform"
            ),
            DemoPerson(
                name="Priya Shah", email="priya.shah@northstar.example", team="Data Engineering"
            ),
            DemoPerson(
                name="Noah Williams", email="noah.williams@northstar.example", team="Finance"
            ),
            DemoPerson(
                name="Elena Garcia",
                email="elena.garcia@northstar.example",
                team="Product Analytics",
            ),
        ],
        redshift_clusters=[
            DemoEntity(
                id="redshift-prod-analytics",
                name="prod-analytics",
                owner_email="jordan.patel@northstar.example",
                project="analytics",
            ),
            DemoEntity(
                id="redshift-finance",
                name="finance-reporting",
                owner_email="avery.chen@northstar.example",
                project="finance",
            ),
        ],
        databricks_clusters=[
            DemoEntity(
                id="0301-analytics-jobs",
                name="analytics-jobs",
                owner_email="jordan.patel@northstar.example",
                project="analytics",
            ),
            DemoEntity(
                id="0301-feature-pipeline",
                name="feature-pipeline",
                owner_email="morgan.reyes@northstar.example",
                project="ml-platform",
            ),
            DemoEntity(
                id="0301-fraud-training",
                name="fraud-training-photon",
                owner_email="priya.shah@northstar.example",
                project="risk",
            ),
            DemoEntity(
                id="0301-shared-analytics",
                name="shared-analytics",
                owner_email="elena.garcia@northstar.example",
                project="product-analytics",
            ),
            DemoEntity(
                id="0301-orchestration",
                name="orchestration-common",
                owner_email="avery.chen@northstar.example",
                project="data-platform",
            ),
            *[
                DemoEntity(
                    id=(
                        f"job-{1059200000000000 + job_index}-"
                        f"run-{403840000000000 + job_index * 7919}-pipeline-cluster"
                    ),
                    name=(
                        f"job-{domain}-{job_index + 1:03d}-"
                        f"{'photon' if job_index % 5 == 0 else 'standard'}"
                    ),
                    owner_email=(
                        "jordan.patel@northstar.example",
                        "morgan.reyes@northstar.example",
                        "priya.shah@northstar.example",
                        "avery.chen@northstar.example",
                        "elena.garcia@northstar.example",
                        "noah.williams@northstar.example",
                    )[job_index % 6],
                    project=domain,
                )
                for job_index, domain in enumerate(
                    (
                        "analytics",
                        "ml-platform",
                        "risk",
                        "data-platform",
                        "product-analytics",
                        "finance",
                    )
                    * 12
                )
            ],
            *[
                DemoEntity(
                    id=f"interactive-{cluster_index + 1:03d}",
                    name=(
                        f"Single User Cluster - {owner}"
                        if cluster_index % 3 == 0
                        else f"{project}-shared-notebook-{cluster_index + 1:02d}"
                    ),
                    owner_email=owner,
                    project=project,
                )
                for cluster_index, (owner, project) in enumerate(
                    [
                        ("jordan.patel@northstar.example", "analytics"),
                        ("morgan.reyes@northstar.example", "ml-platform"),
                        ("priya.shah@northstar.example", "risk"),
                        ("avery.chen@northstar.example", "data-platform"),
                        ("elena.garcia@northstar.example", "product-analytics"),
                        ("noah.williams@northstar.example", "finance"),
                    ]
                    * 5
                )
            ],
            *[
                DemoEntity(
                    id=f"sql-warehouse-{warehouse_index + 1:02d}",
                    name=f"{project}-sql-warehouse-{warehouse_index + 1:02d}",
                    owner_email=owner,
                    project=project,
                )
                for warehouse_index, (owner, project) in enumerate(
                    [
                        ("jordan.patel@northstar.example", "analytics"),
                        ("elena.garcia@northstar.example", "product-analytics"),
                        ("noah.williams@northstar.example", "finance"),
                        ("priya.shah@northstar.example", "risk"),
                        ("avery.chen@northstar.example", "data-platform"),
                        ("morgan.reyes@northstar.example", "ml-platform"),
                    ]
                )
            ],
        ],
        endpoints=[
            DemoEntity(
                id="endpoint-support-assistant",
                name="support-assistant",
                owner_email="morgan.reyes@northstar.example",
                project="ml-platform",
            ),
            DemoEntity(
                id="endpoint-fraud-score",
                name="fraud-score",
                owner_email="priya.shah@northstar.example",
                project="risk",
            ),
            DemoEntity(
                id="endpoint-search-reranker",
                name="search-reranker",
                owner_email="morgan.reyes@northstar.example",
                project="ml-platform",
            ),
            DemoEntity(
                id="endpoint-insights-copilot",
                name="insights-copilot",
                owner_email="elena.garcia@northstar.example",
                project="product-analytics",
            ),
            DemoEntity(
                id="endpoint-document-extract",
                name="document-extract",
                owner_email="noah.williams@northstar.example",
                project="finance",
            ),
        ],
        # Three finished months plus an in-progress August.  The dashboard can therefore
        # demonstrate both closed-month reconciliation and partial-month treatment.
        months=[date(2026, 5, 1), date(2026, 6, 1), date(2026, 7, 1), date(2026, 8, 1)],
    )


def _period(month: date, day: int = 15) -> tuple[datetime, datetime]:
    """A one-day charge period in a deterministic calendar-shaped mock month."""
    start = datetime(month.year, month.month, day, tzinfo=UTC)
    return start, start + timedelta(days=1)


def _cost(
    entity: DemoEntity,
    month: date,
    amount: Decimal,
    *,
    provider: str,
    service: str,
    category: ServiceCategory,
    connector: str,
    compute_class: ComputeClass = ComputeClass.NOT_APPLICABLE,
    subcategory: str | None = None,
    day: int = 15,
    resource_id: str | None = None,
    resource_name: str | None = None,
    resource_type: str | None = None,
    sku_id: str | None = None,
    extra_tags: dict[str, str] | None = None,
    pricing_category: PricingCategory = PricingCategory.STANDARD,
    charge_category: ChargeCategory = ChargeCategory.USAGE,
) -> FocusRecord:
    start, end = _period(month, day)
    teams = {
        "avery.chen@northstar.example": "Data Platform",
        "jordan.patel@northstar.example": "Analytics",
        "morgan.reyes@northstar.example": "ML Platform",
        "priya.shah@northstar.example": "Data Engineering",
        "noah.williams@northstar.example": "Finance",
        "elena.garcia@northstar.example": "Product Analytics",
    }
    tags = {
        "project": entity.project,
        "owner": str(entity.owner_email),
        "team": teams[str(entity.owner_email)],
    }
    if extra_tags:
        tags.update(extra_tags)
    return FocusRecord(
        provider_name=provider,
        billing_account_id="northstar-production",
        billing_account_name="Northstar Production",
        sub_account_id="main-workspace" if provider == "Databricks" else "123456789012",
        billing_period_start=month,
        billing_period_end=date(month.year, month.month + 1, 1)
        if month.month < 12
        else date(month.year + 1, 1, 1),
        charge_period_start=start,
        charge_period_end=end,
        billed_cost=amount,
        effective_cost=amount,
        list_cost=amount * Decimal("1.12"),
        contracted_cost=amount,
        charge_category=charge_category,
        service_category=category,
        service_name=service,
        sku_id=sku_id if sku_id is not None else f"demo-{service.lower().replace(' ', '-')}",
        region_id="us-east-1",
        pricing_category=pricing_category,
        resource_id=resource_id if resource_id is not None else entity.id,
        resource_name=resource_name if resource_name is not None else entity.name,
        resource_type=(
            resource_type
            if resource_type is not None
            else (
                "warehouse"
                if provider == "Snowflake"
                else ("cluster" if "redshift" in entity.id else "workspace-resource")
            )
        ),
        consumed_quantity=float(amount),
        consumed_unit=(
            "DBU" if provider == "Databricks" else ("Credits" if provider == "Snowflake" else "Hrs")
        ),
        tags=tags,
        x_compute_class=compute_class,
        x_source_connector=connector,
        x_cost_subcategory=subcategory,
    )


def _monthly_cost(
    entity: DemoEntity,
    month: date,
    amount: Decimal,
    *,
    provider: str,
    service: str,
    category: ServiceCategory,
    connector: str,
    compute_class: ComputeClass = ComputeClass.NOT_APPLICABLE,
    subcategory: str | None = None,
    resource_id: str | None = None,
    resource_name: str | None = None,
    resource_type: str | None = None,
    sku_id: str | None = None,
    extra_tags: dict[str, str] | None = None,
    pricing_category: PricingCategory = PricingCategory.STANDARD,
    charge_category: ChargeCategory = ChargeCategory.USAGE,
    days: int | None = None,
) -> list[FocusRecord]:
    """Spread a monthly total over every day in the mock billing month.

    The dashboard's daily charts and date pickers should operate on the same shape as a
    real bill.  Keep the monthly total exact by placing the remainder on the final day,
    rather than letting decimal division introduce a rounding discrepancy.
    """
    charge_days = days if days is not None else monthrange(month.year, month.month)[1]
    if not 1 <= charge_days <= monthrange(month.year, month.month)[1]:
        raise ValueError(f"invalid mock charge-day count for {month}: {charge_days}")
    # Use a deterministic workload-shaped daily profile instead of an even division.
    # Allocate integer cents with a largest-remainder pass: independently rounding
    # each day can otherwise make tiny resource/SKU allocations finish with a negative
    # last day. A negative usage line is a credit in GOLD, which incorrectly raises the
    # visible gross cost above the month's intended allocation.
    seed = sum(ord(char) for char in f"{entity.id}:{service}:{sku_id or ''}") % 17
    weights = [
        70 + ((day * 11 + seed * 7) % 53) + (42 if (day + seed) % 13 == 0 else 0)
        for day in range(1, charge_days + 1)
    ]
    cents = int((amount * 100).to_integral_value())
    weight_total = sum(weights)
    daily_cents = [cents * weight // weight_total for weight in weights]
    remaining_cents = cents - sum(daily_cents)
    remainders = sorted(
        range(charge_days),
        key=lambda day: (cents * weights[day] % weight_total, -day),
        reverse=True,
    )
    for day in remainders[:remaining_cents]:
        daily_cents[day] += 1
    daily_amounts = [Decimal(value) / 100 for value in daily_cents]
    records = [
        _cost(
            entity,
            month,
            daily_amounts[day - 1],
            provider=provider,
            service=service,
            category=category,
            connector=connector,
            compute_class=compute_class,
            subcategory=subcategory,
            day=day,
            resource_id=resource_id,
            resource_name=resource_name,
            resource_type=resource_type,
            sku_id=sku_id,
            extra_tags=extra_tags,
            pricing_category=pricing_category,
            charge_category=charge_category,
        )
        for day in range(1, charge_days)
    ]
    records.append(
        _cost(
            entity,
            month,
            daily_amounts[-1],
            provider=provider,
            service=service,
            category=category,
            connector=connector,
            compute_class=compute_class,
            subcategory=subcategory,
            day=charge_days,
            resource_id=resource_id,
            resource_name=resource_name,
            resource_type=resource_type,
            sku_id=sku_id,
            extra_tags=extra_tags,
            pricing_category=pricing_category,
            charge_category=charge_category,
        )
    )
    return records


def _mock_charge_days(month: date) -> int:
    """Number of mocked charge days: August is intentionally still accruing."""
    return 9 if month == date(2026, 8, 1) else monthrange(month.year, month.month)[1]


def _databricks_allocation(index: int, month: date) -> dict[str, Decimal]:
    """One explicit, additive Databricks cost model for a mock month.

    The demo must teach reconciliation, not merely contain plausible-looking line
    items.  Every Databricks total therefore has a stable composition that the Home
    page, the provider page, and the backing-cost tabs can all explain:

    * 76.1% Databricks vendor usage, split across the production SKU long tail;
    * 16.8% AWS EC2 backing its classic clusters;
    * 7.1% AWS S3 backing its managed storage.
    """
    full_month_total = (Decimal(32400), Decimal(38500), Decimal(44200), Decimal(48300))[index]
    full_days = Decimal(monthrange(month.year, month.month)[1])
    total = (full_month_total * Decimal(_mock_charge_days(month)) / full_days).quantize(
        Decimal("0.01")
    )
    # Allocate to cents before creating daily FOCUS records. The final component
    # receives the remainder, so these three figures always reconcile exactly to
    # the all-in total (including August's partial month).
    dbus = (total * Decimal("0.761")).quantize(Decimal("0.01"))
    backing_compute = (total * Decimal("0.168")).quantize(Decimal("0.01"))
    return {
        "total": total,
        "dbus": dbus,
        "backing_compute": backing_compute,
        "backing_storage": total - dbus - backing_compute,
    }


def _databricks_sku_allocation(dbus: Decimal) -> list[tuple[DemoSku, Decimal]]:
    """Split direct Databricks spend by the observed production SKU mix."""
    weight_total = sum((sku.weight for sku in _DATABRICKS_SKUS), Decimal("0"))
    remaining = dbus
    rows: list[tuple[DemoSku, Decimal]] = []
    for sku in _DATABRICKS_SKUS[:-1]:
        amount = (dbus * sku.weight / weight_total).quantize(Decimal("0.01"))
        rows.append((sku, amount))
        remaining -= amount
    rows.append((_DATABRICKS_SKUS[-1], remaining))
    return rows


def _redshift_allocation(index: int, month: date) -> dict[str, Decimal]:
    """One explicit, additive Redshift cost model for a mock month.

    Redshift's dashboard starts from its AWS service total and drills into FOCUS cost
    subcategories.  This follows the production mix: compute 62.1%, managed storage
    23.6%, Spectrum scans 10.6%, and concurrency scaling 3.68% (with the small
    rounding remainder represented as other Redshift usage).
    """
    full_month_total = (Decimal(42200), Decimal(50300), Decimal(58600), Decimal(63200))[index]
    full_days = Decimal(monthrange(month.year, month.month)[1])
    total = (full_month_total * Decimal(_mock_charge_days(month)) / full_days).quantize(
        Decimal("0.01")
    )
    weights = {
        "compute": Decimal("0.621"),
        "storage": Decimal("0.236"),
        "spectrum_scan": Decimal("0.106"),
        "concurrency_scaling": Decimal("0.0368"),
        "other": Decimal("0.0002"),
    }
    remaining = total
    allocation: dict[str, Decimal] = {"total": total}
    for name, weight in tuple(weights.items())[:-1]:
        allocation[name] = (total * weight).quantize(Decimal("0.01"))
        remaining -= allocation[name]
    allocation["other"] = remaining
    return allocation


def _records(
    data: DemoScenario,
) -> tuple[
    list[FocusRecord],
    list[EfficiencyRecord],
    list[DriverHealthRecord],
    list[ComputeInstanceRecord],
    list[AiUsageRecord],
    list[StorageLocationRecord],
    list[RedshiftPolicyConfigRecord],
]:
    costs: list[FocusRecord] = []
    efficiency: list[EfficiencyRecord] = []
    health: list[DriverHealthRecord] = []
    instances: list[ComputeInstanceRecord] = []
    usage: list[AiUsageRecord] = []
    policies: list[RedshiftPolicyConfigRecord] = []
    dbx_entities = [*data.databricks_clusters, *data.endpoints]

    def split(amount: Decimal, count: int) -> list[Decimal]:
        rows = [(amount / count).quantize(Decimal("0.01")) for _ in range(count - 1)]
        return [*rows, amount - sum(rows, Decimal("0"))]

    def weighted_split(amount: Decimal, weights: tuple[Decimal, ...]) -> list[Decimal]:
        total_weight = sum(weights, Decimal("0"))
        rows = [
            (amount * weight / total_weight).quantize(Decimal("0.01")) for weight in weights[:-1]
        ]
        return [*rows, amount - sum(rows, Decimal("0"))]

    for index, month in enumerate(data.months):
        days = _mock_charge_days(month)
        databricks, redshift = (
            _databricks_allocation(index, month),
            _redshift_allocation(index, month),
        )
        # Snowflake spend for By Provider comes from ACCOUNT_USAGE synthetic Parquet
        # (``generate_snowflake_dashboard_demo``), not FOCUS BRONZE — keep all Snowflake
        # cost services there. Do not emit a thin FOCUS snowflake.monthly_bill here.
        for cluster_index, entity in enumerate(data.redshift_clusters):
            amount = Decimal("0")
            for subcategory in (
                "compute",
                "storage",
                "spectrum_scan",
                "concurrency_scaling",
                "other",
            ):
                component = split(redshift[subcategory], len(data.redshift_clusters))[cluster_index]
                amount += component
                costs.extend(
                    _monthly_cost(
                        entity,
                        month,
                        component,
                        provider="AWS",
                        service="Amazon Redshift",
                        category=ServiceCategory.DATABASES,
                        connector=SAMPLE_CONNECTOR,
                        subcategory=subcategory,
                        sku_id=f"REDSHIFT_{subcategory.upper()}",
                        resource_id=(
                            "arn:aws:redshift:us-east-1:123456789012:cluster:"
                            f"{entity.id.removeprefix('redshift-')}"
                        ),
                        resource_type="cluster",
                        days=days,
                    )
                )
            efficiency.append(
                EfficiencyRecord(
                    provider_name="AWS",
                    charge_month=month,
                    entity_type=EntityType.SQL_WAREHOUSE,
                    # Redshift's cluster identifier in both the AWS ARN and telemetry
                    # is the bare identifier, not the sample's internal `redshift-`
                    # label. Keeping this join key identical prevents the dashboard
                    # from presenting an uninstrumented duplicate tab.
                    entity_id=entity.id.removeprefix("redshift-"),
                    entity_name=entity.name,
                    owner_user=str(entity.owner_email),
                    owner_project=entity.project,
                    billed_cost=amount,
                    native_quantity=float(amount / 10),
                    native_unit="node-hours",
                    utilization_pct=22.0 + cluster_index * 51,
                    activity_count=1_600 + index * 180 + cluster_index * 460,
                    cause_detail={
                        "compute_cost": float(redshift["compute"] / len(data.redshift_clusters)),
                        "concurrency_scaling_cost": float(
                            redshift["concurrency_scaling"] / len(data.redshift_clusters)
                        ),
                        "storage_cost": float(redshift["storage"] / len(data.redshift_clusters)),
                        "spectrum_scan_cost": float(
                            redshift["spectrum_scan"] / len(data.redshift_clusters)
                        ),
                        "on_demand_node_hours": 980 + index * 45 + cluster_index * 160,
                        "reserved_node_hours": 220 + cluster_index * 110,
                        "disk_spill_query_count": 75 + index * 12 + cluster_index * 20,
                        "wlm_queue_wait_ms_p95": 6_400 + cluster_index * 1_800,
                        "wlm_queue_wait_ms_p99": 14_000 + cluster_index * 2_000,
                        "cluster_cpu_utilization_avg_pct": 21 + cluster_index * 52,
                        "cluster_cpu_utilization_max_pct": 46 + cluster_index * 47,
                        "cluster_disk_space_used_avg_pct": 48 + cluster_index * 19,
                        "cluster_disk_space_used_max_pct": 76 + cluster_index * 14,
                        "concurrency_scaling_active_seconds": 16_000 + index * 1_800,
                        "failed_cost": float(amount * Decimal("0.04")),
                    },
                    x_source_connector=REDSHIFT_CONNECTOR,
                )
            )
            cluster_id = entity.id.removeprefix("redshift-")
            # Query-pattern and table facts are derived telemetry: they deliberately
            # carry $0 because Redshift bills shared cluster capacity, not a statement
            # or table. They enrich drill-throughs without creating a second bill.
            for query_index in range(60):
                efficiency.append(
                    EfficiencyRecord(
                        provider_name="AWS",
                        charge_month=month,
                        entity_type=EntityType.QUERY_PATTERN,
                        entity_id=f"{cluster_id}:query-{query_index + 1:03d}",
                        entity_name=f"query-pattern-{query_index + 1:03d}",
                        owner_user=str(data.people[query_index % len(data.people)].email),
                        owner_project=entity.project,
                        activity_count=8 + query_index * 3,
                        cause_detail={
                            "run_count": 8 + query_index * 3,
                            "pct_runs_spilling": 0.55 if query_index % 3 else 0.18,
                            "avg_disk_spill_gb": 2.5 + query_index / 3,
                            "avg_skew_ratio": 2.3 if query_index % 4 else 1.2,
                            "max_skew_ratio": 4.2 + query_index / 8,
                            "sample_query_text": (
                                "SELECT customer_id, SUM(amount) FROM fact_orders GROUP BY 1"
                            ),
                        },
                        x_source_connector=REDSHIFT_CONNECTOR,
                    )
                )
            for table_index in range(120):
                spectrum_table = table_index % 5 == 0
                efficiency.append(
                    EfficiencyRecord(
                        provider_name="AWS",
                        charge_month=month,
                        entity_type=EntityType.TABLE,
                        entity_id=f"{cluster_id}:table-{table_index + 1:03d}",
                        entity_name=f"{entity.project}_fact_{table_index + 1:03d}",
                        owner_user=str(entity.owner_email),
                        owner_project=entity.project,
                        native_quantity=80.0 + table_index * 6.5,
                        native_unit="GB",
                        cause_detail={
                            "encoded": "N" if table_index % 4 == 0 else "Y",
                            "tbl_rows": 200_000 + table_index * 41_000,
                            "unsorted_pct": 28.0 if table_index % 3 == 0 else 4.0,
                            "stats_off_pct": 31.0 if table_index % 6 == 0 else 6.0,
                            "days_since_last_access": 120 if table_index % 7 == 0 else 14,
                            "spectrum_scan_count": 14 + table_index if spectrum_table else None,
                            "spectrum_scanned_gb": 90.0 + table_index * 2.0
                            if spectrum_table
                            else None,
                            "spectrum_returned_gb": 4.0 + table_index / 10
                            if spectrum_table
                            else None,
                            "spectrum_allocated_cost": float(
                                redshift["spectrum_scan"] / Decimal("48")
                            )
                            if spectrum_table
                            else None,
                        },
                        x_source_connector=REDSHIFT_CONNECTOR,
                    )
                )
            health.append(
                DriverHealthRecord(
                    provider_name="AWS",
                    charge_month=month,
                    client_driver="Amazon Redshift JDBC 2.1.0"
                    if cluster_index == 0
                    else "Amazon Redshift JDBC 2.0.0",
                    client_application="Tableau" if cluster_index == 0 else "Power BI",
                    executed_by=str(entity.owner_email),
                    query_count=480 + index * 20,
                    x_source_connector=REDSHIFT_CONNECTOR,
                )
            )
            policies.append(
                RedshiftPolicyConfigRecord(
                    snapshot_month=month,
                    cluster_id=entity.id,
                    cluster_name=entity.name,
                    encrypted=True,
                    publicly_accessible=False,
                    enhanced_vpc_routing=True,
                    automated_snapshot_retention_days=14,
                    require_ssl=True,
                    tag_count=5,
                    x_source_connector=REDSHIFT_CONNECTOR,
                )
            )

        dbx_totals: dict[str, Decimal] = {entity.id: Decimal("0") for entity in dbx_entities}
        for sku, sku_amount in _databricks_sku_allocation(databricks["dbus"]):
            targets = data.endpoints if sku.service == "MODEL_SERVING" else data.databricks_clusters
            # A deterministic, non-uniform distribution makes every workload visibly
            # different while weighted_split preserves the exact SKU/month total.
            weights = tuple(
                Decimal(10 + ((position * 17 + len(sku.service) * 5) % 41))
                for position in range(len(targets))
            )
            for entity, amount in zip(targets, weighted_split(sku_amount, weights), strict=True):
                costs.extend(
                    _monthly_cost(
                        entity,
                        month,
                        amount,
                        provider="Databricks",
                        service=sku.service,
                        category=sku.category,
                        connector=SAMPLE_CONNECTOR,
                        compute_class=sku.compute_class,
                        sku_id=sku.sku_id,
                        extra_tags={
                            "workspace": "main-workspace",
                            "compute_type": sku.service.lower(),
                        },
                        days=days,
                    )
                )
                dbx_totals[entity.id] += amount

        for entity_index, entity in enumerate(data.databricks_clusters):
            # Job-run resource ids commonly end in the same `-cluster` suffix.
            # Use the fleet position, not that suffix, so every backing EC2 node has
            # a distinct physical instance identity and cannot be deduplicated.
            instance_id = f"i-demo-{entity_index:04d}-{index}"
            instances.append(
                ComputeInstanceRecord(
                    provider_name="Databricks",
                    charge_month=month,
                    cluster_id=entity.id,
                    cluster_name=entity.name,
                    owner_user=str(entity.owner_email),
                    instance_id=instance_id,
                    is_driver=False,
                    node_type="m5d.2xlarge",
                    x_source_connector=DATABRICKS_CONNECTOR,
                )
            )
            health.append(
                DriverHealthRecord(
                    provider_name="Databricks",
                    charge_month=month,
                    client_driver="Databricks JDBC 2.6.38"
                    if entity.id != "0301-orchestration"
                    else "Databricks JDBC 2.6.31",
                    client_application="dbt Cloud",
                    executed_by=str(entity.owner_email),
                    query_count=720 + index * 45,
                    x_source_connector=DATABRICKS_CONNECTOR,
                )
            )
        backing_weights = tuple(
            Decimal(10 + ((position * 19 + 7) % 37))
            for position in range(len(data.databricks_clusters))
        )
        pricing_cycle = (
            PricingCategory.COMMITTED,
            PricingCategory.DYNAMIC,
            PricingCategory.STANDARD,
        )
        for entity_index, (entity, amount) in enumerate(
            zip(
                data.databricks_clusters,
                weighted_split(databricks["backing_compute"], backing_weights),
                strict=True,
            )
        ):
            pricing = pricing_cycle[entity_index % len(pricing_cycle)]
            instance_id = f"i-demo-{entity_index:04d}-{index}"
            costs.extend(
                _monthly_cost(
                    entity,
                    month,
                    amount,
                    provider="AWS",
                    service="Amazon Elastic Compute Cloud",
                    category=ServiceCategory.COMPUTE,
                    connector=SAMPLE_CONNECTOR,
                    resource_id=f"arn:aws:ec2:us-east-1:123456789012:instance/{instance_id}",
                    resource_name=instance_id,
                    resource_type="instance",
                    sku_id="EC2_M5D_2XLARGE",
                    pricing_category=pricing,
                    extra_tags={"pricing_model": str(pricing)},
                    days=days,
                )
            )
        bucket_weights = (
            Decimal("0.868"),
            Decimal("0.029"),
            Decimal("0.072"),
            Decimal("0.024"),
            Decimal("0.007"),
        )
        buckets = (
            "northstar-dbx-prod-metastore",
            "northstar-dbx-dev-metastore",
            "northstar-dbx-bronze",
            "northstar-dbx-silver",
            "northstar-dbx-gold",
        )
        remaining = databricks["backing_storage"]
        for entity, bucket, weight in zip(dbx_entities[:5], buckets, bucket_weights, strict=True):
            bucket_amount = (
                (databricks["backing_storage"] * weight).quantize(Decimal("0.01"))
                if bucket != buckets[-1]
                else remaining
            )
            remaining -= bucket_amount
            # Every managed bucket contains the same real S3 charge families, with a
            # storage-heavy mix and distinct SKU/charge records for the Storage tab.
            for subcategory, sku_id, amount in zip(
                ("storage", "requests", "data_transfer", "other"),
                (
                    "S3_STANDARD_STORAGE",
                    "S3_REQUESTS_AND_RETRIEVALS",
                    "S3_DATA_TRANSFER_OUT",
                    "S3_INVENTORY_AND_ANALYTICS",
                ),
                weighted_split(
                    bucket_amount,
                    (Decimal("0.82"), Decimal("0.10"), Decimal("0.05"), Decimal("0.03")),
                ),
                strict=True,
            ):
                costs.extend(
                    _monthly_cost(
                        entity,
                        month,
                        amount,
                        provider="AWS",
                        service="Amazon Simple Storage Service",
                        category=ServiceCategory.STORAGE,
                        connector=SAMPLE_CONNECTOR,
                        resource_id=f"arn:aws:s3:::{bucket}",
                        resource_name=bucket,
                        resource_type="bucket",
                        subcategory=subcategory,
                        sku_id=sku_id,
                        extra_tags={"storage_location": bucket, "charge_type": subcategory},
                        days=days,
                    )
                )

        opportunities = split(
            (databricks["total"] * Decimal("0.10")).quantize(Decimal("0.01")), len(dbx_entities)
        )
        for entity_index, (entity, opportunity) in enumerate(
            zip(dbx_entities, opportunities, strict=True)
        ):
            amount = dbx_totals[entity.id]
            entity_type = (
                EntityType.ENDPOINT
                if entity in data.endpoints
                else EntityType.SQL_WAREHOUSE
                if entity.id.startswith("sql-warehouse-")
                else EntityType.INTERACTIVE
                if entity.id.startswith("interactive-")
                or entity.id in {"0301-shared-analytics", "0301-orchestration"}
                else EntityType.JOB
            )
            policy_facts: dict[str, int | str] = {}
            if entity_type == EntityType.INTERACTIVE:
                policy_facts = {
                    "auto_termination_minutes": 10 + (entity_index % 7) * 15,
                    "min_autoscale_workers": 1 + entity_index % 3,
                    "max_autoscale_workers": 2 + entity_index % 10,
                    "tag_count": entity_index % 5,
                }
                if entity_index % 3:
                    policy_facts["policy_id"] = f"interactive-guardrails-{entity_index % 8 + 1}"
            elif entity_type == EntityType.SQL_WAREHOUSE:
                policy_facts = {
                    "tag_count": 1 + entity_index % 6,
                    "auto_stop_minutes": 5 + entity_index % 7 * 10,
                }
            elif entity_type == EntityType.ENDPOINT:
                policy_facts = {"tag_count": 4 if entity_index % 2 else 0}
            telemetry: dict[str, int | float | str | bool] = {
                "failed_cost": float(opportunity),
                "scale_to_zero_enabled": entity not in data.endpoints[:2],
            }
            if entity_type == EntityType.JOB:
                telemetry.update(
                    {
                        "pct_runs_underutilized": 0.85 if entity_index % 4 == 0 else 0.42,
                        "photon": entity_index % 5 == 0,
                        "max_cpu_pct": 63.0 + entity_index % 34,
                        "max_mem_pct": 58.0 + entity_index % 37,
                        "pct_time_high_cpu_wait": 0.18 if entity_index % 3 == 0 else 0.04,
                        "pct_time_high_mem_swap": 0.15 if entity_index % 5 == 0 else 0.03,
                        "min_local_disk_free_bytes": 7_000_000_000
                        if entity_index % 7 == 0
                        else 42_000_000_000,
                        "network_bytes": 780_000_000_000
                        if entity_index % 4 == 0
                        else 180_000_000_000,
                        "avg_run_seconds": 480 + entity_index * 9,
                        "worker_node_type": "m5d.4xlarge" if entity_index % 3 else "r5d.8xlarge",
                    }
                )
            elif entity_type == EntityType.INTERACTIVE:
                telemetry.update(
                    {
                        "photon": entity_index % 4 == 0,
                        "worker_node_type": "m5d.4xlarge" if entity_index % 3 else "r5d.8xlarge",
                        "core_count": 16 if entity_index % 3 else 32,
                        "availability": "ON_DEMAND" if entity_index % 2 else "SPOT_WITH_FALLBACK",
                        "job_shaped_cost": float(amount * Decimal("0.35"))
                        if entity_index % 3 == 0
                        else 0.0,
                        "jobs_priced_cost": float(amount * Decimal("0.19"))
                        if entity_index % 3 == 0
                        else 0.0,
                        "top_job_name": f"scheduled-refresh-{entity_index:03d}",
                        "top_job_owner": str(entity.owner_email),
                    }
                )
            elif entity_type == EntityType.SQL_WAREHOUSE:
                telemetry.update(
                    {
                        "warehouse_type": "SERVERLESS" if entity_index % 2 else "PRO",
                        "cache_hit_pct": 2.0 if entity_index % 2 else 38.0,
                        "query_count": 1_400 + entity_index * 90,
                        "spill_query_count": 45 + entity_index * 4,
                        "spilled_bytes": 180_000_000_000 + entity_index * 8_000_000_000,
                    }
                )
            elif entity_type == EntityType.ENDPOINT:
                telemetry.update(
                    {
                        "request_count": 260 + entity_index * 35,
                        "workload_type": "GPU_MEDIUM" if entity_index % 2 == 0 else "CPU",
                    }
                )
            efficiency.append(
                EfficiencyRecord(
                    provider_name="Databricks",
                    charge_month=month,
                    entity_type=entity_type,
                    entity_id=entity.id,
                    entity_name=entity.name,
                    owner_user=str(entity.owner_email),
                    owner_project=entity.project,
                    billed_cost=amount,
                    native_quantity=float(amount / 2),
                    native_unit="DBU",
                    utilization_pct=90.0
                    if entity in data.endpoints
                    else float(35 + (entity_index * 11 + index * 7) % 61),
                    activity_count=90 + entity_index * 13 + index * 75,
                    cause_detail={
                        **telemetry,
                        **policy_facts,
                    },
                    x_source_connector=DATABRICKS_CONNECTOR,
                )
            )
            if entity_type == EntityType.SQL_WAREHOUSE:
                # Per-user allocations are visibility-only. Their $0 billed cost avoids
                # double charging the warehouse while making concentration and cadence
                # drill-throughs useful on every warehouse.
                for user_index in range(18):
                    person = data.people[user_index % len(data.people)]
                    efficiency.append(
                        EfficiencyRecord(
                            provider_name="Databricks",
                            charge_month=month,
                            entity_type=EntityType.SQL_WAREHOUSE_USER,
                            entity_id=f"{entity.id}:user-{user_index + 1:02d}",
                            entity_name=f"{entity.name} / {person.name}",
                            owner_user=str(person.email),
                            owner_project=entity.project,
                            activity_count=25 + user_index * 18,
                            cause_detail={
                                "warehouse_type": "SERVERLESS" if entity_index % 2 else "PRO",
                                "duration_share_pct": 52.0 if user_index == 0 else 48.0 / 17,
                                "query_count": 140 + user_index * 21,
                                "avg_interval_minutes": 20 + user_index % 5 * 15,
                            },
                            x_source_connector=DATABRICKS_CONNECTOR,
                        )
                    )
            if entity_type == EntityType.JOB and entity_index % 2 == 0:
                for notebook_index in range(2):
                    efficiency.append(
                        EfficiencyRecord(
                            provider_name="Databricks",
                            charge_month=month,
                            entity_type=EntityType.NOTEBOOK,
                            entity_id=f"{entity.id}:notebook-{notebook_index + 1}",
                            entity_name=f"{entity.name} notebook {notebook_index + 1}",
                            owner_user=str(entity.owner_email),
                            owner_project=entity.project,
                            activity_count=12 + notebook_index * 7,
                            cause_detail={"jobs_priced_cost": 0.0},
                            x_source_connector=DATABRICKS_CONNECTOR,
                        )
                    )
        for endpoint_index, endpoint in enumerate(data.endpoints):
            # One canonical owning requester per endpoint keeps allocated token cost
            # exactly equal to the endpoint's FOCUS charge, while endpoints themselves
            # retain visibly different users, models, token volumes, and request shapes.
            person = data.people[endpoint_index]
            token_scale = 2 + endpoint_index * 4 + index
            usage.append(
                AiUsageRecord(
                    provider_name="Databricks",
                    charge_month=month,
                    endpoint_id=endpoint.id,
                    endpoint_name=endpoint.name,
                    served_entity_id=f"model-{endpoint.id}",
                    model_name=f"northstar-{endpoint.name}-model",
                    model_version=str(2 + endpoint_index % 2),
                    model_kind="CUSTOM_MODEL",
                    serving_mode="pay_per_token",
                    requester=str(person.email),
                    usage_context_project=endpoint.project,
                    scale_to_zero_enabled=endpoint not in data.endpoints[:2],
                    workload_size="Small" if endpoint_index < 3 else "Medium",
                    workload_type="CPU" if endpoint_index < 4 else "GPU_SMALL",
                    request_count=420 * token_scale,
                    error_request_count=7 * (endpoint_index + 1),
                    input_tokens=120_000 * token_scale,
                    output_tokens=26_000 * token_scale,
                    error_input_tokens=900 * (endpoint_index + 1),
                    error_output_tokens=180 * (endpoint_index + 1),
                    total_duration_ms=3_000_000 * token_scale,
                    x_source_connector=DATABRICKS_CONNECTOR,
                )
            )

    storage = [
        StorageLocationRecord(
            provider_name="Databricks",
            snapshot_month=data.months[-1],
            location_kind="metastore_root" if index < 2 else "catalog",
            location_name=bucket.removeprefix("northstar-dbx-"),
            url=f"s3://{bucket}",
            scheme="s3",
            cloud_provider_name="AWS",
            bucket_name=bucket,
            key_prefix=None,
            credential_name="northstar-data-role",
            x_source_connector=DATABRICKS_CONNECTOR,
        )
        for index, bucket in enumerate(
            (
                "northstar-dbx-prod-metastore",
                "northstar-dbx-dev-metastore",
                "northstar-dbx-bronze",
                "northstar-dbx-silver",
                "northstar-dbx-gold",
            )
        )
    ]
    return costs, efficiency, health, instances, usage, storage, policies


def _assert_reconciled(costs: list[FocusRecord], efficiency: list[EfficiencyRecord]) -> None:
    """Ensure entity telemetry cannot disagree with the FOCUS drill-down amount."""
    focus_totals: dict[tuple[str, str, date], Decimal] = {}
    for record in costs:
        key = (
            record.provider_name,
            record.resource_id or "",
            record.charge_period_start.date().replace(day=1),
        )
        focus_totals[key] = focus_totals.get(key, Decimal("0")) + record.effective_cost
        if record.provider_name == "AWS" and ":cluster:" in (record.resource_id or ""):
            cluster_id = (record.resource_id or "").rsplit(":cluster:", 1)[1]
            # Redshift telemetry uses the physical bare cluster id; the sample's
            # human-readable demo entity retains a `redshift-` prefix.
            focus_totals[
                (record.provider_name, cluster_id, record.charge_period_start.date().replace(day=1))
            ] = (
                focus_totals.get(
                    (
                        record.provider_name,
                        cluster_id,
                        record.charge_period_start.date().replace(day=1),
                    ),
                    Decimal("0"),
                )
                + record.effective_cost
            )
            legacy_key = (
                record.provider_name,
                f"redshift-{cluster_id}",
                record.charge_period_start.date().replace(day=1),
            )
            focus_totals[legacy_key] = (
                focus_totals.get(legacy_key, Decimal("0")) + record.effective_cost
            )
    for efficiency_record in efficiency:
        # Derived telemetry (query patterns, tables, notebook/user attribution) is
        # intentionally unbilled. It supplies drill-through evidence only and must
        # never be forced into a made-up FOCUS resource allocation.
        if efficiency_record.billed_cost == 0:
            continue
        key = (
            efficiency_record.provider_name,
            efficiency_record.entity_id,
            efficiency_record.charge_month,
        )
        if key not in focus_totals:
            raise ValueError(f"telemetry entity has no FOCUS cost: {key}")
        if focus_totals[key] != efficiency_record.billed_cost:
            raise ValueError(
                f"telemetry cost does not reconcile for {key}: "
                f"{efficiency_record.billed_cost} != {focus_totals[key]}"
            )


def _audit_gold_contract() -> None:
    """Fail sample generation when the published GOLD contract changes underneath it.

    The catalog is the public GOLD schema.  Reading it at runtime means a newly
    declared view or dimension is detected without maintaining a second list in
    the demo generator.  A new *business* metric still needs an explicit source
    mapping in ``_records``; column names alone cannot say what data is truthful.
    """
    from flashlight.transform.catalog import current_catalog

    failures: list[str] = []
    for view in current_catalog():
        path = paths.gold_dir() / view.relpath
        if not path.exists():
            failures.append(f"missing view {view.name}")
            continue
        columns = set(pq.read_schema(path).names)
        missing = (set(view.dimensions) | set(view.measures)) - columns
        if missing:
            failures.append(f"{view.name} missing {sorted(missing)}")
    if failures:
        raise ValueError("demo GOLD contract audit failed: " + "; ".join(failures))


def _audit_demo_accounting() -> None:
    """Prove every visible mock drill-through has one additive parent.

    This is deliberately executed by ``flashlight sample``, not just by a test. A
    future change to a GOLD view must fail generation if it makes a Home, provider,
    service, resource, cluster, storage, or compute figure disagree with the mock's
    declared percentage model.
    """
    from flashlight.lake import duck

    con = duck.connect()
    duck.register_gold(con)

    def scalar(sql: str) -> Decimal:
        row = con.execute(sql).fetchone()
        if row is None:
            raise ValueError(f"demo accounting query returned no row: {sql}")
        value = row[0]
        return Decimal(str(value or 0))

    def equal(label: str, actual: Decimal, expected: Decimal) -> None:
        # GOLD is materialized through DuckDB's numeric interchange; retain cent-scale
        # integrity while tolerating a binary floating-point representation artefact.
        if abs(actual - expected) > Decimal("0.001"):
            raise ValueError(f"demo accounting mismatch for {label}: {actual} != {expected}")

    try:
        for index, month in enumerate(scenario().months):
            stamp = month.isoformat()
            databricks = _databricks_allocation(index, month)
            redshift = _redshift_allocation(index, month)

            dbx_bill = scalar(
                f"SELECT gross_cost FROM databricks.monthly_bill WHERE charge_month = '{stamp}'"
            )
            dbx_service = scalar(
                "SELECT sum(gross_cost) FROM databricks.spend_by_service_month "
                f"WHERE charge_month = '{stamp}'"
            )
            dbx_resource = scalar(
                "SELECT sum(gross_cost) FROM databricks.resource_month "
                f"WHERE charge_month = '{stamp}'"
            )
            dbx_skus = scalar(
                "SELECT sum(gross_cost) FROM databricks.spend_by_sku_month "
                f"WHERE charge_month = '{stamp}'"
            )
            storage = scalar(
                "SELECT sum(gross_cost) FROM storage.backing_storage_month "
                f"WHERE charge_month = '{stamp}' AND mapping = 'databricks'"
            )
            compute = scalar(
                "SELECT sum(gross_cost) FROM compute.backing_compute_month "
                f"WHERE charge_month = '{stamp}' AND mapping = 'databricks'"
            )
            equal("Databricks DBU bill → services", dbx_service, dbx_bill)
            equal("Databricks services → resources", dbx_resource, dbx_bill)
            equal("Databricks services → SKUs", dbx_skus, dbx_bill)
            equal("Databricks DBUs", dbx_bill, databricks["dbus"])
            equal("Databricks backing compute share", compute, databricks["backing_compute"])
            equal("Databricks backing storage share", storage, databricks["backing_storage"])
            equal("Databricks all-in Home total", dbx_bill + compute + storage, databricks["total"])

            aws_bill = scalar(
                f"SELECT gross_cost FROM aws.monthly_bill WHERE charge_month = '{stamp}'"
            )
            aws_service = scalar(
                "SELECT sum(gross_cost) FROM aws.spend_by_service_month "
                f"WHERE charge_month = '{stamp}'"
            )
            aws_resource = scalar(
                f"SELECT sum(gross_cost) FROM aws.resource_month WHERE charge_month = '{stamp}'"
            )
            aws_clusters = scalar(
                "SELECT sum(gross_cost) FROM aws.redshift_cluster_cost_month "
                f"WHERE charge_month = '{stamp}'"
            )
            equal("Redshift bill → service", aws_service, aws_bill)
            equal("Redshift service → resources", aws_resource, aws_bill)
            equal("Redshift resources → clusters", aws_clusters, aws_bill)
            equal("Redshift total", aws_bill, redshift["total"])
            for subcategory, amount in redshift.items():
                if subcategory == "total":
                    continue
                component = scalar(
                    "SELECT sum(net_cost) FROM aws.spend_by_cost_subcategory_month "
                    f"WHERE charge_month = '{stamp}' AND cost_subcategory = '{subcategory}'"
                )
                equal(f"Redshift {subcategory} share", component, amount)

            # Efficiency is an evidence plane, not another bill. The richer sample
            # deliberately gives one entity several supporting recommendations, so
            # summing every rule would double count. The dashboard instead shows the
            # best-priced action per entity; prove that conservative roll-up never
            # claims more than the entity's own billed cost.
            for provider in ("Databricks", "AWS"):
                over_billed = scalar(
                    "SELECT count(*) FROM ("
                    "SELECT entity_id, max(recoverable_cost) AS potential, "
                    "max(billed_cost) AS billed FROM efficiency.waste_record "
                    f"WHERE provider_name = '{provider}' AND charge_month = '{stamp}' "
                    "GROUP BY entity_id"
                    # Derived query/table evidence can honestly carry no direct
                    # billed amount; it is not a second provider bill. Only compare
                    # an opportunity to the billed entity when that entity has one.
                    ") WHERE billed > 0 AND potential > billed + 0.001"
                )
                equal(f"{provider} best efficiency action ≤ billed cost", over_billed, Decimal("0"))
    finally:
        con.close()


def cleanup() -> None:
    """Remove data produced by this generator, then rebuild GOLD."""
    for connector in (SAMPLE_CONNECTOR, REDSHIFT_CONNECTOR, DATABRICKS_CONNECTOR):
        bronze.purge_connector(connector)
        for run in paths.runs_dir().glob(f"*-{connector}.parquet"):
            run.unlink()
    # Telemetry roots are shared with real ingest, so cleanup may remove only this
    # scenario's provider/month partitions; never delete a whole telemetry plane.
    for root, providers, key in (
        (paths.metrics_dir(), ("AWS", "Databricks"), "charge_month"),
        (paths.bronze_driver_health_dir(), ("AWS",), "charge_month"),
        (paths.compute_instances_dir(), ("Databricks",), "charge_month"),
        (paths.ai_usage_dir(), ("Databricks",), "charge_month"),
        (paths.storage_locations_dir(), ("Databricks",), "snapshot_month"),
        (paths.redshift_policy_config_dir(), ("AWS",), "snapshot_month"),
    ):
        for provider in providers:
            for sample_month in scenario().months:
                target = root / f"provider_name={provider}" / f"{key}={sample_month:%Y-%m}"
                if target.exists():
                    shutil.rmtree(target)
    published = build_gold()
    typer.echo(f"Cleaned generated demo data → rebuilt {published} GOLD views.")


def load_sample(*, force: bool = False) -> None:
    """Generate and publish the deterministic cross-cloud FOCUS demo."""
    del force  # Generation is deterministic and always replaces its sample window.
    data = scenario()
    costs, efficiency, health, instances, usage, storage, policies = _records(data)
    _assert_reconciled(costs, efficiency)
    latest_month = data.months[-1]
    window = IngestWindow(
        start=data.months[0],
        end=latest_month.replace(day=_mock_charge_days(latest_month)),
    )
    paths.ensure_layout()
    run_id = bronze.new_run_id()
    started = datetime.now(UTC)
    written = bronze.write_window(SAMPLE_CONNECTOR, window, costs, ingest_run_id=run_id)
    metrics.write_efficiency(window, efficiency)
    driver_health.write_driver_health(window, health)
    compute_instances.write_compute_instances(window, instances)
    ai_usage.write_ai_usage(window, usage)
    storage_locations.write_storage_locations(storage)
    redshift_policy_config.write(window, policies)
    runlog.record_run(
        run_id=run_id,
        connector=SAMPLE_CONNECTOR,
        status="success",
        rows=written,
        started_at=started,
        finished_at=datetime.now(UTC),
    )
    published = build_gold()
    _audit_gold_contract()
    _audit_demo_accounting()
    typer.echo(
        f"Generated {written} reconciled FOCUS records plus Redshift and Databricks "
        f"data → {published} GOLD views."
    )
