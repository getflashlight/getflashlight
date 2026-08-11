"""Deterministic, schema-driven data for ``flashlight sample``.

The sample is a coherent organization, not a downloaded CSV.  FOCUS is the
authoritative cost plane; Redshift and Databricks records add entity metadata
and telemetry so every dashboard drill-down uses the normal read path.
"""

from __future__ import annotations

import hashlib
import random
import shutil
import uuid
from calendar import monthrange
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import typer
from pydantic import BaseModel, Field, field_validator, model_validator

from flashlight.core.logging import get_logger
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
# Generated demo data belongs in the lake, not beside the installed Python package.
# The latter is usually read-only in a container and does not contain repository-only
# files such as ``snowflake/synthetic_data``.
_SNOWFLAKE_SYNTHETIC_DIR = paths.home() / "sample_data" / "snowflake"


def generate_snowflake_dashboard_demo() -> None:
    """Generate synthetic ACCOUNT_USAGE and install it under FLASHLIGHT_HOME.

    ``fl sample`` installs the Parquet into ``account_usage_dir()`` so the dashboard
    reads the same lake layout as a live ingest. Its intermediate files are generated
    under ``FLASHLIGHT_HOME``, keeping installed packages and container images
    read-only.
    """
    from flashlight.lake import account_usage, paths

    _SNOWFLAKE_SYNTHETIC_DIR.mkdir(parents=True, exist_ok=True)
    _generate_snowflake_synthetic_data()
    paths.ensure_layout()
    # Demo window ends 2026-08-08 — stamp partitions under that month.
    account_usage.install_flat_parquets(
        _SNOWFLAKE_SYNTHETIC_DIR,
        charge_month="2026-08",
    )


def cleanup_snowflake_dashboard_demo() -> None:
    """Remove Snowflake demo Parquet and lake ACCOUNT_USAGE, never the generator.

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


# Snowflake ACCOUNT_USAGE synthetic-data generator. It lives in this module so
# the CLI works from an ordinary wheel without repository-relative source files.

OUTPUT_DIR = _SNOWFLAKE_SYNTHETIC_DIR
SEED = 42
random.seed(SEED)
np.random.seed(SEED)
logger = get_logger("flashlight.sample.snowflake")


def _log_written(dataset: str, rows: int, **extra: object) -> None:
    """Emit one structlog line per dataset — same shape as bronze/gold sample logs."""
    logger.info("snowflake_synthetic_written", dataset=dataset, rows=rows, **extra)


# --- Account parameters ---
ACCOUNT = "ACME_ANALYTICS"
START_DATE = date(2026, 1, 1)
END_DATE = date(2026, 8, 8)  # matches the validated reference window
DAYS = (END_DATE - START_DATE).days + 1  # 220 days
CREDIT_PRICE = 4.00  # $/credit
# Hard ceilings: monthly TCO ≤ $50K, annual ≤ $600K. Credit budget is sized under the
# reference $4.032M / 84K-credit profile so realized TCO (WH + metering + storage)
# stays inside those caps.
MONTHLY_COST_CAP = 50_000
ANNUAL_COST_CAP = 600_000
ANNUAL_COST_TARGET = 576_000  # $48K/mo × 12 — headroom under the $50K / $600K caps
MONTHLY_CREDITS = ANNUAL_COST_TARGET / (12 * CREDIT_PRICE)  # 12,000
# Prior reference demo was 84K credits/mo ($4.032M/yr). Absolute waste/storage sizes
# scale with this ratio so every cost service is retained, only scaled.
_COST_SCALE = MONTHLY_CREDITS / 84_000
# Note: actual generated credits vary due to hourly traffic patterns;
# the dashboard shows the realized credits, not the target.
# Hidden Waste KPI target: share of last-30-day spend (validated ~33% at full
# _COST_SCALE; dialed to 23% for a more plausible actionable-savings headline).
HIDDEN_WASTE_PCT_TARGET = 0.23

# --- Warehouse definitions ---
# (name, size, credits_per_hour, workload_type, pct_of_total)
# AI workloads (ml + ai) ~20% of warehouse spend; + 5% serverless = ~25-30% total
WAREHOUSES = [
    ("ETL_PROD", "X-Large", 16, "etl", 0.20),
    ("DBT_PROD", "Large", 8, "etl", 0.12),
    ("BI_REPORTS", "Medium", 4, "bi", 0.10),
    ("LOOKER", "Medium", 4, "bi", 0.07),
    ("ANALYTICS", "Large", 8, "analytics", 0.08),
    ("DATA_SCIENCE", "Large", 8, "data_science", 0.06),
    ("ML_TRAINING", "2X-Large", 32, "ml", 0.08),
    ("CORTEX_AI", "Large", 8, "ai", 0.07),
    ("CORTEX_SEARCH", "Medium", 4, "ai", 0.03),
    ("CORTEX_AGENTS", "Medium", 4, "ai", 0.02),
    ("STREAMING", "Medium", 4, "streaming", 0.04),
    ("FINANCE", "Small", 2, "bi", 0.03),
    ("MARKETING", "Medium", 4, "bi", 0.03),
    ("AIRFLOW", "Medium", 4, "etl", 0.02),
    ("DEV", "Small", 2, "dev", 0.03),
    ("ADHOC", "Small", 2, "dev", 0.02),
]

# --- User profiles: role-based warehouse access + usage patterns ---
# pattern: "good" = efficient, "medium" = normal, "bad" = wasteful (drives hidden waste)
USER_PROFILES = {
    "ETL_SERVICE": {"warehouses": ["ETL_PROD", "DBT_PROD"], "pattern": "good", "type": "compute"},
    "DBT_RUNNER": {"warehouses": ["DBT_PROD", "ETL_PROD"], "pattern": "good", "type": "compute"},
    "LOOKER_SVC": {"warehouses": ["LOOKER", "BI_REPORTS"], "pattern": "good", "type": "compute"},
    "ANALYST_JANE": {
        "warehouses": ["ANALYTICS", "BI_REPORTS", "FINANCE"],
        "pattern": "medium",
        "type": "compute",
    },
    "ANALYST_BOB": {
        "warehouses": ["ANALYTICS", "BI_REPORTS"],
        "pattern": "good",
        "type": "compute",
    },
    "DS_TEAM": {"warehouses": ["DATA_SCIENCE", "ML_TRAINING"], "pattern": "medium", "type": "ai"},
    "ML_PIPELINE": {"warehouses": ["ML_TRAINING", "CORTEX_AI"], "pattern": "good", "type": "ai"},
    "CORTEX_SVC": {
        "warehouses": ["CORTEX_AI", "CORTEX_SEARCH", "CORTEX_AGENTS"],
        "pattern": "medium",
        "type": "ai",
    },
    "AIRFLOW_SVC": {
        "warehouses": ["ETL_PROD", "DBT_PROD", "AIRFLOW"],
        "pattern": "good",
        "type": "compute",
    },
    "FINANCE_RPT": {
        "warehouses": ["FINANCE", "BI_REPORTS"],
        "pattern": "medium",
        "type": "compute",
    },
    "MARKETING_USER": {"warehouses": ["MARKETING", "ADHOC"], "pattern": "bad", "type": "compute"},
    "DEV_ALICE": {
        "warehouses": ["DEV", "ADHOC", "DATA_SCIENCE"],
        "pattern": "bad",
        "type": "compute",
    },
    "DEV_CHARLIE": {"warehouses": ["DEV", "ADHOC", "CORTEX_AI"], "pattern": "bad", "type": "ai"},
    "STREAMING_SVC": {"warehouses": ["STREAMING"], "pattern": "good", "type": "compute"},
    "ADHOC_USER": {
        "warehouses": ["ADHOC", "DEV", "ANALYTICS"],
        "pattern": "bad",
        "type": "compute",
    },
}

# Build reverse lookup: warehouse -> list of users who can access it
_WH_USERS: dict[str, list[str]] = {}
for _user, _prof in USER_PROFILES.items():
    for _wh in _prof["warehouses"]:
        _WH_USERS.setdefault(_wh, []).append(_user)


def _hours_in_day() -> int:
    return 24


def generate_warehouse_metering_history() -> pd.DataFrame:
    """Credit consumption per warehouse per hour for 30 days."""
    rows = []
    for day_offset in range(DAYS):
        usage_date = START_DATE + timedelta(days=day_offset)
        is_weekday = usage_date.weekday() < 5
        # Monthly growth: ~1.5%/month increase (≈$700/month at ~$48K base)
        months_elapsed = day_offset / 30.0
        growth_factor = 1.0 + (months_elapsed * 0.015)
        for wh_name, size, cph, wtype, pct in WAREHOUSES:
            daily_budget = (MONTHLY_CREDITS * pct) / 30  # per-month budget / 30 days
            for hour in range(24):
                # Traffic pattern: peak 8-18 for BI/analytics, flat for ETL/streaming
                if wtype in ("etl", "streaming"):
                    hour_weight = 1.0 if 2 <= hour <= 8 else 0.3
                elif wtype in ("bi", "analytics"):
                    hour_weight = 1.5 if (9 <= hour <= 17 and is_weekday) else 0.2
                elif wtype in ("ml", "ai", "data_science"):
                    hour_weight = 1.2 if 6 <= hour <= 22 else 0.5
                else:  # dev, adhoc
                    hour_weight = 1.0 if (10 <= hour <= 16 and is_weekday) else 0.1

                credits = (
                    (daily_budget / 24) * hour_weight * growth_factor * np.random.uniform(0.7, 1.3)
                )
                credits = max(0, credits)
                cloud_credits = credits * 0.1 * np.random.uniform(0.8, 1.2)

                rows.append(
                    {
                        "start_time": datetime(
                            usage_date.year, usage_date.month, usage_date.day, hour
                        ),
                        "end_time": datetime(
                            usage_date.year, usage_date.month, usage_date.day, hour, 59, 59
                        ),
                        "warehouse_name": wh_name,
                        "warehouse_id": hash(wh_name) % 10000 + 1,
                        "credits_used": round(credits, 4),
                        "credits_used_compute": round(credits * 0.9, 4),
                        "credits_used_cloud_services": round(cloud_credits, 4),
                    }
                )
    df = pd.DataFrame(rows)
    pq.write_table(pa.Table.from_pandas(df), OUTPUT_DIR / "warehouse_metering_history.parquet")
    _log_written(
        "warehouse_metering_history",
        len(df),
        credits=int(round(float(df["credits_used"].sum()))),
    )
    return df


def generate_warehouse_load_history() -> pd.DataFrame:
    """Warehouse load metrics per 5-min interval, summarized to hourly for demo."""
    rows = []
    for day_offset in range(DAYS):
        usage_date = START_DATE + timedelta(days=day_offset)
        is_weekday = usage_date.weekday() < 5
        for wh_name, size, cph, wtype, pct in WAREHOUSES:
            for hour in range(24):
                if wtype in ("etl", "streaming"):
                    avg_running = (
                        np.random.uniform(0.4, 0.9)
                        if 2 <= hour <= 8
                        else np.random.uniform(0.02, 0.15)
                    )
                elif wtype in ("bi", "analytics"):
                    avg_running = (
                        np.random.uniform(0.3, 0.7)
                        if (9 <= hour <= 17 and is_weekday)
                        else np.random.uniform(0.01, 0.1)
                    )
                elif wtype in ("ml", "ai"):
                    avg_running = (
                        np.random.uniform(0.5, 0.95)
                        if 6 <= hour <= 22
                        else np.random.uniform(0.1, 0.3)
                    )
                else:
                    avg_running = (
                        np.random.uniform(0.05, 0.25)
                        if is_weekday
                        else np.random.uniform(0.0, 0.05)
                    )

                # Some warehouses get queue pressure
                avg_queued = 0.0
                if wh_name in ("ETL_PROD", "ML_TRAINING", "BI_REPORTS") and avg_running > 0.7:
                    avg_queued = np.random.uniform(0.0, 0.3)

                rows.append(
                    {
                        "start_time": datetime(
                            usage_date.year, usage_date.month, usage_date.day, hour
                        ),
                        "end_time": datetime(
                            usage_date.year, usage_date.month, usage_date.day, hour, 59, 59
                        ),
                        "warehouse_name": wh_name,
                        "avg_running": round(avg_running, 3),
                        "avg_queued_load": round(avg_queued, 3),
                        "avg_queued_provisioning": round(np.random.uniform(0, 0.02), 3),
                        "avg_blocked": round(np.random.uniform(0, 0.01), 3),
                    }
                )
    df = pd.DataFrame(rows)
    pq.write_table(pa.Table.from_pandas(df), OUTPUT_DIR / "warehouse_load_history.parquet")
    _log_written("warehouse_load_history", len(df))
    return df


def generate_query_history() -> pd.DataFrame:
    """~200K queries over 30 days with realistic role-based patterns."""
    roles_by_type = {
        "compute": ["ETL_ROLE", "ANALYST_ROLE", "SYSADMIN"],
        "ai": ["DATA_SCIENCE_ROLE", "ML_ROLE", "AI_ROLE"],
        "etl": ["ETL_ROLE", "SYSADMIN"],
        "bi": ["ANALYST_ROLE", "SYSADMIN"],
        "analytics": ["ANALYST_ROLE", "DATA_SCIENCE_ROLE"],
        "ml": ["ML_ROLE", "DATA_SCIENCE_ROLE"],
        "data_science": ["DATA_SCIENCE_ROLE", "ML_ROLE"],
        "streaming": ["ETL_ROLE", "SYSADMIN"],
        "dev": ["PUBLIC", "ANALYST_ROLE", "SYSADMIN"],
    }

    # Pre-generate query hashes (patterns)
    n_patterns = 800
    patterns = [hashlib.md5(f"pattern_{i}".encode()).hexdigest()[:32] for i in range(n_patterns)]

    rows = []
    queries_per_day = 7000  # ~210K over 30 days

    for day_offset in range(DAYS):
        usage_date = START_DATE + timedelta(days=day_offset)
        is_weekday = usage_date.weekday() < 5
        day_queries = int(queries_per_day * (1.2 if is_weekday else 0.6))

        for _ in range(day_queries):
            wh_idx = random.choices(range(len(WAREHOUSES)), weights=[w[4] for w in WAREHOUSES])[0]
            wh_name = WAREHOUSES[wh_idx][0]
            wtype = WAREHOUSES[wh_idx][3]

            hour = random.choices(
                range(24), weights=[max(0.1, 1.0 if 8 <= h <= 18 else 0.3) for h in range(24)]
            )[0]

            # Query characteristics based on workload type
            if wtype == "etl":
                qt = random.choice(["INSERT", "CREATE_TABLE_AS_SELECT", "MERGE", "COPY"])
                elapsed = int(np.random.lognormal(10, 1.5))  # ms
                scanned = int(np.random.lognormal(28, 2))
                spill_local = int(np.random.lognormal(24, 3)) if random.random() < 0.15 else 0
                spill_remote = int(np.random.lognormal(26, 2)) if random.random() < 0.05 else 0
                cache_pct = np.random.uniform(0, 40)
            elif wtype in ("bi", "analytics"):
                qt = "SELECT"
                elapsed = int(np.random.lognormal(8, 1.2))
                scanned = int(np.random.lognormal(25, 2.5))
                spill_local = int(np.random.lognormal(22, 2)) if random.random() < 0.08 else 0
                spill_remote = 0
                cache_pct = np.random.uniform(30, 90)
            elif wtype in ("ml", "ai", "data_science"):
                qt = random.choice(["SELECT", "CALL", "INSERT"])
                elapsed = int(np.random.lognormal(11, 1.8))
                scanned = int(np.random.lognormal(29, 2))
                spill_local = int(np.random.lognormal(26, 2)) if random.random() < 0.25 else 0
                spill_remote = int(np.random.lognormal(28, 1.5)) if random.random() < 0.10 else 0
                cache_pct = np.random.uniform(5, 50)
            else:  # dev/adhoc
                qt = random.choice(["SELECT", "INSERT", "SELECT", "SELECT"])
                elapsed = int(np.random.lognormal(7, 1.5))
                scanned = int(np.random.lognormal(22, 3))
                spill_local = 0
                spill_remote = 0
                cache_pct = np.random.uniform(40, 95)

            query_id = str(uuid.uuid4())
            # Select user based on warehouse access
            user_name = random.choice(_WH_USERS.get(wh_name, list(USER_PROFILES.keys())))
            user_pattern = USER_PROFILES.get(user_name, {}).get("pattern", "medium")

            # "bad" users degrade query characteristics
            if user_pattern == "bad":
                elapsed = int(elapsed * random.uniform(1.5, 3.0))
                cache_pct = max(0, cache_pct * 0.4)
                spill_local = (
                    int(spill_local * 2.5) if spill_local else int(np.random.lognormal(24, 2))
                )
                spill_remote = (
                    int(spill_remote * 2.0)
                    if spill_remote
                    else (int(np.random.lognormal(26, 1.5)) if random.random() < 0.15 else 0)
                )
            elif user_pattern == "good":
                elapsed = int(elapsed * random.uniform(0.5, 0.8))
                cache_pct = min(100, cache_pct * 1.3)
                spill_local = 0
                spill_remote = 0

            start_time = datetime(
                usage_date.year,
                usage_date.month,
                usage_date.day,
                hour,
                random.randint(0, 59),
                random.randint(0, 59),
            )
            compilation_time = int(np.random.lognormal(6, 1))
            queued_time = int(np.random.exponential(500)) if random.random() < 0.1 else 0

            rows.append(
                {
                    "query_id": query_id,
                    "query_hash": random.choice(patterns),
                    "query_parameterized_hash": random.choice(patterns[:400]),
                    "query_text": f"/* {wtype} workload */ SELECT ... FROM ...",
                    "query_type": qt,
                    "query_tag": random.choice(
                        [f"team:{wtype}", f"pipeline:{wtype}_daily", "", "cortex_inference"]
                    )
                    if wtype == "ai"
                    else random.choice([f"team:{wtype}", f"pipeline:{wtype}", ""]),
                    "database_name": random.choice(
                        ["RAW", "ANALYTICS", "ML_FEATURES", "REPORTING", "STAGING"]
                    ),
                    "schema_name": random.choice(["PUBLIC", "CORE", "MARTS", "STAGING"]),
                    "warehouse_name": wh_name,
                    "warehouse_size": WAREHOUSES[wh_idx][1],
                    "user_name": user_name,
                    "role_name": random.choice(roles_by_type.get(wtype, roles_by_type["compute"])),
                    "execution_status": "SUCCESS"
                    if random.random() < 0.97
                    else random.choice(["FAIL", "INCIDENT_QUEUE_FULL"]),
                    "start_time": start_time,
                    "end_time": start_time + timedelta(milliseconds=elapsed),
                    "total_elapsed_time": elapsed,
                    "execution_time": max(0, elapsed - compilation_time - queued_time),
                    "compilation_time": compilation_time,
                    "queued_overload_time": queued_time,
                    "queued_provisioning_time": 0,
                    "queued_repair_time": 0,
                    "transaction_blocked_time": 0,
                    "bytes_scanned": max(0, scanned),
                    "bytes_written": int(scanned * 0.3) if qt != "SELECT" else 0,
                    "bytes_spilled_to_local_storage": max(0, spill_local),
                    "bytes_spilled_to_remote_storage": max(0, spill_remote),
                    "rows_produced": int(np.random.lognormal(8, 3)),
                    "percentage_scanned_from_cache": round(min(100, max(0, cache_pct)), 1),
                    "bytes_read_from_result": int(scanned * 0.5)
                    if cache_pct > 80 and random.random() < 0.3
                    else 0,
                    "partitions_scanned": int(np.random.lognormal(4, 2)),
                    "partitions_total": int(np.random.lognormal(6, 2)),
                }
            )

    df = pd.DataFrame(rows)
    pq.write_table(pa.Table.from_pandas(df), OUTPUT_DIR / "query_history.parquet")
    _log_written("query_history", len(df))
    return df


def generate_query_attribution_history(query_df: pd.DataFrame) -> pd.DataFrame:
    """Attributed credits per query — subset of successful queries."""
    successful = query_df[query_df["execution_status"] == "SUCCESS"].copy()

    # Distribute monthly credits across queries proportional to elapsed time
    total_elapsed = successful["total_elapsed_time"].sum()
    successful["credits_attributed_compute"] = (
        successful["total_elapsed_time"] / total_elapsed * MONTHLY_CREDITS
    ).round(6)
    successful["credits_used_query_acceleration"] = np.where(
        successful["bytes_scanned"] > 1e11,
        successful["credits_attributed_compute"] * np.random.uniform(0.05, 0.2, len(successful)),
        0,
    ).round(6)

    result = successful[
        [
            "query_id",
            "query_parameterized_hash",
            "warehouse_name",
            "start_time",
            "credits_attributed_compute",
            "credits_used_query_acceleration",
        ]
    ].copy()
    pq.write_table(pa.Table.from_pandas(result), OUTPUT_DIR / "query_attribution_history.parquet")
    _log_written(
        "query_attribution_history",
        len(result),
        credits=int(round(float(result["credits_attributed_compute"].sum()))),
    )
    return result


def generate_storage_usage() -> pd.DataFrame:
    """Daily account storage — scaled with spend (~10% of TCO at $23/TB)."""
    rows = []
    base_storage = 900.0 * _COST_SCALE * (1024**4)  # ~253 TB
    base_stage = 120.0 * _COST_SCALE * (1024**4)  # ~34 TB stages
    base_failsafe = 60.0 * _COST_SCALE * (1024**4)  # ~17 TB failsafe

    for day_offset in range(DAYS):
        usage_date = START_DATE + timedelta(days=day_offset)
        growth = 1 + (day_offset * 0.002)  # 0.2% daily growth
        rows.append(
            {
                "usage_date": usage_date,
                "storage_bytes": int(base_storage * growth * np.random.uniform(0.99, 1.01)),
                "stage_bytes": int(base_stage * growth * np.random.uniform(0.95, 1.05)),
                "failsafe_bytes": int(base_failsafe * np.random.uniform(0.98, 1.02)),
                "hybrid_table_storage_bytes": int(
                    20 * _COST_SCALE * (1024**4) * np.random.uniform(0.9, 1.1)
                ),
                "archive_storage_cool_bytes": int(40 * _COST_SCALE * (1024**4) * growth),
                "archive_storage_cold_bytes": int(20 * _COST_SCALE * (1024**4) * growth),
            }
        )
    df = pd.DataFrame(rows)
    pq.write_table(pa.Table.from_pandas(df), OUTPUT_DIR / "storage_usage.parquet")
    _log_written("storage_usage", len(df))
    return df


def generate_table_storage_metrics() -> pd.DataFrame:
    """Top 200 tables by storage. Realistic range: 100 MB to ~140 TB max."""
    databases = ["RAW", "ANALYTICS", "ML_FEATURES", "REPORTING", "STAGING"]
    schemas = ["PUBLIC", "CORE", "MARTS", "STAGING", "ML", "FEATURES"]
    rows = []
    # Cap scales with account size (was 500 TB at $4M)
    max_bytes = int(500 * _COST_SCALE * (1024**4))
    for i in range(200):
        # lognormal scaled so table sizes track the smaller account
        active = min(int(np.random.lognormal(25, 1.8) * _COST_SCALE), max_bytes)
        rows.append(
            {
                "table_catalog": random.choice(databases),
                "table_schema": random.choice(schemas),
                "table_name": f"TABLE_{i:04d}"
                if i > 20
                else random.choice(
                    [
                        "FACT_ORDERS",
                        "DIM_CUSTOMERS",
                        "FACT_EVENTS",
                        "ML_EMBEDDINGS",
                        "RAW_CLICKSTREAM",
                        "CORTEX_INFERENCE_LOG",
                        "FEATURE_STORE",
                        "DIM_PRODUCTS",
                        "FACT_TRANSACTIONS",
                        "RAW_API_LOGS",
                        "STAGING_IMPORTS",
                        "AGG_DAILY_METRICS",
                        "USER_SESSIONS",
                        "ML_TRAINING_DATA",
                        "CORTEX_SEARCH_INDEX",
                        "RAW_IOT_TELEMETRY",
                        "DIM_GEOGRAPHY",
                        "FACT_REVENUE",
                        "RAW_SOCIAL_FEEDS",
                        "AUDIT_LOG",
                        "VECTOR_EMBEDDINGS",
                    ]
                ),
                "active_bytes": active,
                "time_travel_bytes": int(active * np.random.uniform(0.05, 0.3)),
                "failsafe_bytes": int(active * np.random.uniform(0.02, 0.15)),
                "retained_for_clone_bytes": int(active * np.random.uniform(0, 0.1))
                if random.random() < 0.3
                else 0,
                "is_transient": random.random() < 0.15,
            }
        )
    df = pd.DataFrame(rows)
    pq.write_table(pa.Table.from_pandas(df), OUTPUT_DIR / "table_storage_metrics.parquet")
    _log_written("table_storage_metrics", len(df))
    return df


def generate_tag_references() -> pd.DataFrame:
    """Tags on warehouses + databases — 90% spend attributed via 5 required tags.

    Models a governance-mature org where:
      - ~90% of warehouse spend is fully tagged (5/5 required tags)
      - ~10% of spend is unattributed (untagged or partially tagged)
      - Small quality issues exist in some tag values
      - Databases have moderate coverage
    Required tags: department, environment, application, owner, cost_center
    """
    rows = []

    # Tag taxonomy
    departments = ["Engineering", "Data", "Finance", "Marketing", "Operations", "IT"]
    environments = ["prod", "staging", "dev", "sandbox"]
    applications = [
        "Analytics Platform",
        "ML Pipeline",
        "ERP Integration",
        "Customer 360",
        "Real-time Streaming",
        "BI Reporting",
    ]

    # Workload type → department mapping (realistic attribution)
    wtype_dept = {
        "etl": "Data",
        "bi": "Finance",
        "analytics": "Data",
        "data_science": "Engineering",
        "ml": "Engineering",
        "ai": "Engineering",
        "streaming": "Data",
        "dev": "Engineering",
    }
    wtype_env = {
        "etl": "prod",
        "bi": "prod",
        "analytics": "prod",
        "data_science": "staging",
        "ml": "prod",
        "ai": "prod",
        "streaming": "prod",
        "dev": "dev",
    }
    wtype_app = {
        "etl": "ERP Integration",
        "bi": "BI Reporting",
        "analytics": "Analytics Platform",
        "data_science": "ML Pipeline",
        "ml": "ML Pipeline",
        "ai": "Customer 360",
        "streaming": "Real-time Streaming",
        "dev": "Analytics Platform",
    }

    # Sort warehouses by spend percentage (descending) to control which are untagged
    # The bottom ~10% by spend will be untagged/partially tagged
    wh_sorted = sorted(WAREHOUSES, key=lambda x: x[4], reverse=True)
    cumulative_pct = 0.0

    for wh_name, _, _, wtype, pct in wh_sorted:
        cumulative_pct += pct
        if cumulative_pct <= 0.90:
            # Top 90% of spend: fully tagged with 5 required tags
            tags_to_add = ["department", "environment", "application", "owner", "cost_center"]
        elif cumulative_pct <= 0.95:
            # Next 5%: partially tagged (1-3 tags) — governance gap
            tags_to_add = random.sample(
                ["department", "environment", "application", "owner", "cost_center"],
                k=random.randint(1, 3),
            )
        else:
            # Bottom 5%: completely untagged — generates unattributed findings
            continue

        for tag_name in tags_to_add:
            # Introduce quality issues for ~10% of tags (G04 findings)
            quality_roll = random.random()
            if quality_roll < 0.04:
                tag_value = ""  # Empty value
            elif quality_roll < 0.07:
                tag_value = random.choice(["TBD", "TODO", "unknown"])  # Placeholder
            elif quality_roll < 0.10 and tag_name == "environment":
                # Case mismatch (e.g. "Prod" instead of "prod")
                tag_value = wtype_env.get(wtype, "prod").capitalize()
            else:
                # Good value
                if tag_name == "department":
                    tag_value = wtype_dept.get(wtype, random.choice(departments))
                elif tag_name == "environment":
                    tag_value = wtype_env.get(wtype, "prod")
                elif tag_name == "application":
                    tag_value = wtype_app.get(wtype, random.choice(applications))
                elif tag_name == "owner":
                    tag_value = f"{wtype}_team"
                else:  # cost_center
                    tag_value = f"CC_{wtype.upper()}"

            rows.append(
                {
                    "object_name": wh_name,
                    "domain": "WAREHOUSE",
                    "tag_name": tag_name,
                    "tag_value": tag_value,
                }
            )

    # Database-level tags (lower coverage than warehouses — common in real orgs)
    databases = [
        "ANALYTICS",
        "ML_FEATURES",
        "REPORTING",
        "RAW_EVENTS",
        "STAGING",
        "SANDBOX",
        "PRODUCTION",
    ]
    for db_name in databases:
        if random.random() < 0.45:  # Only 45% of databases tagged at all
            for tag_name in random.sample(
                ["department", "environment", "owner"], k=random.randint(1, 3)
            ):
                if tag_name == "department":
                    tag_value = random.choice(departments)
                elif tag_name == "environment":
                    tag_value = random.choice(environments)
                else:
                    tag_value = random.choice(["platform_team", "data_eng", "analytics_team"])
                rows.append(
                    {
                        "object_name": db_name,
                        "domain": "DATABASE",
                        "tag_name": tag_name,
                        "tag_value": tag_value,
                    }
                )

    df = pd.DataFrame(rows)
    pq.write_table(pa.Table.from_pandas(df), OUTPUT_DIR / "tag_references.parquet")

    # Print governance stats
    wh_names = {wh[0] for wh in WAREHOUSES}
    tagged_wh = df[df["domain"] == "WAREHOUSE"]["object_name"].nunique()
    fully_tagged = df[df["domain"] == "WAREHOUSE"].groupby("object_name")["tag_name"].nunique()
    full_count = (fully_tagged >= 5).sum()
    _log_written(
        "tag_references",
        len(df),
        warehouses_tagged=f"{tagged_wh}/{len(wh_names)}",
        fully_tagged=int(full_count),
    )
    return df


def generate_warehouse_events_history() -> pd.DataFrame:
    """Warehouse suspend/resume events — some with thrashing patterns."""
    rows = []
    for day_offset in range(DAYS):
        usage_date = START_DATE + timedelta(days=day_offset)
        for wh_name, _, _, wtype, _ in WAREHOUSES:
            # DEV and ADHOC thrash (many suspend/resume cycles)
            if wtype == "dev":
                n_events = random.randint(8, 20)
            elif wtype in ("bi", "analytics"):
                n_events = random.randint(3, 8)
            else:
                n_events = random.randint(1, 4)

            for _ in range(n_events):
                hour = random.randint(0, 23)
                rows.append(
                    {
                        "timestamp": datetime(
                            usage_date.year,
                            usage_date.month,
                            usage_date.day,
                            hour,
                            random.randint(0, 59),
                        ),
                        "warehouse_name": wh_name,
                        "event_name": random.choice(["RESUME_WAREHOUSE", "SUSPEND_WAREHOUSE"]),
                        "event_reason": random.choice(
                            ["SUSPEND_IDLE", "RESUME_QUERY", "RESUME_USER"]
                        ),
                        "event_state": random.choice(["STARTED", "COMPLETED"]),
                        "cluster_number": 1,
                    }
                )
    df = pd.DataFrame(rows)
    pq.write_table(pa.Table.from_pandas(df), OUTPUT_DIR / "warehouse_events_history.parquet")
    _log_written("warehouse_events_history", len(df))
    return df


def generate_metering_history() -> pd.DataFrame:
    """All serverless service metering — AI + managed services (pipes, tasks, clustering, etc.)."""
    # AI services (~10% of monthly credits)
    ai_monthly_credits = MONTHLY_CREDITS * 0.10
    ai_services = [
        ("CORTEX_AI_FUNCTIONS", "CORTEX_AI", 0.35),
        ("CORTEX_SEARCH", "CORTEX_SEARCH_SVC", 0.20),
        ("AI_SERVICES", "ML_TRAINING_SVC", 0.15),
        ("CORTEX_ANALYST", "ANALYST_SVC", 0.10),
        ("DOCUMENT_AI", "DOC_AI_SVC", 0.08),
        ("SNOWFLAKE_INTELLIGENCE", "INTELLIGENCE_SVC", 0.05),
        ("CORTEX_AGENTS", "AGENT_SVC", 0.04),
        ("CORTEX_GUARDRAILS", "GUARDRAILS_SVC", 0.03),
    ]
    # Managed services (~12% of monthly credits — notable cost visible to leadership)
    managed_monthly_credits = MONTHLY_CREDITS * 0.12
    managed_services = [
        ("AUTOMATIC_CLUSTERING", "AUTO_CLUSTER_SVC", 0.22),
        ("SNOWPIPE", "PIPE_SVC", 0.18),
        ("SERVERLESS_TASK", "TASK_SVC", 0.16),
        ("REPLICATION", "REPLICATION_SVC", 0.14),
        ("DATA_TRANSFER", "EGRESS_SVC", 0.12),
        ("SEARCH_OPTIMIZATION", "SEARCH_OPT_SVC", 0.08),
        ("MATERIALIZED_VIEW", "MATVIEW_SVC", 0.05),
        ("QUERY_ACCELERATION", "QAS_SVC", 0.05),
    ]
    rows = []
    for day_offset in range(DAYS):
        usage_date = START_DATE + timedelta(days=day_offset)
        for svc_type, name, pct in ai_services:
            daily_credits = (ai_monthly_credits * pct) / 30
            credits = daily_credits * np.random.uniform(0.7, 1.3)
            rows.append(
                {
                    "start_time": datetime(usage_date.year, usage_date.month, usage_date.day),
                    "end_time": datetime(usage_date.year, usage_date.month, usage_date.day, 23, 59),
                    "service_type": svc_type,
                    "name": name,
                    "entity_type": "SERVICE",
                    "database_name": random.choice(["ANALYTICS", "ML_FEATURES", "REPORTING"]),
                    "schema_name": "PUBLIC",
                    "credits_used": round(credits, 4),
                }
            )
        for svc_type, name, pct in managed_services:
            daily_credits = (managed_monthly_credits * pct) / 30
            credits = daily_credits * np.random.uniform(0.7, 1.3)
            rows.append(
                {
                    "start_time": datetime(usage_date.year, usage_date.month, usage_date.day),
                    "end_time": datetime(usage_date.year, usage_date.month, usage_date.day, 23, 59),
                    "service_type": svc_type,
                    "name": name,
                    "entity_type": "SERVICE",
                    "database_name": random.choice(
                        ["RAW", "ANALYTICS", "REPORTING", "ML_FEATURES"]
                    ),
                    "schema_name": "PUBLIC",
                    "credits_used": round(credits, 4),
                }
            )
    df = pd.DataFrame(rows)
    pq.write_table(pa.Table.from_pandas(df), OUTPUT_DIR / "metering_history.parquet")
    total_ai = df[df["service_type"].isin([s[0] for s in ai_services])]["credits_used"].sum()
    total_managed = df[df["service_type"].isin([s[0] for s in managed_services])][
        "credits_used"
    ].sum()
    _log_written(
        "metering_history",
        len(df),
        ai_credits=int(round(float(total_ai))),
        managed_credits=int(round(float(total_managed))),
    )
    return df


def generate_cortex_ai_functions_usage() -> pd.DataFrame:
    """Cortex AI function usage — for AI02 check."""
    functions = [
        ("COMPLETE", "llama3.1-70b", 0.25),
        ("COMPLETE", "mistral-large2", 0.20),
        ("COMPLETE", "claude-3.5-sonnet", 0.15),
        ("SUMMARIZE", "llama3.1-70b", 0.10),
        ("TRANSLATE", "snowflake-arctic", 0.08),
        ("SENTIMENT", "snowflake-arctic", 0.07),
        ("CLASSIFY_TEXT", "llama3.1-8b", 0.05),
        ("EXTRACT_ANSWER", "mistral-large2", 0.05),
        ("EMBED_TEXT_768", "e5-base-v2", 0.03),
        ("EMBED_TEXT_1024", "voyage-multilingual-2", 0.02),
    ]
    ai_func_credits = MONTHLY_CREDITS * 0.30 * 0.35  # 35% of AI budget
    rows = []
    for day_offset in range(DAYS):
        usage_date = START_DATE + timedelta(days=day_offset)
        for func_name, model, pct in functions:
            daily_credits = (ai_func_credits * pct) / 30
            credits = daily_credits * np.random.uniform(0.6, 1.4)
            calls = int(credits * np.random.uniform(50, 200))
            tokens_in = int(calls * np.random.uniform(200, 2000))
            tokens_out = int(calls * np.random.uniform(50, 500))
            rows.append(
                {
                    "start_time": datetime(usage_date.year, usage_date.month, usage_date.day),
                    "function_name": func_name,
                    "model_name": model,
                    "credits": round(credits, 4),
                    "calls": calls,
                    "tokens_sent": tokens_in,
                    "tokens_received": tokens_out,
                    "user_id": random.choice(
                        ["CORTEX_SVC", "ML_PIPELINE", "ANALYST_JANE", "APP_SVC"]
                    ),
                    "query_tag": random.choice(
                        ["inference:prod", "batch:daily", "interactive", ""]
                    ),
                }
            )
    df = pd.DataFrame(rows)
    pq.write_table(
        pa.Table.from_pandas(df), OUTPUT_DIR / "cortex_ai_functions_usage_history.parquet"
    )
    _log_written("cortex_ai_functions_usage_history", len(df))
    return df


def generate_cortex_search_daily_usage() -> pd.DataFrame:
    """Cortex Search daily usage — for AI03 check."""
    services = [
        ("ANALYTICS", "SEARCH", "PRODUCT_SEARCH_SVC", 0.40),
        ("ML_FEATURES", "PUBLIC", "DOC_SEARCH_SVC", 0.35),
        ("REPORTING", "PUBLIC", "SUPPORT_SEARCH_SVC", 0.25),
    ]
    search_credits = MONTHLY_CREDITS * 0.30 * 0.20  # 20% of AI budget
    rows = []
    for day_offset in range(DAYS):
        usage_date = START_DATE + timedelta(days=day_offset)
        for db, schema, svc, pct in services:
            for ctype in ["SERVING", "EMBEDDING", "BATCH_QUERY"]:
                type_pct = {"SERVING": 0.5, "EMBEDDING": 0.35, "BATCH_QUERY": 0.15}[ctype]
                credits = (search_credits * pct * type_pct / 30) * np.random.uniform(0.7, 1.3)
                tokens = int(credits * np.random.uniform(5000, 20000))
                rows.append(
                    {
                        "usage_date": usage_date,
                        "database_name": db,
                        "schema_name": schema,
                        "service_name": svc,
                        "consumption_type": ctype,
                        "credits": round(credits, 4),
                        "tokens": tokens,
                    }
                )
    df = pd.DataFrame(rows)
    pq.write_table(
        pa.Table.from_pandas(df), OUTPUT_DIR / "cortex_search_daily_usage_history.parquet"
    )
    _log_written("cortex_search_daily_usage_history", len(df))
    return df


def generate_metering_daily_history() -> pd.DataFrame:
    """Daily metering by service type — for F01 executive trend."""
    service_types = [
        ("WAREHOUSE_METERING", 0.55),
        ("CLOUD_SERVICES", 0.06),
        ("AUTOMATIC_CLUSTERING", 0.04),
        ("SEARCH_OPTIMIZATION", 0.02),
        ("MATERIALIZED_VIEW", 0.02),
        ("SNOWPIPE", 0.03),
        ("SERVERLESS_TASK", 0.03),
        ("REPLICATION", 0.02),
        ("QUERY_ACCELERATION", 0.01),
        ("CORTEX_AI_FUNCTIONS", 0.06),
        ("CORTEX_SEARCH", 0.04),
        ("AI_SERVICES", 0.03),
        ("SNOWPARK_CONTAINER_SERVICES", 0.03),
        ("DOCUMENT_AI", 0.02),
        ("DATA_TRANSFER", 0.01),
        ("HYBRID_TABLE", 0.01),
        ("CORTEX_ANALYST", 0.01),
        ("CORTEX_AGENTS", 0.01),
    ]
    rows = []
    for day_offset in range(DAYS):
        usage_date = START_DATE + timedelta(days=day_offset)
        for svc, pct in service_types:
            daily = (MONTHLY_CREDITS * pct / 30) * np.random.uniform(0.7, 1.3)
            rows.append(
                {
                    "usage_date": usage_date,
                    "service_type": svc,
                    "credits_used": round(daily, 4),
                }
            )
    df = pd.DataFrame(rows)
    pq.write_table(pa.Table.from_pandas(df), OUTPUT_DIR / "metering_daily_history.parquet")
    _log_written("metering_daily_history", len(df))
    return df


def generate_serverless_services_data() -> None:
    """Generate Parquet for serverless services."""

    # Automatic clustering history
    tables = [
        ("ANALYTICS", "CORE", "FACT_ORDERS"),
        ("ANALYTICS", "CORE", "FACT_EVENTS"),
        ("RAW", "PUBLIC", "RAW_CLICKSTREAM"),
        ("ML_FEATURES", "FEATURES", "FEATURE_STORE"),
        ("REPORTING", "MARTS", "AGG_DAILY_METRICS"),
    ]
    rows = []
    for day_offset in range(DAYS):
        dt = START_DATE + timedelta(days=day_offset)
        for db, schema, table in tables:
            credits = np.random.uniform(0.5, 8.0)
            rows.append(
                {
                    "start_time": datetime(dt.year, dt.month, dt.day),
                    "end_time": datetime(dt.year, dt.month, dt.day, 23, 59),
                    "database_name": db,
                    "schema_name": schema,
                    "table_name": table,
                    "credits_used": round(credits, 4),
                    "num_bytes_reclustered": int(np.random.lognormal(28, 1.5)),
                    "num_rows_reclustered": int(np.random.lognormal(18, 2)),
                }
            )
    df = pd.DataFrame(rows)
    pq.write_table(pa.Table.from_pandas(df), OUTPUT_DIR / "automatic_clustering_history.parquet")
    _log_written("automatic_clustering_history", len(df))

    # Snowpipe usage
    pipes = [
        ("RAW_EVENTS_PIPE", 0.35),
        ("CLICKSTREAM_PIPE", 0.25),
        ("IOT_TELEMETRY_PIPE", 0.20),
        ("<internal_or_auto_refresh>", 0.10),
        ("API_LOGS_PIPE", 0.10),
    ]
    pipe_credits = MONTHLY_CREDITS * 0.03
    rows = []
    for day_offset in range(DAYS):
        dt = START_DATE + timedelta(days=day_offset)
        for pipe_name, pct in pipes:
            credits = (pipe_credits * pct / 30) * np.random.uniform(0.7, 1.3)
            files = int(np.random.uniform(100, 5000))
            bytes_ins = int(files * np.random.uniform(1e6, 50e6))
            rows.append(
                {
                    "start_time": datetime(dt.year, dt.month, dt.day),
                    "pipe_name": pipe_name,
                    "credits_used": round(credits, 4),
                    "bytes_inserted": bytes_ins,
                    "files_inserted": files,
                }
            )
    df = pd.DataFrame(rows)
    pq.write_table(pa.Table.from_pandas(df), OUTPUT_DIR / "pipe_usage_history.parquet")
    _log_written("pipe_usage_history", len(df))

    # Serverless tasks
    tasks = [
        ("ANALYTICS", "ORCHESTRATION", "REFRESH_DASHBOARDS", 0.25),
        ("RAW", "INGESTION", "LOAD_EXTERNAL_DATA", 0.20),
        ("ML_FEATURES", "ML", "FEATURE_PIPELINE", 0.20),
        ("ANALYTICS", "CORE", "AGGREGATE_METRICS", 0.15),
        ("REPORTING", "ALERTS", "ANOMALY_DETECTOR", 0.10),
        ("RAW", "MAINTENANCE", "CLEANUP_STAGING", 0.10),
    ]
    task_credits = MONTHLY_CREDITS * 0.03
    rows = []
    for day_offset in range(DAYS):
        dt = START_DATE + timedelta(days=day_offset)
        for db, schema, task_name, pct in tasks:
            credits = (task_credits * pct / 30) * np.random.uniform(0.6, 1.4)
            rows.append(
                {
                    "start_time": datetime(dt.year, dt.month, dt.day),
                    "database_name": db,
                    "schema_name": schema,
                    "task_name": task_name,
                    "task_id": hash(f"{db}.{schema}.{task_name}") % 100000,
                    "credits_used": round(credits, 4),
                }
            )
    df = pd.DataFrame(rows)
    pq.write_table(pa.Table.from_pandas(df), OUTPUT_DIR / "serverless_task_history.parquet")
    _log_written("serverless_task_history", len(df))

    # Data transfer
    transfers = [
        ("COPY", "AWS", "us-east-1", "AWS", "eu-west-1", 0.40),
        ("REPLICATION", "AWS", "us-east-1", "AWS", "us-west-2", 0.30),
        ("UNLOAD", "AWS", "us-east-1", "AZURE", "eastus2", 0.20),
        ("STAGE", "AWS", "us-east-1", "GCP", "us-central1", 0.10),
    ]
    rows = []
    for day_offset in range(DAYS):
        dt = START_DATE + timedelta(days=day_offset)
        for ttype, sc, sr, tc, tr, pct in transfers:
            bytes_t = int(np.random.lognormal(33, 1.5) * pct)
            rows.append(
                {
                    "start_time": datetime(dt.year, dt.month, dt.day),
                    "transfer_type": ttype,
                    "source_cloud": sc,
                    "source_region": sr,
                    "target_cloud": tc,
                    "target_region": tr,
                    "bytes_transferred": bytes_t,
                }
            )
    df = pd.DataFrame(rows)
    pq.write_table(pa.Table.from_pandas(df), OUTPUT_DIR / "data_transfer_history.parquet")
    _log_written("data_transfer_history", len(df))


def _spend_30d_usd() -> float:
    """Last-30-day credit spend (WH + serverless) — same basis as hidden_waste_summary."""
    as_of = END_DATE
    cutoff = pd.Timestamp(as_of) - pd.Timedelta(days=30)
    wh = pq.read_table(OUTPUT_DIR / "warehouse_metering_history.parquet").to_pandas()
    svc = pq.read_table(OUTPUT_DIR / "metering_history.parquet").to_pandas()
    wh_c = float(wh.loc[pd.to_datetime(wh["start_time"]) >= cutoff, "credits_used"].sum())
    svc_c = float(svc.loc[pd.to_datetime(svc["start_time"]) >= cutoff, "credits_used"].sum())
    return (wh_c + svc_c) * CREDIT_PRICE


def _scale_hidden_waste(
    compute: pd.DataFrame,
    storage: pd.DataFrame,
    ai: pd.DataFrame,
    *,
    target_pct: float = HIDDEN_WASTE_PCT_TARGET,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Scale waste $ so headline total ≈ target_pct of last-30-day spend."""
    spend_30d = max(_spend_30d_usd(), 1.0)
    compute_total = float(compute["wasted_cost_usd"].sum()) if not compute.empty else 0.0
    storage_annual = float(storage["monthly_cost_usd"].sum()) * 12 if not storage.empty else 0.0
    ai_total = float(ai["wasted_cost_usd"].sum()) if not ai.empty else 0.0
    current = compute_total + storage_annual + ai_total
    if current <= 0:
        return compute, storage, ai
    scale = (spend_30d * target_pct) / current
    compute = compute.copy()
    storage = storage.copy()
    ai = ai.copy()
    for col in ("actual_cost_usd", "wasted_credits", "wasted_cost_usd"):
        if col in compute.columns:
            compute[col] = (compute[col].astype(float) * scale).round(2)
    for col in ("actual_cost_usd", "monthly_cost_usd"):
        if col in storage.columns:
            storage[col] = (storage[col].astype(float) * scale).round(2)
    for col in ("actual_cost_usd", "wasted_credits", "wasted_cost_usd"):
        if col in ai.columns:
            ai[col] = (ai[col].astype(float) * scale).round(2)
    return compute, storage, ai


def generate_shadow_waste_data() -> None:
    """Generate shadow waste findings for Compute, Storage, and AI/Cortex."""
    # ── Compute Shadow Waste ───────────────────────────────────────────────
    rows = []
    for wh_name, size, cph, wtype, pct in WAREHOUSES:
        # Total warehouse cost for last 30 days
        total_credits = (MONTHLY_CREDITS * pct / 30) * 30 * 0.64  # avg hourly weight
        if wtype == "dev":
            idle_hours = int(np.random.uniform(200, 500) * _COST_SCALE)
            idle_credits = idle_hours * np.random.uniform(1.5, 3.0)
        elif wtype in ("bi", "analytics"):
            idle_hours = int(np.random.uniform(50, 150) * _COST_SCALE)
            idle_credits = idle_hours * np.random.uniform(2.0, 6.0)
        else:
            idle_hours = int(np.random.uniform(10, 60) * _COST_SCALE)
            idle_credits = idle_hours * np.random.uniform(3.0, 12.0)
        rows.append(
            {
                "warehouse_name": wh_name,
                "waste_type": "IDLE_RUNNING",
                "idle_hours": idle_hours,
                "actual_cost_usd": round(total_credits * CREDIT_PRICE, 2),
                "wasted_credits": round(idle_credits, 2),
                "wasted_cost_usd": round(idle_credits * CREDIT_PRICE, 2),
                "recommendation": "Reduce auto-suspend timeout or schedule",
                "size": size,
            }
        )
    for wh_name, size, cph, wtype, pct in WAREHOUSES:
        if wtype in ("dev", "bi") and random.random() < 0.5:
            total_credits = (MONTHLY_CREDITS * pct / 30) * 30 * 0.64
            over_credits = np.random.uniform(50, 300) * _COST_SCALE
            rows.append(
                {
                    "warehouse_name": wh_name,
                    "waste_type": "OVERSIZED",
                    "idle_hours": 0,
                    "actual_cost_usd": round(total_credits * CREDIT_PRICE, 2),
                    "wasted_credits": round(over_credits, 2),
                    "wasted_cost_usd": round(over_credits * CREDIT_PRICE, 2),
                    "recommendation": f"Downsize from {size} — avg util <20%",
                    "size": size,
                }
            )
    compute_df = pd.DataFrame(rows)

    # ── Storage Hidden Waste ────────────────────────────────────────────────
    rows = []
    stale = [
        "RAW.PUBLIC.OLD_IMPORT_2025",
        "STAGING.TEMP.MIGRATION_BACKUP",
        "ANALYTICS.ARCHIVE.LEGACY_REPORTS",
        "ML_FEATURES.OLD.V1_FEATURES",
        "RAW.PUBLIC.ABANDONED_POC_DATA",
        "STAGING.TEMP.ETL_DEBUG_COPY",
    ]
    for table in stale:
        tb = np.random.uniform(5, 40) * _COST_SCALE
        # Actual cost includes TT + failsafe overhead (~30% extra)
        actual_monthly = tb * 23 * 1.3
        rows.append(
            {
                "object_name": table,
                "waste_type": "STALE_TABLE",
                "size_gb": round(tb * 1024, 1),
                "days_since_access": int(np.random.uniform(90, 365)),
                "actual_cost_usd": round(actual_monthly, 2),
                "monthly_cost_usd": round(tb * 23, 2),
                "recommendation": "Archive or drop — no access in 90+ days",
            }
        )
    for table in ["ANALYTICS.CORE.FACT_ORDERS", "RAW.PUBLIC.RAW_CLICKSTREAM"]:
        tb = np.random.uniform(10, 30) * _COST_SCALE
        # Actual = full table cost; saving = just the TT excess portion (~80% of TT)
        actual_monthly = tb * 23 * 1.5  # full table with TT
        saving_monthly = tb * 23 * 0.8  # recoverable TT portion
        rows.append(
            {
                "object_name": table,
                "waste_type": "TIME_TRAVEL_EXCESS",
                "size_gb": round(tb * 1024, 1),
                "days_since_access": 0,
                "actual_cost_usd": round(actual_monthly, 2),
                "monthly_cost_usd": round(saving_monthly, 2),
                "recommendation": "Reduce retention from 90 to 7 days",
            }
        )
    for i in range(3):
        tb = np.random.uniform(8, 25) * _COST_SCALE
        actual_monthly = tb * 23
        rows.append(
            {
                "object_name": f"STAGING.CLONES.DEV_CLONE_{i + 1}",
                "waste_type": "ABANDONED_CLONE",
                "size_gb": round(tb * 1024, 1),
                "days_since_access": int(np.random.uniform(30, 180)),
                "actual_cost_usd": round(actual_monthly, 2),
                "monthly_cost_usd": round(actual_monthly, 2),
                "recommendation": "Drop abandoned clone",
            }
        )
    storage_df = pd.DataFrame(rows)

    # ── AI/Cortex Shadow Waste (6 patterns from snowflake-ai-finops) ──────
    # Credit amounts are scaled from the prior $4M demo so $ waste tracks TCO.
    def _ai_cr(credits: float) -> float:
        return credits * _COST_SCALE

    rows = []
    # P1: Over-sized models
    for func, model, task, calls, cr, alt, sav in [
        (
            "AI_COMPLETE",
            "claude-3-5-sonnet",
            "sentiment",
            850,
            _ai_cr(4680),
            "AI_SENTIMENT",
            "50-70%",
        ),
        (
            "AI_COMPLETE",
            "mistral-large2",
            "classification",
            620,
            _ai_cr(2280),
            "AI_CLASSIFY",
            "50-75%",
        ),
        ("AI_COMPLETE", "llama3.1-70b", "extraction", 430, _ai_cr(1580), "AI_EXTRACT", "30-60%"),
    ]:
        rows.append(
            {
                "waste_pattern": "OVERSIZED_MODEL",
                "function_name": func,
                "model_name": model,
                "task_type": task,
                "calls_30d": calls,
                "actual_cost_usd": round(cr * CREDIT_PRICE, 2),
                "wasted_credits": round(cr * 0.6, 2),
                "wasted_cost_usd": round(cr * 0.6 * CREDIT_PRICE, 2),
                "recommendation": f"Replace with {alt} — est. {sav} savings",
            }
        )
    # P2: Duplicate calls
    for func, cause, calls, cr in [
        ("AI_SENTIMENT", "hourly_no_incremental", 12000, _ai_cr(180)),
        ("AI_COMPLETE", "notebook_rerun", 3500, _ai_cr(420)),
        ("AI_CLASSIFY", "retry_on_success", 2800, _ai_cr(95)),
    ]:
        rows.append(
            {
                "waste_pattern": "DUPLICATE_CALLS",
                "function_name": func,
                "model_name": cause,
                "task_type": "duplicate",
                "calls_30d": calls,
                "actual_cost_usd": cr * CREDIT_PRICE * 1.8,
                "wasted_credits": float(cr),
                "wasted_cost_usd": cr * CREDIT_PRICE,
                "recommendation": "Incremental processing / cache results",
            }
        )
    # P3: Verbose prompts
    rows.append(
        {
            "waste_pattern": "VERBOSE_PROMPTS",
            "function_name": "AI_COMPLETE",
            "model_name": "llama3.1-70b",
            "task_type": "prompt_bloat",
            "calls_30d": 15000,
            "actual_cost_usd": _ai_cr(320.0) * CREDIT_PRICE * 3.0,
            "wasted_credits": _ai_cr(320.0),
            "wasted_cost_usd": _ai_cr(320.0) * CREDIT_PRICE,
            "recommendation": "Trim prompts — avg 200 tokens filler/call",
        }
    )
    # P4: Idle Cortex Search
    for svc, cr in [("ABANDONED_POC_SEARCH", _ai_cr(45)), ("OLD_DEMO_SEARCH", _ai_cr(28))]:
        rows.append(
            {
                "waste_pattern": "IDLE_SEARCH_SERVICE",
                "function_name": "CORTEX_SEARCH",
                "model_name": svc,
                "task_type": "idle_indexing",
                "calls_30d": 0,
                "actual_cost_usd": cr * CREDIT_PRICE,
                "wasted_credits": float(cr),
                "wasted_cost_usd": cr * CREDIT_PRICE,
                "recommendation": "Drop idle search service — 0 queries/30d",
            }
        )
    # P5: Agent loops
    rows.append(
        {
            "waste_pattern": "AGENT_LOOP",
            "function_name": "CORTEX_AGENT",
            "model_name": "support_agent_v2",
            "task_type": "unbounded_loop",
            "calls_30d": 85,
            "actual_cost_usd": _ai_cr(250.0) * CREDIT_PRICE * 2.0,
            "wasted_credits": _ai_cr(250.0),
            "wasted_cost_usd": _ai_cr(250.0) * CREDIT_PRICE,
            "recommendation": "Set token budget (50K) and time limit (120s)",
        }
    )
    # P6: Dev in prod
    rows.append(
        {
            "waste_pattern": "DEV_IN_PROD",
            "function_name": "AI_COMPLETE",
            "model_name": "claude-3-5-sonnet",
            "task_type": "dev_experiment",
            "calls_30d": 2200,
            "actual_cost_usd": _ai_cr(380.0) * CREDIT_PRICE,
            "wasted_credits": _ai_cr(380.0),
            "wasted_cost_usd": _ai_cr(380.0) * CREDIT_PRICE,
            "recommendation": "Revoke AI access from DEV_ROLE in prod",
        }
    )
    ai_df = pd.DataFrame(rows)

    compute_df, storage_df, ai_df = _scale_hidden_waste(compute_df, storage_df, ai_df)

    pq.write_table(pa.Table.from_pandas(compute_df), OUTPUT_DIR / "hidden_waste_compute.parquet")
    _log_written(
        "hidden_waste_compute",
        len(compute_df),
        wasted_cost_usd=int(round(float(compute_df["wasted_cost_usd"].sum()))),
    )
    pq.write_table(pa.Table.from_pandas(storage_df), OUTPUT_DIR / "hidden_waste_storage.parquet")
    _log_written(
        "hidden_waste_storage",
        len(storage_df),
        monthly_cost_usd=int(round(float(storage_df["monthly_cost_usd"].sum()))),
    )
    pq.write_table(pa.Table.from_pandas(ai_df), OUTPUT_DIR / "hidden_waste_ai.parquet")
    _log_written(
        "hidden_waste_ai",
        len(ai_df),
        wasted_cost_usd=int(round(float(ai_df["wasted_cost_usd"].sum()))),
        waste_pct_target=HIDDEN_WASTE_PCT_TARGET,
    )


def _generate_snowflake_synthetic_data() -> None:
    logger.info(
        "snowflake_synthetic_started",
        account=ACCOUNT,
        start=str(START_DATE),
        end=str(START_DATE + timedelta(days=DAYS - 1)),
        monthly_credits=round(float(MONTHLY_CREDITS), 1),
        annual_cost_target=ANNUAL_COST_TARGET,
        monthly_cost_cap=MONTHLY_COST_CAP,
        annual_cost_cap=ANNUAL_COST_CAP,
    )

    generate_warehouse_metering_history()
    generate_warehouse_load_history()
    query_df = generate_query_history()
    generate_query_attribution_history(query_df)
    generate_storage_usage()
    generate_table_storage_metrics()
    generate_tag_references()
    generate_warehouse_events_history()
    generate_metering_history()
    generate_cortex_ai_functions_usage()
    generate_cortex_search_daily_usage()
    generate_metering_daily_history()
    generate_serverless_services_data()
    generate_shadow_waste_data()

    files = len(list(OUTPUT_DIR.glob("*.parquet")))
    logger.info("snowflake_synthetic_built", files=files, path=str(OUTPUT_DIR))
