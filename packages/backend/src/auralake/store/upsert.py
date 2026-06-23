"""BRONZE writes: partition-replace per (connector, charge-period window).

Cloud billing is restated data — Databricks appends corrections, AWS CUR re-delivers
whole months. So ingest is authoritative for the window it pulls: ``delete_window``
purges the connector's existing rows in that charge-period range, then
``insert_focus_records`` bulk-inserts the fresh pull. Re-running the same window is
therefore idempotent AND self-purging (bad/orphaned rows can't survive). Caller must
run both in one transaction (savepoint) so a failed insert never leaves a hole.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta

from sqlalchemy import delete, insert
from sqlmodel import Session, col

from auralake.core.logging import get_logger
from auralake.focus.model import FocusRecord
from auralake.store.models import RawFocusRecord

logger = get_logger(__name__)


def _to_row(record: FocusRecord, ingest_run_id: int) -> dict[str, object]:
    return {
        "dedupe_key": record.dedupe_key(),
        "ingest_run_id": ingest_run_id,
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
        "x_effective_is_list": record.x_effective_is_list,
        "x_record_id": record.x_record_id,
        "x_record_type": record.x_record_type,
    }


def collapse_duplicates(records: list[FocusRecord]) -> list[FocusRecord]:
    """Collapse records sharing a ``dedupe_key`` (last wins) — a within-batch guard.

    After a window delete the insert is conflict-free, but a single pull must still not
    contain the same physical row twice (the UNIQUE(dedupe_key) constraint would
    reject it). The key identifies a physical source row — for Databricks it includes
    record_id + record_type, so ORIGINAL/RETRACTION/RESTATEMENT do NOT collapse; their
    costs net later via SUM in SILVER/GOLD (retraction is negative). A genuine collision
    here means the identical row appeared twice in one pull, so last-wins loses nothing.
    """
    by_key: dict[str, FocusRecord] = {}
    for record in records:
        by_key[record.dedupe_key()] = record

    if len(by_key) != len(records):
        logger.info(
            "collapsed_duplicate_rows",
            input_rows=len(records),
            output_rows=len(by_key),
            dropped=len(records) - len(by_key),
        )
    return list(by_key.values())


def delete_window(session: Session, connector_name: str, start: date, end: date) -> int:
    """Delete a connector's rows whose charge period falls in [start, end] (inclusive).

    Makes the upcoming insert authoritative for the window: anything the source no
    longer reports (or earlier bad data) is purged. ``end`` is inclusive, so we delete
    up to the exclusive start of the day after ``end``.
    """
    lo = datetime.combine(start, time.min, tzinfo=UTC)
    hi = datetime.combine(end + timedelta(days=1), time.min, tzinfo=UTC)
    stmt = delete(RawFocusRecord).where(
        col(RawFocusRecord.x_source_connector) == connector_name,
        col(RawFocusRecord.charge_period_start) >= lo,
        col(RawFocusRecord.charge_period_start) < hi,
    )
    # Log the effective SQL so the destructive step is auditable in the run log.
    logger.info(
        "window_delete_sql",
        sql=(
            "DELETE FROM raw.focus_record WHERE "
            f"x_source_connector = '{connector_name}' "
            f"AND charge_period_start >= '{lo.isoformat()}' "
            f"AND charge_period_start < '{hi.isoformat()}'"
        ),
    )
    result = session.execute(stmt)
    deleted = int(getattr(result, "rowcount", 0) or 0)
    logger.info("window_deleted", connector=connector_name, rows=deleted, start=str(start),
                end=str(end))
    return deleted


def insert_focus_records(
    session: Session,
    records: list[FocusRecord],
    ingest_run_id: int,
    batch_size: int = 1000,
) -> int:
    """Bulk-insert *records* into ``raw.focus_record``. Returns rows written.

    Assumes the target window was already cleared via ``delete_window`` in the same
    transaction, so a plain INSERT is conflict-free.
    """
    deduped = collapse_duplicates(records)

    written = 0
    for start in range(0, len(deduped), batch_size):
        batch = deduped[start : start + batch_size]
        rows = [_to_row(r, ingest_run_id) for r in batch]
        session.execute(insert(RawFocusRecord).values(rows))
        written += len(rows)
    return written
