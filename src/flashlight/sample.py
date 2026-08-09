"""Deterministic, schema-driven data for ``flashlight sample``.

The sample is a coherent organization, not a downloaded CSV.  FOCUS is the
authoritative cost plane; Redshift and Databricks records add entity metadata
and telemetry so every dashboard drill-down uses the normal read path.
"""

from __future__ import annotations

import shutil
from calendar import monthrange
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

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
        Decimal("13966"),
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
            else ("cluster" if "redshift" in entity.id else "workspace-resource")
        ),
        consumed_quantity=float(amount),
        consumed_unit="DBU" if provider == "Databricks" else "Hrs",
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
    # This creates weekday/weekend variation and occasional batch spikes while keeping
    # the resource × SKU monthly total exact to the cent.
    seed = sum(ord(char) for char in f"{entity.id}:{service}:{sku_id or ''}") % 17
    weights = [
        Decimal(70 + ((day * 11 + seed * 7) % 53) + (42 if (day + seed) % 13 == 0 else 0))
        for day in range(1, charge_days + 1)
    ]
    weight_total = sum(weights, Decimal("0"))
    daily_amounts = [
        (amount * weight / weight_total).quantize(Decimal("0.01")) for weight in weights[:-1]
    ]
    daily_amounts.append(amount - sum(daily_amounts, Decimal("0")))
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
    return {
        "total": total,
        "dbus": total * Decimal("0.761"),
        "backing_compute": total * Decimal("0.168"),
        "backing_storage": total * Decimal("0.071"),
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
                    entity_id=entity.id,
                    entity_name=entity.name,
                    owner_user=str(entity.owner_email),
                    owner_project=entity.project,
                    billed_cost=amount,
                    native_quantity=float(amount / 10),
                    native_unit="node-hours",
                    utilization_pct=81.0 + cluster_index * 8,
                    activity_count=320 + index * 25,
                    cause_detail={"failed_cost": float(amount * Decimal("0.10"))},
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
            weights = (
                (
                    Decimal("0.31"),
                    Decimal("0.22"),
                    Decimal("0.18"),
                    Decimal("0.17"),
                    Decimal("0.12"),
                )
                if sku.service != "MODEL_SERVING"
                else (
                    Decimal("0.34"),
                    Decimal("0.23"),
                    Decimal("0.18"),
                    Decimal("0.15"),
                    Decimal("0.10"),
                )
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

        for entity in data.databricks_clusters:
            instance_id = f"i-demo-{entity.id[-8:]}-{index}"
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
        for entity, amount, pricing in zip(
            data.databricks_clusters,
            weighted_split(
                databricks["backing_compute"],
                (
                    Decimal("0.34"),
                    Decimal("0.24"),
                    Decimal("0.19"),
                    Decimal("0.14"),
                    Decimal("0.09"),
                ),
            ),
            (
                PricingCategory.COMMITTED,
                PricingCategory.DYNAMIC,
                PricingCategory.STANDARD,
                PricingCategory.COMMITTED,
                PricingCategory.DYNAMIC,
            ),
            strict=True,
        ):
            instance_id = f"i-demo-{entity.id[-8:]}-{index}"
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
            (databricks["total"] * Decimal("0.10")).quantize(Decimal("0.01")),
            len(dbx_entities),
        )
        entity_types = (
            EntityType.JOB,
            EntityType.JOB,
            EntityType.INTERACTIVE,
            EntityType.SQL_WAREHOUSE,
            EntityType.INTERACTIVE,
        )
        for entity_index, (entity, opportunity) in enumerate(
            zip(dbx_entities, opportunities, strict=True)
        ):
            amount = dbx_totals[entity.id]
            entity_type = (
                EntityType.ENDPOINT if entity in data.endpoints else entity_types[entity_index]
            )
            policy_facts: dict[str, int | str] = {}
            if entity_type == EntityType.INTERACTIVE:
                policy_facts = {
                    "auto_termination_minutes": 45 if entity_index == 2 else 120,
                    "min_autoscale_workers": 2,
                    "max_autoscale_workers": 8 if entity_index == 2 else 2,
                    "tag_count": 5 if entity_index == 2 else 0,
                }
                if entity_index == 2:
                    policy_facts["policy_id"] = "shared-analytics-guardrails"
            elif entity_type == EntityType.SQL_WAREHOUSE:
                policy_facts = {"tag_count": 4, "auto_stop_minutes": 20}
            elif entity_type == EntityType.ENDPOINT:
                policy_facts = {"tag_count": 4 if entity_index % 2 else 0}
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
                    else 76.0 + (len(entity.id) % 15),
                    activity_count=600 + index * 75,
                    cause_detail={
                        "failed_cost": float(opportunity),
                        "scale_to_zero_enabled": entity not in data.endpoints[:2],
                        **policy_facts,
                    },
                    x_source_connector=DATABRICKS_CONNECTOR,
                )
            )
        requester_weights = (Decimal("0.46"), Decimal("0.31"), Decimal("0.23"))
        for endpoint_index, endpoint in enumerate(data.endpoints):
            for requester_index, requester_weight in enumerate(requester_weights):
                person = data.people[(endpoint_index + requester_index) % len(data.people)]
                token_scale = 1 + endpoint_index * 3 + requester_index * 2 + index
                usage.append(
                    AiUsageRecord(
                        provider_name="Databricks",
                        charge_month=month,
                        endpoint_id=endpoint.id,
                        endpoint_name=endpoint.name,
                        served_entity_id=f"model-{endpoint.id}",
                        model_name=f"northstar-{endpoint.name}-model",
                        model_version="3",
                        model_kind="CUSTOM_MODEL",
                        serving_mode="pay_per_token",
                        requester=str(person.email),
                        usage_context_project=endpoint.project,
                        scale_to_zero_enabled=endpoint not in data.endpoints[:2],
                        workload_size="Small",
                        workload_type="CPU",
                        request_count=420 * token_scale,
                        error_request_count=7 * (requester_index + 1),
                        input_tokens=int(120_000 * token_scale * float(requester_weight)),
                        output_tokens=int(26_000 * token_scale * float(requester_weight)),
                        error_input_tokens=900 * (requester_index + 1),
                        error_output_tokens=180 * (requester_index + 1),
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
            legacy_key = (
                record.provider_name,
                f"redshift-{cluster_id}",
                record.charge_period_start.date().replace(day=1),
            )
            focus_totals[legacy_key] = (
                focus_totals.get(legacy_key, Decimal("0")) + record.effective_cost
            )
    for efficiency_record in efficiency:
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

            # Utilization/efficiency is another drill-through, not another bill. The
            # mocked retry opportunities intentionally add to 10% of each provider's
            # all-in cost, while the source bill itself remains unchanged.
            dbx_opportunity = scalar(
                "SELECT sum(recoverable_cost) FROM efficiency.waste_record "
                f"WHERE provider_name = 'Databricks' AND charge_month = '{stamp}'"
            )
            aws_opportunity = scalar(
                "SELECT sum(recoverable_cost) FROM efficiency.waste_record "
                f"WHERE provider_name = 'AWS' AND charge_month = '{stamp}'"
            )
            equal(
                "Databricks efficiency opportunity",
                dbx_opportunity,
                (databricks["total"] * Decimal("0.10")).quantize(Decimal("0.01")),
            )
            equal(
                "Redshift efficiency opportunity",
                aws_opportunity,
                (redshift["total"] * Decimal("0.10")).quantize(Decimal("0.01")),
            )
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
    """Generate and publish the deterministic Redshift + Databricks + FOCUS demo."""
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
        f"Generated {written} reconciled FOCUS records plus Redshift and "
        f"Databricks telemetry → {published} GOLD views."
    )
    typer.echo("Next: flashlight dashboard serve   # http://127.0.0.1:8501")
