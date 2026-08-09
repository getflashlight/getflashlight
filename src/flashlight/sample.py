"""Deterministic, schema-driven data for ``flashlight sample``.

The sample is a coherent organization, not a downloaded CSV.  FOCUS is the
authoritative cost plane; Redshift and Databricks records add entity metadata
and telemetry so every dashboard drill-down uses the normal read path.
"""

from __future__ import annotations

import shutil
from calendar import monthrange
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
        ],
        endpoints=[
            DemoEntity(
                id="endpoint-support-assistant",
                name="support-assistant",
                owner_email="morgan.reyes@northstar.example",
                project="ml-platform",
            )
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
) -> FocusRecord:
    start, end = _period(month, day)
    tags = {
        "project": entity.project,
        "owner": str(entity.owner_email),
        "team": "Data Platform"
        if entity.owner_email == "avery.chen@northstar.example"
        else (
            "Analytics" if entity.owner_email == "jordan.patel@northstar.example" else "ML Platform"
        ),
    }
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
        charge_category=ChargeCategory.USAGE,
        service_category=category,
        service_name=service,
        sku_id=f"demo-{service.lower().replace(' ', '-')}",
        region_id="us-east-1",
        pricing_category=PricingCategory.STANDARD,
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
    # Billing exports are currency amounts, so mock daily rows stay at cent precision.
    # The final remainder preserves the exact monthly amount at the resource grain.
    daily = (amount / charge_days).quantize(Decimal("0.01"))
    records = [
        _cost(
            entity,
            month,
            daily,
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
        )
        for day in range(1, charge_days)
    ]
    records.append(
        _cost(
            entity,
            month,
            amount - daily * (charge_days - 1),
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

    * 65% Databricks DBUs — 60% Jobs and 40% Model Serving;
    * 30% AWS EC2 backing its classic clusters;
    * 5% AWS S3 backing its managed storage.
    """
    full_month_total = (Decimal(32400), Decimal(38500), Decimal(44200), Decimal(48300))[index]
    full_days = Decimal(monthrange(month.year, month.month)[1])
    total = (
        full_month_total * Decimal(_mock_charge_days(month)) / full_days
    ).quantize(Decimal("0.01"))
    dbus = total * Decimal("0.65")
    jobs = dbus * Decimal("0.60")
    return {
        "total": total,
        "dbus": dbus,
        "jobs": jobs,
        "model_serving": dbus - jobs,
        "backing_compute": total * Decimal("0.30"),
        "backing_storage": total * Decimal("0.05"),
    }


def _redshift_allocation(index: int, month: date) -> dict[str, Decimal]:
    """One explicit, additive Redshift cost model for a mock month.

    Redshift's dashboard starts from its AWS service total and drills into FOCUS cost
    subcategories, so every mock month uses the same visible composition: 65% cluster
    compute, 20% managed storage, 10% concurrency scaling, and 5% Spectrum scans.
    """
    full_month_total = (Decimal(42200), Decimal(50300), Decimal(58600), Decimal(63200))[index]
    full_days = Decimal(monthrange(month.year, month.month)[1])
    total = (
        full_month_total * Decimal(_mock_charge_days(month)) / full_days
    ).quantize(Decimal("0.01"))
    return {
        "total": total,
        "compute": total * Decimal("0.65"),
        "storage": total * Decimal("0.20"),
        "concurrency_scaling": total * Decimal("0.10"),
        "spectrum": total * Decimal("0.05"),
    }


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
    for index, month in enumerate(data.months):
        charge_days = _mock_charge_days(month)
        databricks = _databricks_allocation(index, month)
        redshift = _redshift_allocation(index, month)
        for cluster_index, entity in enumerate(data.redshift_clusters):
            # Named clusters divide every Redshift subcategory 65/35, so both the
            # service and cluster drill-downs add to the same provider total.
            share = Decimal("0.65") if cluster_index == 0 else Decimal("0.35")
            amount = Decimal("0")
            for subcategory in ("compute", "storage", "concurrency_scaling", "spectrum"):
                component = redshift[subcategory] * share
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
                        days=charge_days,
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
                    utilization_pct=84.0 if entity.project == "analytics" else 88.0,
                    activity_count=320 + index * 25,
                    # The demo opportunity pool is an explicit 10% of each Redshift
                    # workload's billed cost, represented as measurable retry cost.
                    cause_detail={"failed_cost": float(amount * Decimal("0.10"))},
                    x_source_connector=REDSHIFT_CONNECTOR,
                )
            )
            health.append(
                DriverHealthRecord(
                    provider_name="AWS",
                    charge_month=month,
                    client_driver="Amazon Redshift JDBC 2.1.0",
                    client_application="Tableau",
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
                    tag_count=3,
                    x_source_connector=REDSHIFT_CONNECTOR,
                )
            )
        for cluster_index, entity in enumerate(data.databricks_clusters):
            # The two Jobs clusters add to 60% of DBUs; keeping two named workloads
            # makes the drill-down and allocation pages useful without breaking the
            # published percentage contract.
            cluster_share = Decimal("0.45") if cluster_index == 0 else Decimal("0.55")
            amount = databricks["jobs"] * cluster_share
            costs.extend(
                _monthly_cost(
                    entity,
                    month,
                    amount,
                    provider="Databricks",
                    service="Databricks Jobs Compute",
                    category=ServiceCategory.ANALYTICS,
                    connector=SAMPLE_CONNECTOR,
                    compute_class=ComputeClass.CLASSIC,
                    days=charge_days,
                )
            )
            efficiency.append(
                EfficiencyRecord(
                    provider_name="Databricks",
                    charge_month=month,
                    entity_type=EntityType.JOB,
                    entity_id=entity.id,
                    entity_name=entity.name,
                    owner_user=str(entity.owner_email),
                    owner_project=entity.project,
                    billed_cost=amount,
                    native_quantity=float(amount / 2),
                    native_unit="DBU",
                    utilization_pct=84.0 if entity.project == "analytics" else 88.0,
                    activity_count=180 + index * 15,
                    # DBU telemetry is 65% of Databricks' all-in cost.  Scale the
                    # measured opportunity so the provider's action queue is exactly
                    # 10% of its DBU + EC2 + S3 total, without inventing a second cost.
                    cause_detail={
                        "failed_cost": float(amount * Decimal("0.10") / Decimal("0.65"))
                    },
                    x_source_connector=DATABRICKS_CONNECTOR,
                )
            )
            instances.extend(
                [
                    ComputeInstanceRecord(
                        provider_name="Databricks",
                        charge_month=month,
                        cluster_id=entity.id,
                        cluster_name=entity.name,
                        owner_user=str(entity.owner_email),
                        instance_id=f"i-demo-{entity.id[-8:]}-{index}",
                        is_driver=False,
                        node_type="m5d.2xlarge",
                        x_source_connector=DATABRICKS_CONNECTOR,
                    )
                ]
            )
            health.append(
                DriverHealthRecord(
                    provider_name="Databricks",
                    charge_month=month,
                    client_driver="Databricks JDBC 2.6.38",
                    client_application="dbt Cloud",
                    executed_by=str(entity.owner_email),
                    query_count=720 + index * 45 + cluster_index * 110,
                    x_source_connector=DATABRICKS_CONNECTOR,
                )
            )
            # Classic Databricks clusters are also backed by AWS instances.  These rows
            # deliberately use the same instance ids as the metadata above so the normal
            # backing-compute GOLD mapping, Home stack, and Databricks Compute tab all
            # exercise the exact production accounting path.
            instance_id = f"i-demo-{entity.id[-8:]}-{index}"
            backing_compute = databricks["backing_compute"] * cluster_share
            costs.extend(
                _monthly_cost(
                    entity,
                    month,
                    backing_compute,
                    provider="AWS",
                    service="Amazon Elastic Compute Cloud",
                    category=ServiceCategory.COMPUTE,
                    connector=SAMPLE_CONNECTOR,
                    resource_id=f"arn:aws:ec2:us-east-1:123456789012:instance/{instance_id}",
                    resource_name=instance_id,
                    resource_type="instance",
                    days=charge_days,
                )
            )
        endpoint = data.endpoints[0]
        endpoint_cost = databricks["model_serving"]
        costs.extend(
            _monthly_cost(
                endpoint,
                month,
                endpoint_cost,
                provider="Databricks",
                service="Databricks Model Serving",
                category=ServiceCategory.AI_AND_MACHINE_LEARNING,
                connector=SAMPLE_CONNECTOR,
                compute_class=ComputeClass.SERVERLESS,
                days=charge_days,
            )
        )
        # Unity Catalog storage is AWS-billed but Databricks-managed.  The bucket root
        # matches the storage-location metadata below, which makes this mocked cost a
        # Databricks Storage row in GOLD rather than an opaque AWS S3 charge.
        costs.extend(
            _monthly_cost(
                endpoint,
                month,
                databricks["backing_storage"],
                provider="AWS",
                service="Amazon Simple Storage Service",
                category=ServiceCategory.STORAGE,
                connector=SAMPLE_CONNECTOR,
                resource_id="arn:aws:s3:::northstar-databricks-root",
                resource_name="northstar-databricks-root",
                resource_type="bucket",
                subcategory="storage",
                days=charge_days,
            )
        )
        efficiency.append(
            EfficiencyRecord(
                provider_name="Databricks",
                charge_month=month,
                entity_type=EntityType.ENDPOINT,
                entity_id=endpoint.id,
                entity_name=endpoint.name,
                owner_user=str(endpoint.owner_email),
                owner_project=endpoint.project,
                billed_cost=endpoint_cost,
                native_quantity=float(endpoint_cost),
                native_unit="DBU",
                utilization_pct=90.0,
                activity_count=1500 + index * 200,
                cause_detail={
                    "scale_to_zero_enabled": False,
                    "failed_cost": float(
                        endpoint_cost * Decimal("0.10") / Decimal("0.65")
                    ),
                },
                x_source_connector=DATABRICKS_CONNECTOR,
            )
        )
        for person in data.people[:2]:
            usage.append(
                AiUsageRecord(
                    provider_name="Databricks",
                    charge_month=month,
                    endpoint_id=endpoint.id,
                    endpoint_name=endpoint.name,
                    served_entity_id="northstar-support-model",
                    model_name="northstar-support-model",
                    model_version="3",
                    model_kind="CUSTOM_MODEL",
                    serving_mode="pay_per_token",
                    requester=str(person.email),
                    usage_context_project="analytics"
                    if person.team == "Analytics"
                    else "ml-platform",
                    scale_to_zero_enabled=False,
                    workload_size="Small",
                    workload_type="CPU",
                    request_count=700 + index * 40,
                    input_tokens=1_600_000 + index * 100_000,
                    output_tokens=320_000 + index * 20_000,
                    x_source_connector=DATABRICKS_CONNECTOR,
                )
            )
    storage = [
        StorageLocationRecord(
            provider_name="Databricks",
            snapshot_month=data.months[-1],
            location_kind="metastore_root",
            location_name="northstar-metastore",
            url="s3://northstar-databricks-root",
            scheme="s3",
            cloud_provider_name="AWS",
            bucket_name="northstar-databricks-root",
            key_prefix=None,
            credential_name="northstar-data-role",
            x_source_connector=DATABRICKS_CONNECTOR,
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
                "SELECT gross_cost FROM databricks.monthly_bill "
                f"WHERE charge_month = '{stamp}'"
            )
            dbx_service = scalar(
                "SELECT sum(gross_cost) FROM databricks.spend_by_service_month "
                f"WHERE charge_month = '{stamp}'"
            )
            dbx_resource = scalar(
                "SELECT sum(gross_cost) FROM databricks.resource_month "
                f"WHERE charge_month = '{stamp}'"
            )
            dbx_jobs = scalar(
                "SELECT sum(gross_cost) FROM databricks.spend_by_service_month "
                f"WHERE charge_month = '{stamp}' AND service_name = 'Databricks Jobs Compute'"
            )
            dbx_serving = scalar(
                "SELECT sum(gross_cost) FROM databricks.spend_by_service_month "
                f"WHERE charge_month = '{stamp}' AND service_name = 'Databricks Model Serving'"
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
            equal("Databricks DBUs", dbx_bill, databricks["dbus"])
            equal("Databricks Jobs share", dbx_jobs, databricks["jobs"])
            equal("Databricks Model Serving share", dbx_serving, databricks["model_serving"])
            equal("Databricks backing compute share", compute, databricks["backing_compute"])
            equal("Databricks backing storage share", storage, databricks["backing_storage"])
            equal("Databricks all-in Home total", dbx_bill + compute + storage, databricks["total"])

            aws_bill = scalar(
                "SELECT gross_cost FROM aws.monthly_bill " f"WHERE charge_month = '{stamp}'"
            )
            aws_service = scalar(
                "SELECT sum(gross_cost) FROM aws.spend_by_service_month "
                f"WHERE charge_month = '{stamp}'"
            )
            aws_resource = scalar(
                "SELECT sum(gross_cost) FROM aws.resource_month "
                f"WHERE charge_month = '{stamp}'"
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
