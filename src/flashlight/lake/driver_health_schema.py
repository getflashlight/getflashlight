"""Client-driver fleet-health record + its Parquet schema.

A second, parallel telemetry dataset alongside the efficiency/waste plane — same
*pattern* (aggregated-at-source record → partition-replace Parquet → DuckDB view →
GOLD passthrough), different table, because this data has no dollar/waste semantics
(no ``entity_type``, no ``billed_cost``) and doesn't fit ``EfficiencyRecord``. It's a
fleet-health/compliance signal: which JDBC/ODBC driver versions and applications are
hitting Databricks, and who's running them — for humans to judge staleness, not an
automated verdict (there's no reference table of "current" versions in our data).
"""

from __future__ import annotations

from datetime import date

import pyarrow as pa
from pydantic import BaseModel, field_validator


class DriverHealthRecord(BaseModel):
    """One (driver, application, user)'s query volume for one month, aggregated at source."""

    provider_name: str  # Databricks | … (partition key)
    charge_month: date  # first of month (partition key)
    client_driver: str | None = None  # e.g. "DatabricksJDBCDriver, 2.7.1"
    client_application: str | None = None  # e.g. "Fivetran", "Tableau"
    executed_by: str | None = None
    query_count: int = 0
    x_source_connector: str = "unknown"

    @field_validator("charge_month")
    @classmethod
    def _first_of_month(cls, v: date) -> date:
        return v.replace(day=1)


#: Non-partition columns first, then the two partition keys last — mirrors
#: lake/metrics_schema.py::METRICS_SCHEMA.
DRIVER_HEALTH_SCHEMA: pa.Schema = pa.schema(
    [
        ("client_driver", pa.string()),
        ("client_application", pa.string()),
        ("executed_by", pa.string()),
        ("query_count", pa.int64()),
        ("x_source_connector", pa.string()),
        # ── Hive partition keys (written as dirs, restored on read) ──────────
        ("provider_name", pa.string()),
        ("charge_month", pa.string()),  # YYYY-MM
    ]
)

PARTITION_COLUMNS: tuple[str, ...] = ("provider_name", "charge_month")


def charge_month_of(record: DriverHealthRecord) -> str:
    """Partition key for a record: the charge month, ``YYYY-MM``."""
    return record.charge_month.strftime("%Y-%m")


def record_to_row(record: DriverHealthRecord) -> dict[str, object]:
    """Flatten a :class:`DriverHealthRecord` into a driver-health row dict."""
    return {
        "client_driver": record.client_driver,
        "client_application": record.client_application,
        "executed_by": record.executed_by,
        "query_count": record.query_count,
        "x_source_connector": record.x_source_connector,
        "provider_name": str(record.provider_name),
        "charge_month": charge_month_of(record),
    }


def build_table(records: list[DriverHealthRecord]) -> pa.Table:
    """Build a typed Arrow table (``DRIVER_HEALTH_SCHEMA``) from validated records."""
    return pa.Table.from_pylist([record_to_row(r) for r in records], schema=DRIVER_HEALTH_SCHEMA)


def empty_table() -> pa.Table:
    """An empty, fully-typed driver-health table — the no-data fallback for readers."""
    return DRIVER_HEALTH_SCHEMA.empty_table()
