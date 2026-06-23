"""Ingest orchestration. Subcommand: ``auralake ingest [--start --end]``.

For each enabled connector: open an IngestRun, stream FOCUS records, upsert them
idempotently, and close the run with a status. A failing connector is isolated —
its run is marked failed and the others still proceed.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from pydantic import BaseModel

from auralake.core.exceptions import FocusValidationError
from auralake.core.logging import get_logger
from auralake.core.settings import get_settings
from auralake.ingest.base import Connector, IngestWindow
from auralake.ingest.config import (
    AwsFocusConfig,
    AwsInfraConfig,
    DatabricksConfig,
    FocusFileConfig,
    load_connections,
)
from auralake.ingest.connectors import (
    AwsFocusConnector,
    AwsInfraConnector,
    DatabricksConnector,
    FocusFileConnector,
)
from auralake.store.engine import session_scope
from auralake.store.models import IngestRun
from auralake.store.upsert import delete_window, insert_focus_records
from auralake.transform.runner import apply_views

logger = get_logger(__name__)

_REGISTRY: dict[type[BaseModel], type[Connector]] = {
    AwsFocusConfig: AwsFocusConnector,
    FocusFileConfig: FocusFileConnector,
    DatabricksConfig: DatabricksConnector,
    AwsInfraConfig: AwsInfraConnector,
}

DEFAULT_LOOKBACK_DAYS = 35


def build_connector(config: BaseModel) -> Connector:
    cls = _REGISTRY.get(type(config))
    if cls is None:
        raise FocusValidationError(f"No connector for config {type(config).__name__}")
    return cls(config)  # type: ignore[call-arg]


def run_connector(connector: Connector, window: IngestWindow) -> int:
    """Run one connector end-to-end. Returns rows written."""
    base_currency = get_settings().base_currency
    with session_scope() as session:
        run = IngestRun(connector=connector.name, started_at=datetime.now(UTC))
        session.add(run)
        session.flush()  # assign run.id
        run_id = run.id
        assert run_id is not None

        try:
            records = []
            for record in connector.fetch(window):
                if record.billing_currency != base_currency:
                    raise FocusValidationError(
                        f"{connector.name}: currency {record.billing_currency} "
                        f"!= base {base_currency}; mixed-currency sums are unsafe"
                    )
                records.append(record)

            # Partition-replace, atomically: clear the window then insert the fresh
            # pull in one savepoint, so a failed insert can't leave the window emptied.
            with session.begin_nested():
                delete_window(session, connector.name, window.start, window.end)
                written = insert_focus_records(session, records, run_id)
            run.status = "success"
            run.rows_ingested = written
            run.finished_at = datetime.now(UTC)
            logger.info("ingest_ok", connector=connector.name, rows=written)
            return written
        except Exception as exc:  # noqa: BLE001
            run.status = "failed"
            run.detail = str(exc)[:1000]
            run.finished_at = datetime.now(UTC)
            session.add(run)
            logger.error("ingest_failed", connector=connector.name, error=str(exc))
            return 0


def run_ingest(
    start: date | None = None,
    end: date | None = None,
    connections: str | None = None,
    no_transform: bool = False,
) -> int:
    """Pull all enabled connectors for the window, then refresh views. Returns rows.

    Backs the ``auralake ingest`` subcommand.
    """
    # Self-apply schema so an on-demand ingest works without a separate migrate
    # step (no-op if already current; gated by AURALAKE_AUTO_MIGRATE).
    if get_settings().auto_migrate:
        from auralake.store.migrate import upgrade_to_head

        upgrade_to_head()

    end = end or date.today()
    start = start or (end - timedelta(days=DEFAULT_LOOKBACK_DAYS))
    window = IngestWindow(start=start, end=end)

    configs = load_connections(connections)
    if not configs:
        logger.warning("ingest_no_connectors")
        return 0

    total = 0
    for config in configs:
        connector = build_connector(config)
        total += run_connector(connector, window)

    # Refresh SILVER/GOLD so the data is immediately queryable (the bundled flow).
    if not no_transform:
        apply_views()
        logger.info("transform_done")
    logger.info("ingest_complete", connectors=len(configs), rows=total)
    return total
