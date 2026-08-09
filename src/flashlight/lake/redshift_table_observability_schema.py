"""Typed BRONZE records for durable Redshift table and Spectrum observability."""

from __future__ import annotations

from datetime import date, datetime

import pyarrow as pa
from pydantic import BaseModel


class RedshiftTableObservabilityRecord(BaseModel):
    """One daily fact from a read-only Redshift table-observability extract.

    ``record_kind`` distinguishes the three source grains. Keeping them in one
    typed relation makes a complete day/cluster snapshot atomically replaceable
    without pretending that metadata, table scans, and Spectrum scans share a
    natural entity grain.
    """

    cluster_id: str
    observation_date: date
    record_kind: str  # table_usage | external_catalog | external_query
    table_id: int | None = None
    table_name: str | None = None
    external_schema: str | None = None
    external_table: str | None = None
    source_type: str | None = None
    file_location: str | None = None
    file_format: str | None = None
    query_count: int | None = None
    scan_step_count: int | None = None
    scan_bytes: int | None = None
    rows_pre_filter: int | None = None
    rows_returned: int | None = None
    first_scan_at: datetime | None = None
    last_scan_at: datetime | None = None
    segment_count: int | None = None
    duration_seconds: float | None = None
    total_partitions: int | None = None
    qualified_partitions: int | None = None
    scanned_files: int | None = None
    source_rows: int | None = None
    source_bytes: int | None = None
    s3_listing_milliseconds: int | None = None
    partition_catalog_milliseconds: int | None = None
    redshift_database_name: str | None = None
    table_type: str | None = None
    table_location: str | None = None
    input_format: str | None = None
    output_format: str | None = None
    serialization_lib: str | None = None
    compressed: int | None = None
    table_parameters: str | None = None
    partition_count: int | None = None
    sample_partition_location: str | None = None
    x_source_connector: str = "unknown"


SCHEMA = pa.schema(
    [
        ("record_kind", pa.string()), ("table_id", pa.int64()), ("table_name", pa.string()),
        ("external_schema", pa.string()), ("external_table", pa.string()),
        ("source_type", pa.string()), ("file_location", pa.string()), ("file_format", pa.string()),
        ("query_count", pa.int64()), ("scan_step_count", pa.int64()), ("scan_bytes", pa.int64()),
        ("rows_pre_filter", pa.int64()), ("rows_returned", pa.int64()),
        ("first_scan_at", pa.timestamp("us")), ("last_scan_at", pa.timestamp("us")),
        ("segment_count", pa.int64()), ("duration_seconds", pa.float64()),
        ("total_partitions", pa.int64()), ("qualified_partitions", pa.int64()),
        ("scanned_files", pa.int64()), ("source_rows", pa.int64()), ("source_bytes", pa.int64()),
        ("s3_listing_milliseconds", pa.int64()), ("partition_catalog_milliseconds", pa.int64()),
        ("redshift_database_name", pa.string()), ("table_type", pa.string()),
        ("table_location", pa.string()), ("input_format", pa.string()),
        ("output_format", pa.string()), ("serialization_lib", pa.string()),
        ("compressed", pa.int64()), ("table_parameters", pa.string()),
        ("partition_count", pa.int64()), ("sample_partition_location", pa.string()),
        ("x_source_connector", pa.string()),
        ("cluster_id", pa.string()), ("observation_date", pa.string()),
    ]
)


def build_table(records: list[RedshiftTableObservabilityRecord]) -> pa.Table:
    return pa.Table.from_pylist(
        [
            {
                **record.model_dump(exclude={"observation_date"}),
                "observation_date": record.observation_date.isoformat(),
            }
            for record in records
        ],
        schema=SCHEMA,
    )


def empty_table() -> pa.Table:
    return SCHEMA.empty_table()
