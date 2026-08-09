"""The metrics-plane Parquet schema — the column contract for ``metrics.efficiency_record``.

The waste-plane sibling of :mod:`flashlight.lake.schema`: a fixed :class:`pyarrow.Schema`
mirroring :class:`~flashlight.efficiency.model.EfficiencyRecord`, plus the row/table
builders the efficiency pull writes. Explicit schema (not inference) keeps types stable
across partition files. ``cause_detail`` is a JSON **string** (the same choice as
``tags`` in the BRONZE schema). ``provider_name``, ``x_source_connector``, and
``charge_month`` are the Hive partition keys (``YYYY-MM``). The connector key makes
one Redshift cluster refresh independent of another cluster's AWS telemetry.
"""

from __future__ import annotations

import json
from decimal import ROUND_HALF_EVEN, Decimal

import pyarrow as pa

from flashlight.efficiency.model import EfficiencyRecord

_MONEY = pa.decimal128(20, 6)
_CENTS = Decimal("0.000001")

#: Non-partition columns first, then the three partition keys last.
METRICS_SCHEMA: pa.Schema = pa.schema(
    [
        ("entity_type", pa.string()),
        ("entity_id", pa.string()),
        ("entity_name", pa.string()),
        ("owner_user", pa.string()),
        ("owner_project", pa.string()),
        ("billed_cost", _MONEY),
        ("native_quantity", pa.float64()),
        ("native_unit", pa.string()),
        ("utilization_pct", pa.float64()),
        ("activity_count", pa.int64()),
        ("cause_detail", pa.string()),  # JSON
        # ── Hive partition keys (written as dirs, restored on read) ──────────
        ("provider_name", pa.string()),
        ("x_source_connector", pa.string()),
        ("charge_month", pa.string()),  # YYYY-MM
    ]
)

#: Partition columns, in directory nesting order.
PARTITION_COLUMNS: tuple[str, ...] = ("provider_name", "x_source_connector", "charge_month")


def _money(value: Decimal) -> Decimal:
    return value.quantize(_CENTS, rounding=ROUND_HALF_EVEN)


def charge_month_of(record: EfficiencyRecord) -> str:
    """Partition key for a record: the charge month, ``YYYY-MM``."""
    return record.charge_month.strftime("%Y-%m")


def record_to_row(record: EfficiencyRecord) -> dict[str, object]:
    """Flatten an :class:`EfficiencyRecord` into a metrics row dict (enums → values)."""
    return {
        "entity_type": record.entity_type.value,
        "entity_id": record.entity_id,
        "entity_name": record.entity_name,
        "owner_user": record.owner_user,
        "owner_project": record.owner_project,
        "billed_cost": _money(record.billed_cost),
        "native_quantity": record.native_quantity,
        "native_unit": record.native_unit,
        "utilization_pct": record.utilization_pct,
        "activity_count": record.activity_count,
        "cause_detail": json.dumps(record.cause_detail, sort_keys=True, default=str),
        "x_source_connector": record.x_source_connector,
        "provider_name": str(record.provider_name),
        "charge_month": charge_month_of(record),
    }


def build_table(records: list[EfficiencyRecord]) -> pa.Table:
    """Build a typed Arrow table (``METRICS_SCHEMA``) from validated records."""
    return pa.Table.from_pylist([record_to_row(r) for r in records], schema=METRICS_SCHEMA)


def empty_table() -> pa.Table:
    """An empty, fully-typed metrics table — the no-data fallback for readers."""
    return METRICS_SCHEMA.empty_table()
