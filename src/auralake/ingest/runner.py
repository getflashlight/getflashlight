"""Ingest orchestration. Subcommand: ``auralake ingest [--start --end]``.

For each enabled connector: stream FOCUS records, validate currency, and
partition-replace them into the BRONZE Parquet lake. Runs **fail-fast** — the
first connector to fail aborts the whole run: the remaining connectors are not
run, GOLD is *not* rebuilt, and :class:`IngestError` is raised so the CLI reports
the failure and exits non-zero. Only when every connector succeeds is GOLD rebuilt
from BRONZE. (BRONZE written by connectors that ran before the failure stays on
disk; the next clean run is authoritative for its window and overwrites it.)
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from pydantic import BaseModel

from auralake.core.exceptions import FocusValidationError, IngestError
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
from auralake.lake import bronze, metrics, runlog
from auralake.transform.runner import build_gold

logger = get_logger(__name__)

_REGISTRY: dict[type[BaseModel], type[Connector]] = {
    AwsFocusConfig: AwsFocusConnector,
    FocusFileConfig: FocusFileConnector,
    DatabricksConfig: DatabricksConnector,
    AwsInfraConfig: AwsInfraConnector,
}

DEFAULT_LOOKBACK_DAYS = 35


@dataclass
class ConnectorOutcome:
    """Result of running one connector: rows written, plus failure detail if any."""

    name: str
    rows: int
    ok: bool
    detail: str | None = None


def build_connector(config: BaseModel) -> Connector:
    cls = _REGISTRY.get(type(config))
    if cls is None:
        raise FocusValidationError(f"No connector for config {type(config).__name__}")
    return cls(config)  # type: ignore[call-arg]


def run_connector(connector: Connector, window: IngestWindow) -> ConnectorOutcome:
    """Run one connector end-to-end.

    Catches the connector's own exceptions so the failure is recorded to the run
    log and returned as ``ok=False`` (with detail) rather than escaping as a raw
    traceback. The orchestrator decides what to do with that — currently fail-fast.
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
        return ConnectorOutcome(name=connector.name, rows=written, ok=True)
    except Exception as exc:  # noqa: BLE001 - record the failure, don't leak a traceback
        detail = str(exc)[:1000]
        runlog.record_run(
            run_id=run_id,
            connector=connector.name,
            status="failed",
            rows=0,
            started_at=started_at,
            finished_at=datetime.now(UTC),
            detail=detail,
        )
        logger.error("ingest_failed", connector=connector.name, error=str(exc))
        return ConnectorOutcome(name=connector.name, rows=0, ok=False, detail=detail)


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

    # Fail-fast: run connectors in order and abort the whole run on the first
    # failure — don't run the rest, and don't rebuild GOLD. BRONZE from connectors
    # that already succeeded stays on disk, but GOLD is left untouched (rather than
    # published from a known-incomplete pull) until a clean run.
    total = 0
    for index, config in enumerate(configs):
        outcome = run_connector(build_connector(config), window)
        if not outcome.ok:
            logger.error(
                "ingest_aborted",
                failed=outcome.name,
                completed=index,
                remaining=len(configs) - index - 1,
            )
            raise IngestError([outcome.name])
        total += outcome.rows

    # Best-effort efficiency/waste pull (secondary to cost): each connector that exposes
    # utilization telemetry writes aggregated EfficiencyRecords to the metrics plane. A
    # failure here warns and skips — it must NOT block the canonical cost pipeline.
    _run_efficiency(window, configs)

    # Every connector succeeded — rebuild SILVER/GOLD so the data is queryable.
    if not no_transform:
        published = build_gold()
        logger.info("transform_done", gold_views=published)
    logger.info("ingest_complete", connectors=len(configs), rows=total)
    return total


def _run_efficiency(window: IngestWindow, configs: list[BaseModel]) -> int:
    """Pull + write efficiency records for every connector that exposes them. Best-effort.

    Returns rows written. Per-connector failures are logged and skipped so the waste
    view simply goes stale rather than aborting the cost ingest.
    """
    written = 0
    for config in configs:
        connector = build_connector(config)
        name = getattr(connector, "name", type(connector).__name__)
        try:
            records = list(connector.fetch_efficiency(window))
        except Exception as exc:  # noqa: BLE001 - secondary signal; never block ingest
            logger.warning("efficiency_pull_failed", connector=name, error=str(exc))
            continue
        if records:
            written += metrics.write_efficiency(window, records)
            logger.info("efficiency_written", connector=name, rows=len(records))
    return written
