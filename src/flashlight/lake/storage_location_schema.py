"""Unity Catalog storage-location record + its Parquet schema.

A **third** telemetry plane beside the efficiency/waste and driver-health ones — same
pattern (record → partition-replace Parquet → DuckDB view → GOLD passthrough),
separate table because this is neither cost nor utilization: it's the map of *which
cloud object-storage URLs back a data platform*.

It exists because Databricks' FOCUS bill (``system.billing.usage``) covers DBU compute
only. Storage lives in customer-owned buckets billed by the cloud provider, so the
only way to show the storage behind Databricks is to label the *AWS* bill's S3 rows
with what Unity Catalog says about their buckets. This is that label — metadata, no
dollars. See ``docs/design/backing-storage.md``.

Deliberately **not** an ``EfficiencyRecord`` with a new ``EntityType``: that model is
"one entity's efficiency summary for one month" with a ``billed_cost`` that reconciles
to the FOCUS bill, and — decisively — ``gold.efficiency_entity_month`` is the coverage
*denominator* that ``efficiency_waste.coverage_caption()`` exists to keep honest.
Metadata rows would inflate the "measured" count and quietly corrupt exactly the
number that's there to prevent over-claiming.
"""

from __future__ import annotations

from datetime import date

import pyarrow as pa
from pydantic import BaseModel, field_validator


class StorageLocationRecord(BaseModel):
    """One Unity Catalog storage location — a metastore/catalog root or an external
    location — resolved to the bucket and key prefix it addresses.

    ``snapshot_month``, not ``charge_month``: Unity Catalog exposes only *current*
    state, so this is a point-in-time inventory stamped with the month it was taken,
    the same call ``databricks._fetch_table_inventory`` makes for the same reason.
    Naming it ``charge_month`` for symmetry with the other planes would imply a charge
    period it doesn't have. It is correspondingly **not** in
    ``catalog.PERIOD_DIMENSIONS`` — you can't trend along it.

    ``key_prefix is None`` means the URL addresses the **bucket root**, and that
    distinction carries the whole mapping-confidence signal downstream: the AWS bill's
    S3 ``ResourceId`` is bucket-grained, so a prefix-scoped location shares its bucket
    with whatever else lives there and its cost can only ever be an upper bound. It is
    never collapsed to an empty string.
    """

    provider_name: str  # the platform owning the METADATA, e.g. Databricks (partition key)
    snapshot_month: date  # first of month (partition key)
    location_kind: str  # metastore_root | catalog | external_location
    location_name: str  # metastore / catalog name / external-location name
    url: str  # the raw UC url, e.g. s3://bucket/prefix
    scheme: str  # s3 | abfss | gs | dbfs | other
    cloud_provider_name: str | None = None  # AWS | Microsoft | Google Cloud | None
    bucket_name: str | None = None  # None when the url isn't parseable object storage
    key_prefix: str | None = None  # None == the url IS the bucket root
    is_read_only: bool | None = None  # external locations only; NULL elsewhere
    credential_name: str | None = None
    x_source_connector: str = "unknown"

    @field_validator("snapshot_month")
    @classmethod
    def _first_of_month(cls, v: date) -> date:
        return v.replace(day=1)


#: Non-partition columns first, then the two partition keys last — mirrors
#: lake/metrics_schema.py::METRICS_SCHEMA and lake/driver_health_schema.py.
STORAGE_LOCATION_SCHEMA: pa.Schema = pa.schema(
    [
        ("location_kind", pa.string()),
        ("location_name", pa.string()),
        ("url", pa.string()),
        ("scheme", pa.string()),
        ("cloud_provider_name", pa.string()),
        ("bucket_name", pa.string()),
        ("key_prefix", pa.string()),
        ("is_read_only", pa.bool_()),
        ("credential_name", pa.string()),
        ("x_source_connector", pa.string()),
        # ── Hive partition keys (written as dirs, restored on read) ──────────
        ("provider_name", pa.string()),
        ("snapshot_month", pa.string()),  # YYYY-MM
    ]
)

PARTITION_COLUMNS: tuple[str, ...] = ("provider_name", "snapshot_month")


def snapshot_month_of(record: StorageLocationRecord) -> str:
    """Partition key for a record: the snapshot month, ``YYYY-MM``."""
    return record.snapshot_month.strftime("%Y-%m")


def record_to_row(record: StorageLocationRecord) -> dict[str, object]:
    """Flatten a :class:`StorageLocationRecord` into a storage-location row dict."""
    return {
        "location_kind": record.location_kind,
        "location_name": record.location_name,
        "url": record.url,
        "scheme": record.scheme,
        "cloud_provider_name": record.cloud_provider_name,
        "bucket_name": record.bucket_name,
        "key_prefix": record.key_prefix,
        "is_read_only": record.is_read_only,
        "credential_name": record.credential_name,
        "x_source_connector": record.x_source_connector,
        "provider_name": str(record.provider_name),
        "snapshot_month": snapshot_month_of(record),
    }


def build_table(records: list[StorageLocationRecord]) -> pa.Table:
    """Build a typed Arrow table (``STORAGE_LOCATION_SCHEMA``) from validated records."""
    return pa.Table.from_pylist(
        [record_to_row(r) for r in records], schema=STORAGE_LOCATION_SCHEMA
    )


def empty_table() -> pa.Table:
    """An empty, fully-typed storage-location table — the no-data fallback for readers."""
    return STORAGE_LOCATION_SCHEMA.empty_table()
