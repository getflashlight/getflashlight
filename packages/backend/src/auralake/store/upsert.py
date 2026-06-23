"""Idempotent bulk upsert of canonical FOCUS records into the BRONZE table.

Re-ingesting a restated billing export must *correct* existing rows, not
duplicate them. We rely on the UNIQUE(dedupe_key) constraint and Postgres
``ON CONFLICT DO UPDATE`` to overwrite the mutable fields (costs, tags, run id)
of a previously-seen charge line.
"""

from __future__ import annotations

from sqlalchemy.dialects.postgresql import insert
from sqlmodel import Session

from auralake.focus.model import FocusRecord
from auralake.store.models import RawFocusRecord

# Fields refreshed when a charge line is re-seen (a restatement).
_MUTABLE_FIELDS = (
    "ingest_run_id",
    "billed_cost",
    "effective_cost",
    "list_cost",
    "contracted_cost",
    "consumed_quantity",
    "consumed_unit",
    "charge_description",
    "tags",
    "x_compute_class",
    "x_focus_version",
)


def _to_row(record: FocusRecord, ingest_run_id: int) -> dict[str, object]:
    return {
        "dedupe_key": record.dedupe_key(),
        "ingest_run_id": ingest_run_id,
        "provider_name": record.provider_name.value,
        "billing_account_id": record.billing_account_id,
        "billing_account_name": record.billing_account_name,
        "sub_account_id": record.sub_account_id,
        "sub_account_name": record.sub_account_name,
        "billing_period_start": record.billing_period_start,
        "billing_period_end": record.billing_period_end,
        "charge_period_start": record.charge_period_start,
        "charge_period_end": record.charge_period_end,
        "billing_currency": record.billing_currency,
        "billed_cost": record.billed_cost,
        "effective_cost": record.effective_cost,
        "list_cost": record.list_cost,
        "contracted_cost": record.contracted_cost,
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
        "tags": record.tags,
        "x_compute_class": record.x_compute_class.value,
        "x_focus_version": record.x_focus_version,
        "x_source_connector": record.x_source_connector,
    }


def upsert_focus_records(
    session: Session,
    records: list[FocusRecord],
    ingest_run_id: int,
    batch_size: int = 1000,
) -> int:
    """Upsert *records* into ``raw.focus_record``. Returns rows written."""
    written = 0
    for start in range(0, len(records), batch_size):
        batch = records[start : start + batch_size]
        rows = [_to_row(r, ingest_run_id) for r in batch]
        stmt = insert(RawFocusRecord).values(rows)
        stmt = stmt.on_conflict_do_update(
            index_elements=["dedupe_key"],
            set_={f: getattr(stmt.excluded, f) for f in _MUTABLE_FIELDS},
        )
        session.execute(stmt)
        written += len(rows)
    return written
