"""Typed Bronze evidence for Redshift cluster policy compliance."""

from __future__ import annotations

from datetime import date

import pyarrow as pa
from pydantic import BaseModel, field_validator


class RedshiftPolicyConfigRecord(BaseModel):
    provider_name: str = "AWS"
    snapshot_month: date
    cluster_id: str
    cluster_name: str | None = None
    encrypted: bool | None = None
    publicly_accessible: bool | None = None
    enhanced_vpc_routing: bool | None = None
    automated_snapshot_retention_days: int | None = None
    require_ssl: bool | None = None
    tag_count: int | None = None
    x_source_connector: str = "unknown"

    @field_validator("snapshot_month")
    @classmethod
    def _month(cls, value: date) -> date:
        return value.replace(day=1)


SCHEMA = pa.schema(
    [
        ("cluster_id", pa.string()), ("cluster_name", pa.string()),
        ("encrypted", pa.bool_()), ("publicly_accessible", pa.bool_()),
        ("enhanced_vpc_routing", pa.bool_()),
        ("automated_snapshot_retention_days", pa.int64()), ("require_ssl", pa.bool_()),
        ("tag_count", pa.int64()), ("x_source_connector", pa.string()),
        ("provider_name", pa.string()), ("snapshot_month", pa.string()),
    ]
)


def build_table(records: list[RedshiftPolicyConfigRecord]) -> pa.Table:
    return pa.Table.from_pylist(
        [
            {
                **r.model_dump(exclude={"snapshot_month"}),
                "snapshot_month": r.snapshot_month.strftime("%Y-%m"),
            }
            for r in records
        ], schema=SCHEMA
    )


def empty_table() -> pa.Table:
    return SCHEMA.empty_table()
