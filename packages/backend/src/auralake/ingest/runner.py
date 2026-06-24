"""Ingest orchestration. Subcommand: ``auralake ingest [--start --end]``.

For each enabled connector: stream FOCUS records, validate currency, and
partition-replace them into the BRONZE Parquet lake. A failing connector is
isolated — its run is logged failed and the others still proceed. After all
connectors land, GOLD is rebuilt from BRONZE so the data is immediately queryable.
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
from auralake.lake import bronze, runlog
from auralake.transform.runner import build_gold

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
    """Run one connector end-to-end. Returns rows written.

    Failures are isolated and logged to the run log; they return 0 rather than
    aborting the whole ingest.
    """
    base_currency = get_settings().base_currency
    run_id = bronze.new_run_id()
    started_at = datetime.now(UTC)
    try:
        records = []
        for record in connector.fetch(window):
            if record.billing_currency != base_currency:
                raise FocusValidationError(
                    f"{connector.name}: currency {record.billing_currency} "
                    f"!= base {base_currency}; mixed-currency sums are unsafe"
                )
            records.append(record)

        written = bronze.write_window(
            connector.name, window, records, ingest_run_id=run_id
        )
        runlog.record_run(
            run_id=run_id,
            connector=connector.name,
            status="success",
            rows=written,
            started_at=started_at,
            finished_at=datetime.now(UTC),
        )
        logger.info("ingest_ok", connector=connector.name, rows=written)
        return written
    except Exception as exc:  # noqa: BLE001 - isolate one connector's failure
        runlog.record_run(
            run_id=run_id,
            connector=connector.name,
            status="failed",
            rows=0,
            started_at=started_at,
            finished_at=datetime.now(UTC),
            detail=str(exc)[:1000],
        )
        logger.error("ingest_failed", connector=connector.name, error=str(exc))
        return 0


def run_ingest(
    start: date | None = None,
    end: date | None = None,
    connections: str | None = None,
    no_transform: bool = False,
) -> int:
    """Pull all enabled connectors for the window, then rebuild GOLD. Returns rows."""
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

    # Rebuild SILVER/GOLD from BRONZE so the data is immediately queryable.
    if not no_transform:
        published = build_gold()
        logger.info("transform_done", gold_views=published)
    logger.info("ingest_complete", connectors=len(configs), rows=total)
    return total
