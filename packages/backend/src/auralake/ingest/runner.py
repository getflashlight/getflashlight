"""Ingest orchestration. Entry point: ``auralake-ingest [--start --end]``.

For each enabled connector: open an IngestRun, stream FOCUS records, upsert them
idempotently, and close the run with a status. A failing connector is isolated —
its run is marked failed and the others still proceed.
"""

from __future__ import annotations

import argparse
from datetime import UTC, date, datetime, timedelta

from pydantic import BaseModel

from auralake.core.exceptions import FocusValidationError
from auralake.core.logging import get_logger, setup_logging
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
from auralake.store.upsert import upsert_focus_records
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

            written = upsert_focus_records(session, records, run_id)
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


def run() -> None:
    parser = argparse.ArgumentParser(description="Run Auralake FOCUS ingestion")
    parser.add_argument("--start", type=date.fromisoformat, default=None)
    parser.add_argument("--end", type=date.fromisoformat, default=None)
    parser.add_argument("--connections", default=None, help="Path to connections.yml")
    parser.add_argument(
        "--no-transform", action="store_true", help="Skip refreshing SILVER/GOLD views"
    )
    args = parser.parse_args()

    setup_logging()
    end = args.end or date.today()
    start = args.start or (end - timedelta(days=DEFAULT_LOOKBACK_DAYS))
    window = IngestWindow(start=start, end=end)

    configs = load_connections(args.connections)
    if not configs:
        logger.warning("ingest_no_connectors")
        return

    total = 0
    for config in configs:
        connector = build_connector(config)
        total += run_connector(connector, window)

    # Refresh SILVER/GOLD so the data is immediately queryable (the bundled flow).
    if not args.no_transform:
        apply_views()
        logger.info("transform_done")
    logger.info("ingest_complete", connectors=len(configs), rows=total)


if __name__ == "__main__":
    run()
