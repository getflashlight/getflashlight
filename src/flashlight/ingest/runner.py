"""Ingest orchestration. Subcommand: ``flashlight ingest [--start --end]``.

For each enabled connector, ``connector.ingest()`` pulls and partition-replaces
its data into the BRONZE Parquet lake (see ``ingest/base.py`` for the row-based
default vs. a connector's own vectorized override). Every connector runs
regardless of earlier failures — one broken source (an expired token, a moved
bucket) must not block a fresh pull from every other source. Failures are
collected and, once every connector has run, raised together as
:class:`IngestError` so the CLI still exits non-zero and names every connector
that needs attention. GOLD is rebuilt from whatever succeeded (best-effort
efficiency/driver-health pulls run only for the connectors whose cost pull
worked — a connector with broken creds fails those the same way).
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from pydantic import BaseModel

from flashlight.core.exceptions import FocusValidationError, IngestError
from flashlight.core.logging import get_logger
from flashlight.core.settings import get_settings
from flashlight.efficiency.model import EfficiencyRecord
from flashlight.ingest.base import Connector, IngestWindow, ProgressCallback
from flashlight.ingest.config import (
    AwsFocusConfig,
    AwsInfraConfig,
    DatabricksConfig,
    FocusFileConfig,
    RedshiftConfig,
    load_connections,
)
from flashlight.ingest.connectors import (
    AwsFocusConnector,
    AwsInfraConnector,
    DatabricksConnector,
    FocusFileConnector,
    RedshiftConnector,
)
from flashlight.lake import bronze, driver_health, metrics, runlog
from flashlight.lake.driver_health_schema import DriverHealthRecord
from flashlight.transform.runner import build_gold

logger = get_logger(__name__)

_REGISTRY: dict[type[BaseModel], type[Connector]] = {
    AwsFocusConfig: AwsFocusConnector,
    FocusFileConfig: FocusFileConnector,
    DatabricksConfig: DatabricksConnector,
    AwsInfraConfig: AwsInfraConnector,
    RedshiftConfig: RedshiftConnector,
}

DEFAULT_LOOKBACK_DAYS = 35


def _max_workers(n: int) -> int:
    """Thread-pool size for ``n`` connectors: bounded by config, never above ``n``,
    never below 1 (``ThreadPoolExecutor`` rejects 0) so a single-connector run still
    gets a valid pool instead of a special case.
    """
    return max(1, min(n, get_settings().ingest_max_workers))


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


def run_connector(
    connector: Connector,
    window: IngestWindow,
    on_progress: ProgressCallback | None = None,
    *,
    full_refresh: bool = False,
) -> ConnectorOutcome:
    """Run one connector end-to-end.

    Catches the connector's own exceptions so the failure is recorded to the run
    log and returned as ``ok=False`` (with detail) rather than escaping as a raw
    traceback. The orchestrator (:func:`run_ingest`) runs every connector
    regardless of what this returns. The actual pull + write is
    ``connector.ingest()`` — the default (row-based) or a connector's own
    vectorized override; see ``ingest/base.py``.

    ``full_refresh`` wipes this connector's entire bronze history
    (:func:`flashlight.lake.bronze.purge_connector`) before the normal
    window-scoped ``ingest()`` call — so any partition outside ``window`` is
    gone, not just replaced within it. Safe to run concurrently across
    connectors: each purges only its own ``x_source_connector=<name>`` dir.
    """
    if full_refresh:
        bronze.purge_connector(connector.name)
    run_id = bronze.new_run_id()
    started_at = datetime.now(UTC)
    if on_progress:
        on_progress("start", connector.name, 0)
    try:
        written = connector.ingest(window, run_id=run_id, on_progress=on_progress)
        runlog.record_run(
            run_id=run_id,
            connector=connector.name,
            status="success",
            rows=written,
            started_at=started_at,
            finished_at=datetime.now(UTC),
        )
        note = {"note": connector.cost_pull_note} if connector.cost_pull_note else {}
        logger.info("ingest_ok", connector=connector.name, rows=written, **note)
        if on_progress:
            on_progress("done", connector.name, written)
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
        if on_progress:
            on_progress("failed", connector.name, 0)
        return ConnectorOutcome(name=connector.name, rows=0, ok=False, detail=detail)


def run_ingest(
    start: date | None = None,
    end: date | None = None,
    connections: str | None = None,
    no_transform: bool = False,
    full_refresh: bool = False,
    on_progress: ProgressCallback | None = None,
) -> int:
    """Pull every enabled connector for the window, then rebuild GOLD. Returns rows.

    Every connector runs even if an earlier one failed — a broken source must
    not block a fresh pull from the others. If any connector failed,
    :class:`IngestError` is raised at the end (after GOLD has been rebuilt from
    the survivors) so the CLI still exits non-zero and names every connector
    that needs attention.

    ``full_refresh`` wipes each connector's entire bronze history before this
    run's ``[start, end]`` pull, instead of only replacing that window's own
    partitions — see :func:`run_connector`. Orthogonal to ``start``/``end``:
    those still decide what gets pulled back in.
    """
    end = end or date.today()
    start = start or (end - timedelta(days=DEFAULT_LOOKBACK_DAYS))
    window = IngestWindow(start=start, end=end)

    configs = load_connections(connections)
    if not configs:
        logger.warning("ingest_no_connectors")
        return 0

    # Each connector's fetch()/write targets its own x_source_connector BRONZE
    # partition dir (see lake/bronze.py) and its own runlog file, so concurrent
    # pulls don't contend — a bounded pool overlaps their network/DuckDB wait
    # instead of summing it. Pool.map preserves input order in its results
    # regardless of which thread finishes first, so the zip below stays correct.
    with ThreadPoolExecutor(max_workers=_max_workers(len(configs))) as pool:
        outcomes = list(
            pool.map(
                lambda config: run_connector(
                    build_connector(config), window, on_progress, full_refresh=full_refresh
                ),
                configs,
            )
        )

    total = 0
    failed: list[str] = []
    succeeded_configs: list[BaseModel] = []
    for config, outcome in zip(configs, outcomes, strict=True):
        if outcome.ok:
            total += outcome.rows
            succeeded_configs.append(config)
        else:
            failed.append(outcome.name)
            logger.error("connector_failed", connector=outcome.name, detail=outcome.detail)

    # Best-effort efficiency/waste pull (secondary to cost): each connector that exposes
    # utilization telemetry writes aggregated EfficiencyRecords to the metrics plane. A
    # failure here warns and skips — it must NOT block the canonical cost pipeline.
    # Only the connectors whose cost pull just succeeded are retried — one whose
    # fetch() already failed almost certainly has broken creds/config, and
    # re-invoking it here would just duplicate that failure.
    _run_efficiency(window, succeeded_configs)

    # Best-effort driver-health pull (fleet-health/compliance, unrelated to waste): each
    # connector that exposes client-driver telemetry writes aggregated
    # DriverHealthRecords. Same never-block-cost-ingest guarantee as efficiency.
    _run_driver_health(window, succeeded_configs)

    # Rebuild SILVER/GOLD from whatever succeeded, so it's queryable even if some
    # connectors failed. A failed connector's own window is left as it was before
    # this run (bronze.write_window re-purges on error, never leaving a partial
    # write), so GOLD never reflects a half-written pull.
    if not no_transform and succeeded_configs:
        published = build_gold()
        logger.info("transform_done", gold_views=published)
    logger.info(
        "ingest_complete",
        connectors=len(configs),
        succeeded=len(succeeded_configs),
        failed=len(failed),
        rows=total,
    )
    if failed:
        raise IngestError(failed)
    return total


def _run_efficiency(window: IngestWindow, configs: list[BaseModel]) -> int:
    """Pull efficiency records for every connector that exposes them (concurrently,
    bounded — see :func:`_max_workers`), then write them all in one call. Best-effort:
    a per-connector pull failure is logged and skipped, never raised.

    The single combined write isn't just for thread-safety — ``write_efficiency``
    purges its target provider/month partitions wholesale before writing, and more
    than one connector can share a ``provider_name`` (``aws_focus`` and ``redshift``
    both emit "AWS"). Writing per-connector, even run one at a time, means the second
    connector's write purges and silently drops the first connector's rows. Gathering
    every connector's records first and writing once is the actual fix; running the
    fetches concurrently is free on top of it.
    """

    def _pull(config: BaseModel) -> list[EfficiencyRecord]:
        connector = build_connector(config)
        name = getattr(connector, "name", type(connector).__name__)
        try:
            records = list(connector.fetch_efficiency(window))
        except Exception as exc:  # noqa: BLE001 - secondary signal; never block ingest
            logger.warning("efficiency_pull_failed", connector=name, error=str(exc))
            return []
        if records:
            logger.info("efficiency_fetched", connector=name, rows=len(records))
        return records

    with ThreadPoolExecutor(max_workers=_max_workers(len(configs))) as pool:
        all_records = [record for batch in pool.map(_pull, configs) for record in batch]

    if not all_records:
        return 0
    written = metrics.write_efficiency(window, all_records)
    logger.info("efficiency_written", rows=written)
    return written


def _run_driver_health(window: IngestWindow, configs: list[BaseModel]) -> int:
    """Pull driver-health records for every connector that exposes them (concurrently,
    bounded), then write them all in one call. Best-effort, same never-block-cost-
    ingest guarantee as :func:`_run_efficiency` — and the same purge-before-write
    reasoning for merging into a single write (only one connector emits these today,
    but the fix is the same shape and costs nothing extra to apply now).
    """

    def _pull(config: BaseModel) -> list[DriverHealthRecord]:
        connector = build_connector(config)
        name = getattr(connector, "name", type(connector).__name__)
        try:
            records = list(connector.fetch_driver_health(window))
        except Exception as exc:  # noqa: BLE001 - secondary signal; never block ingest
            logger.warning("driver_health_pull_failed", connector=name, error=str(exc))
            return []
        if records:
            logger.info("driver_health_fetched", connector=name, rows=len(records))
        return records

    with ThreadPoolExecutor(max_workers=_max_workers(len(configs))) as pool:
        all_records = [record for batch in pool.map(_pull, configs) for record in batch]

    if not all_records:
        return 0
    written = driver_health.write_driver_health(window, all_records)
    logger.info("driver_health_written", rows=written)
    return written
