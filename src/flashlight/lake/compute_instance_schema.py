"""Compute-instance record + its Parquet schema.

A third telemetry plane alongside driver-health and storage-locations — same
*pattern* (aggregated-at-source record → partition-replace Parquet → DuckDB view →
GOLD join), different table, because this data is pure metadata (which EC2 instance
backed which Databricks cluster) with no dollar/waste semantics — deliberately not
an ``EfficiencyRecord`` for the same reason ``StorageLocationRecord`` isn't: it would
inflate the efficiency plane's "measured" coverage denominator without being a
utilization signal.

Unlike storage locations, this is a genuine **time-bounded historical fact**, not a
present-tense snapshot: ``system.compute.node_timeline`` rows carry ``start_time``/
``end_time`` within the pull's own window, so ``charge_month`` here is a real charge
period (partition-replace by window, like ``DriverHealthRecord``) rather than a
"current state as of last sync" stamp. See ``docs/design/backing-compute.md``.
"""

from __future__ import annotations

from datetime import date

import pyarrow as pa
from pydantic import BaseModel, field_validator


class ComputeInstanceRecord(BaseModel):
    """One EC2 instance's membership in a Databricks cluster for one charge month,
    aggregated at source from ``system.compute.node_timeline``."""

    provider_name: str  # the platform owning the METADATA, e.g. Databricks (partition key)
    charge_month: date  # first of month (partition key)
    cluster_id: str
    cluster_name: str | None = None  # None when system.compute.clusters has no row for it
    owner_user: str | None = None  # system.compute.clusters.owned_by
    instance_id: str  # the cloud instance id, e.g. "i-1234a6c12a2681234"
    is_driver: bool | None = None  # None when node_timeline's `driver` flag wasn't reported
    node_type: str | None = None
    x_source_connector: str = "unknown"

    @field_validator("charge_month")
    @classmethod
    def _first_of_month(cls, v: date) -> date:
        return v.replace(day=1)


#: Non-partition columns first, then the two partition keys last — mirrors
#: lake/driver_health_schema.py::DRIVER_HEALTH_SCHEMA.
COMPUTE_INSTANCE_SCHEMA: pa.Schema = pa.schema(
    [
        ("cluster_id", pa.string()),
        ("cluster_name", pa.string()),
        ("owner_user", pa.string()),
        ("instance_id", pa.string()),
        ("is_driver", pa.bool_()),
        ("node_type", pa.string()),
        ("x_source_connector", pa.string()),
        # ── Hive partition keys (written as dirs, restored on read) ──────────
        ("provider_name", pa.string()),
        ("charge_month", pa.string()),  # YYYY-MM
    ]
)

PARTITION_COLUMNS: tuple[str, ...] = ("provider_name", "charge_month")


def charge_month_of(record: ComputeInstanceRecord) -> str:
    """Partition key for a record: the charge month, ``YYYY-MM``."""
    return record.charge_month.strftime("%Y-%m")


def record_to_row(record: ComputeInstanceRecord) -> dict[str, object]:
    """Flatten a :class:`ComputeInstanceRecord` into a compute-instance row dict."""
    return {
        "cluster_id": record.cluster_id,
        "cluster_name": record.cluster_name,
        "owner_user": record.owner_user,
        "instance_id": record.instance_id,
        "is_driver": record.is_driver,
        "node_type": record.node_type,
        "x_source_connector": record.x_source_connector,
        "provider_name": str(record.provider_name),
        "charge_month": charge_month_of(record),
    }


def build_table(records: list[ComputeInstanceRecord]) -> pa.Table:
    """Build a typed Arrow table (``COMPUTE_INSTANCE_SCHEMA``) from validated records."""
    return pa.Table.from_pylist(
        [record_to_row(r) for r in records], schema=COMPUTE_INSTANCE_SCHEMA
    )


def empty_table() -> pa.Table:
    """An empty, fully-typed compute-instance table — the no-data fallback for readers."""
    return COMPUTE_INSTANCE_SCHEMA.empty_table()
