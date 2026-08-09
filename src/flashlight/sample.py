"""Deterministic, schema-driven data for ``flashlight sample``.

The sample is a coherent organization, not a downloaded CSV.  FOCUS is the
authoritative cost plane; Redshift and Databricks records add entity metadata
and telemetry so every dashboard drill-down uses the normal read path.
"""

from __future__ import annotations

import shutil
from datetime import UTC, date, datetime
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
        months=[date(2026, 4, 1), date(2026, 5, 1), date(2026, 6, 1)],
    )


def _period(month: date) -> tuple[datetime, datetime]:
    start = datetime(month.year, month.month, 15, tzinfo=UTC)
    return start, start.replace(day=16)


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
) -> FocusRecord:
    start, end = _period(month)
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
        resource_id=entity.id,
        resource_name=entity.name,
        resource_type="cluster" if "redshift" in entity.id else "workspace-resource",
        consumed_quantity=float(amount),
        consumed_unit="DBU" if provider == "Databricks" else "Hrs",
        tags=tags,
        x_compute_class=compute_class,
        x_source_connector=connector,
        x_cost_subcategory=subcategory,
    )


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
        for entity in data.redshift_clusters:
            amount = Decimal(
                1800 + index * 120 if entity.project == "analytics" else 950 + index * 75
            )
            costs.append(
                _cost(
                    entity,
                    month,
                    amount,
                    provider="AWS",
                    service="Amazon Redshift",
                    category=ServiceCategory.DATABASES,
                    connector=SAMPLE_CONNECTOR,
                    subcategory="compute",
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
                    utilization_pct=54.0 if entity.project == "analytics" else 78.0,
                    activity_count=320 + index * 25,
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
        for entity in data.databricks_clusters:
            amount = Decimal(
                1450 + index * 130 if entity.project == "analytics" else 2100 + index * 180
            )
            costs.append(
                _cost(
                    entity,
                    month,
                    amount,
                    provider="Databricks",
                    service="Databricks Jobs Compute",
                    category=ServiceCategory.ANALYTICS,
                    connector=SAMPLE_CONNECTOR,
                    compute_class=ComputeClass.CLASSIC,
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
                    utilization_pct=61.0 if entity.project == "analytics" else 43.0,
                    activity_count=180 + index * 15,
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
        endpoint = data.endpoints[0]
        endpoint_cost = Decimal(720 + index * 90)
        costs.append(
            _cost(
                endpoint,
                month,
                endpoint_cost,
                provider="Databricks",
                service="Databricks Model Serving",
                category=ServiceCategory.AI_AND_MACHINE_LEARNING,
                connector=SAMPLE_CONNECTOR,
                compute_class=ComputeClass.SERVERLESS,
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
                utilization_pct=37.0,
                activity_count=1500 + index * 200,
                cause_detail={"scale_to_zero_enabled": False},
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
            for month in ("2026-04", "2026-05", "2026-06"):
                target = root / f"provider_name={provider}" / f"{key}={month}"
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
    window = IngestWindow(start=data.months[0], end=date(2026, 6, 30))
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
    typer.echo(
        f"Generated {written} reconciled FOCUS records plus Redshift and "
        f"Databricks telemetry → {published} GOLD views."
    )
    typer.echo("Next: flashlight dashboard serve   # http://127.0.0.1:8501")
