"""The BRONZE Parquet schema — the column contract for ``raw.focus_record``.

This is the Parquet-world replacement for the old SQLModel table: a fixed
:class:`pyarrow.Schema` mirroring :class:`~flashlight.focus.model.FocusRecord`,
plus the row/table builders ingest uses to write it. An explicit schema (not
inference) keeps Decimal/timestamp types stable across every partition file, so
``read_parquet`` over many files never hits a type mismatch.

``tags`` is stored as a JSON **string** (not a Parquet MAP): tag keys like
``kubernetes.io/cluster/<name>`` contain dots and slashes that make JSON-path and
MAP handling fiddly, and DuckDB's ``json_extract_string`` reads a VARCHAR-JSON
column directly. ``x_source_connector`` and ``charge_month`` are the Hive
partition keys — DuckDB writes them as directory names and restores them as
columns on read (``hive_partitioning=true``).
"""

from __future__ import annotations

import json
from datetime import datetime
from decimal import ROUND_HALF_EVEN, Decimal

import pyarrow as pa

from flashlight.focus.model import FocusRecord

# Costs land as NUMERIC(20,6) — quantize so high-precision source values can't
# overflow the Arrow decimal (matches the old Postgres column behaviour).
_MONEY = pa.decimal128(20, 6)
_CENTS = Decimal("0.000001")
_TS = pa.timestamp("us", tz="UTC")

#: Column order is deliberate: provenance, then the FOCUS body, then the two
#: partition keys last (``x_source_connector``, ``charge_month``).
BRONZE_SCHEMA: pa.Schema = pa.schema(
    [
        # ── Provenance ──────────────────────────────────────────────────────
        ("dedupe_key", pa.string()),
        ("ingest_run_id", pa.string()),
        ("x_ingested_at", _TS),
        # ── Accounts ────────────────────────────────────────────────────────
        ("provider_name", pa.string()),
        ("billing_account_id", pa.string()),
        ("billing_account_name", pa.string()),
        ("sub_account_id", pa.string()),
        ("sub_account_name", pa.string()),
        # ── Periods ─────────────────────────────────────────────────────────
        ("billing_period_start", pa.date32()),
        ("billing_period_end", pa.date32()),
        ("charge_period_start", _TS),
        ("charge_period_end", _TS),
        # ── Costs ───────────────────────────────────────────────────────────
        ("billing_currency", pa.string()),
        ("billed_cost", _MONEY),
        ("effective_cost", _MONEY),
        ("list_cost", _MONEY),
        ("contracted_cost", _MONEY),
        # ── Classification ──────────────────────────────────────────────────
        ("charge_category", pa.string()),
        ("charge_class", pa.string()),
        ("charge_description", pa.string()),
        # ── Service / SKU / location ────────────────────────────────────────
        ("service_category", pa.string()),
        ("service_name", pa.string()),
        ("sku_id", pa.string()),
        ("region_id", pa.string()),
        # ── Resource ────────────────────────────────────────────────────────
        ("resource_id", pa.string()),
        ("resource_name", pa.string()),
        ("resource_type", pa.string()),
        # ── Usage ───────────────────────────────────────────────────────────
        ("consumed_quantity", pa.float64()),
        ("consumed_unit", pa.string()),
        # ── Contract commitment / invoice ───────────────────────────────────
        ("commitment_discount_id", pa.string()),
        ("commitment_discount_type", pa.string()),
        ("commitment_discount_category", pa.string()),
        ("commitment_discount_name", pa.string()),
        ("commitment_discount_status", pa.string()),
        ("commitment_discount_quantity", pa.float64()),
        ("commitment_discount_unit", pa.string()),
        ("invoice_id", pa.string()),
        ("invoice_issuer_name", pa.string()),
        # ── Tags (JSON string) + extensions ─────────────────────────────────
        ("tags", pa.string()),
        ("x_compute_class", pa.string()),
        ("x_focus_version", pa.string()),
        ("x_effective_is_list", pa.bool_()),
        ("x_record_id", pa.string()),
        ("x_record_type", pa.string()),
        ("x_cost_subcategory", pa.string()),
        # ── Hive partition keys (written as dirs, restored on read) ──────────
        ("x_source_connector", pa.string()),
        ("charge_month", pa.string()),
    ]
)

#: Partition columns, in directory nesting order.
PARTITION_COLUMNS: tuple[str, ...] = ("x_source_connector", "charge_month")


def _money(value: Decimal) -> Decimal:
    return value.quantize(_CENTS, rounding=ROUND_HALF_EVEN)


def charge_month_of(record: FocusRecord) -> str:
    """Partition key for a record: the charge period's calendar month, ``YYYY-MM``."""
    return record.charge_period_start.strftime("%Y-%m")


def record_to_row(
    record: FocusRecord, *, ingest_run_id: str, ingested_at: datetime
) -> dict[str, object]:
    """Flatten a :class:`FocusRecord` into a BRONZE row dict (enums → values)."""
    return {
        "dedupe_key": record.dedupe_key(),
        "ingest_run_id": ingest_run_id,
        "x_ingested_at": ingested_at,
        "provider_name": str(record.provider_name),
        "billing_account_id": record.billing_account_id,
        "billing_account_name": record.billing_account_name,
        "sub_account_id": record.sub_account_id,
        "sub_account_name": record.sub_account_name,
        "billing_period_start": record.billing_period_start,
        "billing_period_end": record.billing_period_end,
        "charge_period_start": record.charge_period_start,
        "charge_period_end": record.charge_period_end,
        "billing_currency": record.billing_currency,
        "billed_cost": _money(record.billed_cost),
        "effective_cost": _money(record.effective_cost),
        "list_cost": _money(record.list_cost),
        "contracted_cost": _money(record.contracted_cost),
        "charge_category": record.charge_category.value,
        "charge_class": record.charge_class.value if record.charge_class else None,
        "charge_description": record.charge_description,
        "service_category": record.service_category.value,
        "service_name": record.service_name,
        "sku_id": record.sku_id,
        "region_id": record.region_id,
        "resource_id": record.resource_id,
        "resource_name": record.resource_name,
        "resource_type": record.resource_type,
        "consumed_quantity": record.consumed_quantity,
        "consumed_unit": record.consumed_unit,
        "commitment_discount_id": record.commitment_discount_id,
        "commitment_discount_type": record.commitment_discount_type,
        "commitment_discount_category": (
            record.commitment_discount_category.value
            if record.commitment_discount_category
            else None
        ),
        "commitment_discount_name": record.commitment_discount_name,
        "commitment_discount_status": (
            record.commitment_discount_status.value if record.commitment_discount_status else None
        ),
        "commitment_discount_quantity": record.commitment_discount_quantity,
        "commitment_discount_unit": record.commitment_discount_unit,
        "invoice_id": record.invoice_id,
        "invoice_issuer_name": record.invoice_issuer_name,
        "tags": json.dumps(record.tags, sort_keys=True),
        "x_compute_class": record.x_compute_class.value,
        "x_focus_version": record.x_focus_version,
        "x_effective_is_list": record.x_effective_is_list,
        "x_record_id": record.x_record_id,
        "x_record_type": record.x_record_type,
        "x_cost_subcategory": record.x_cost_subcategory,
        "x_source_connector": record.x_source_connector,
        "charge_month": charge_month_of(record),
    }


def build_table(
    records: list[FocusRecord], *, ingest_run_id: str, ingested_at: datetime
) -> pa.Table:
    """Build a typed Arrow table (``BRONZE_SCHEMA``) from validated records."""
    rows = [
        record_to_row(r, ingest_run_id=ingest_run_id, ingested_at=ingested_at)
        for r in records
    ]
    return pa.Table.from_pylist(rows, schema=BRONZE_SCHEMA)


def empty_table() -> pa.Table:
    """An empty, fully-typed BRONZE table — the no-data fallback for readers."""
    return BRONZE_SCHEMA.empty_table()
