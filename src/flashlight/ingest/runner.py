"""Ingest orchestration. Subcommand: ``flashlight ingest [--start --end]``.

For each enabled connector, ``connector.ingest()`` pulls and partition-replaces
its data into the BRONZE Parquet lake (see ``ingest/base.py`` for the row-based
default vs. a connector's own vectorized override). Every connector runs
regardless of earlier failures — one broken source (an expired token, a moved
bucket) must not block a fresh pull from every other source. Failures are
collected and, once every connector has run, raised together as
:class:`IngestError` so the CLI still exits non-zero and names every connector
that needs attention. After every selected connector has finished writing its
own BRONZE data (including best-effort supplemental telemetry for successful
cost pulls), SILVER and GOLD are built once from the complete available lake.
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
    DatabricksConfig,
    RedshiftConfig,
    effective_connector_name,
    load_connections,
)
from flashlight.ingest.connectors import (
    AwsFocusConnector,
    DatabricksConnector,
    RedshiftConnector,
)
from flashlight.lake import (
    ai_usage,
    bronze,
    compute_instances,
    driver_health,
    metrics,
    redshift_policy_config,
    runlog,
    storage_locations,
)
from flashlight.lake.ai_usage_schema import AiUsageRecord
from flashlight.lake.compute_instance_schema import ComputeInstanceRecord
from flashlight.lake.driver_health_schema import DriverHealthRecord
from flashlight.lake.redshift_policy_config_schema import RedshiftPolicyConfigRecord
from flashlight.lake.storage_location_schema import StorageLocationRecord
from flashlight.transform.runner import build_gold

logger = get_logger(__name__)

_REGISTRY: dict[type[BaseModel], type[Connector]] = {
    AwsFocusConfig: AwsFocusConnector,
    DatabricksConfig: DatabricksConnector,
    RedshiftConfig: RedshiftConnector,
}

#: Fallback when ``FLASHLIGHT_INGEST_LOOKBACK_DAYS`` is unset — see
#: :attr:`~flashlight.core.settings.Settings.ingest_lookback_days`.
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
    run_id: str,
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

    ``run_id`` is shared across every connector in the same :func:`run_ingest`
    call (generated once there, not per connector here) — it's what lets the
    dashboard's "Recent sync history" group connector rows back into one sync,
    and what every BRONZE row this connector writes gets stamped with
    (``x_ingest_run_id``) alongside its own ``x_source_connector``, so nothing
    about per-row traceability is lost by sharing it.

    ``full_refresh`` wipes this connector's entire bronze history
    (:func:`flashlight.lake.bronze.purge_connector`) before the normal
    window-scoped ``ingest()`` call — so any partition outside ``window`` is
    gone, not just replaced within it. Safe to run concurrently across
    connectors: each purges only its own ``x_source_connector=<name>`` dir.
    """
    if full_refresh:
        bronze.purge_connector(connector.name)
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
    connector: str | None = None,
    on_progress: ProgressCallback | None = None,
    run_id: str | None = None,
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

    ``connector`` restricts the run to the one connector whose
    :func:`effective_connector_name` matches (the dashboard's per-connection Sync
    button; the CLI's ``--connector``) — everything else about the run (window,
    full_refresh, the GOLD rebuild reading all of BRONZE afterward) is unchanged.

    ``run_id`` identifies this whole sync in the run log (see :func:`run_connector`)
    — generated here if the caller doesn't supply one (a bare CLI invocation), or
    passed in by a caller that needs to know it up front (the dashboard's
    ``--run-id``, so it can name a log file before the subprocess even starts —
    see ``dashboard/ingest_runner.py::stream_sync``).
    """
    end = end or date.today()
    start = start or (end - timedelta(days=get_settings().ingest_lookback_days))
    window = IngestWindow(start=start, end=end)
    run_id = run_id or bronze.new_run_id()

    configs = load_connections(connections)
    if connector is not None:
        configs = [c for c in configs if effective_connector_name(c) == connector]
    if not configs:
        logger.warning("ingest_no_connectors")
        return 0

    # Built once per config and reused across the cost/efficiency/driver-health
    # phases below (instead of building fresh per phase) so a connector's
    # instance-level caches — e.g. Databricks' resolved SQL warehouse id and
    # account-prices probe — pay their network round-trip once per run, not
    # once per phase.
    connectors = [build_connector(config) for config in configs]

    # Each connector's fetch()/write targets its own x_source_connector BRONZE
    # partition dir (see lake/bronze.py) and its own runlog file, so concurrent
    # pulls don't contend — a bounded pool overlaps their network/DuckDB wait
    # instead of summing it. Pool.map preserves input order in its results
    # regardless of which thread finishes first, so the zip below stays correct.
    with ThreadPoolExecutor(max_workers=_max_workers(len(connectors))) as pool:
        outcomes = list(
            pool.map(
                lambda conn: run_connector(
                    conn, window, run_id, on_progress, full_refresh=full_refresh
                ),
                connectors,
            )
        )

    total = 0
    failed: list[str] = []
    succeeded_connectors: list[Connector] = []
    for conn, outcome in zip(connectors, outcomes, strict=True):
        if outcome.ok:
            total += outcome.rows
            succeeded_connectors.append(conn)
        else:
            failed.append(outcome.name)
            logger.error("connector_failed", connector=outcome.name, detail=outcome.detail)

    _run_supplemental(
        window,
        succeeded_connectors,
        on_progress,
    )

    # The layer boundary is deliberate: all selected connectors finish their
    # BRONZE writes before SILVER is evaluated and GOLD is published. A failed
    # connector's window is left as it was before this run (bronze.write_window
    # re-purges on error, never leaving a partial write), while a successful
    # connector contributes its fresh partitions. One full, idempotent transform
    # therefore publishes one coherent view of everything available in BRONZE.
    if not no_transform and succeeded_connectors:
        published = build_gold()
        logger.info("transform_done", gold_views=published, phase="bronze_complete")
    logger.info(
        "ingest_complete",
        connectors=len(connectors),
        succeeded=len(succeeded_connectors),
        failed=len(failed),
        rows=total,
    )
    if failed:
        raise IngestError(failed)
    return total


def _run_supplemental(
    window: IngestWindow,
    connectors: list[Connector],
    on_progress: ProgressCallback | None = None,
) -> None:
    """Run supplemental telemetry under one global concurrency cap.

    Driver health and policy configuration run first because they have a short,
    predictable latency. The remaining planes are still independent and concurrent.
    All of these writes stay in BRONZE until the single transform at the end of
    :func:`run_ingest`.
    """
    priority_phases = (
        lambda: _run_driver_health(window, connectors, max_workers=1),
        lambda: _run_policy_config(window, connectors, max_workers=1),
    )
    with ThreadPoolExecutor(max_workers=_max_workers(len(priority_phases))) as pool:
        list(pool.map(lambda phase: phase(), priority_phases))

    phases = (
        lambda: _run_efficiency(window, connectors, on_progress, max_workers=1),
        lambda: _run_ai_usage(window, connectors, max_workers=1),
        lambda: _run_storage_locations(window, connectors, max_workers=1),
        lambda: _run_compute_instances(window, connectors, max_workers=1),
    )
    with ThreadPoolExecutor(max_workers=_max_workers(len(phases))) as pool:
        # Consume results so an unexpected programmer error keeps the former fail-fast
        # behavior. Connector/network failures remain contained in each helper.
        list(pool.map(lambda phase: phase(), phases))


def _run_efficiency(
    window: IngestWindow,
    connectors: list[Connector],
    on_progress: ProgressCallback | None = None,
    *,
    max_workers: int | None = None,
) -> int:
    """Pull efficiency records for every connector that exposes them (concurrently,
    bounded — see :func:`_max_workers`), then write them all in one call. Best-effort:
    a per-connector pull failure is logged and skipped, never raised — a connector
    whose *cost* pull already reported "done" (see :func:`run_connector`) otherwise
    looks fully synced even though its efficiency/waste telemetry silently never
    landed (the whole payload, for a connector like Redshift whose cost pull is a
    deliberate no-op). ``on_progress`` — same callback ``run_connector`` uses for the
    cost pull's start/done/failed events — gets an ``efficiency_done``/
    ``efficiency_failed`` event per connector here too, so a caller (the CLI, the
    dashboard's sync log) can tell the difference between "cost pull done" and
    "this connector is actually finished."

    The single combined write isn't just for thread-safety — ``write_efficiency``
    purges its target provider/month partitions wholesale before writing, and more
    than one connector can share a ``provider_name`` (``aws_focus`` and ``redshift``
    both emit "AWS"). Writing per-connector, even run one at a time, means the second
    connector's write purges and silently drops the first connector's rows. Gathering
    every connector's records first and writing once is the actual fix; running the
    fetches concurrently is free on top of it. That batching only affects when
    records hit disk, not when a connector's own pull is known to have succeeded or
    failed — the progress event fires as soon as *that* is known, not after the
    later combined write.
    """

    def _pull(connector: Connector) -> list[EfficiencyRecord]:
        name = connector.name
        try:
            records = list(connector.fetch_efficiency(window))
        except Exception as exc:  # noqa: BLE001 - secondary signal; never block ingest
            logger.warning("efficiency_pull_failed", connector=name, error=str(exc))
            if on_progress:
                on_progress("efficiency_failed", name, 0)
            return []
        if records:
            logger.info("efficiency_fetched", connector=name, rows=len(records))
        if on_progress:
            on_progress("efficiency_done", name, len(records))
        return records

    with ThreadPoolExecutor(max_workers=max_workers or _max_workers(len(connectors))) as pool:
        all_records = [record for batch in pool.map(_pull, connectors) for record in batch]

    if not all_records:
        return 0
    written = metrics.write_efficiency(window, all_records)
    logger.info("efficiency_written", rows=written)
    return written


def _run_driver_health(
    window: IngestWindow, connectors: list[Connector], *, max_workers: int | None = None
) -> int:
    """Pull driver-health records for every connector that exposes them (concurrently,
    bounded), then write them all in one call. Best-effort, same never-block-cost-
    ingest guarantee as :func:`_run_efficiency` — and the same purge-before-write
    reasoning for merging into a single write (only one connector emits these today,
    but the fix is the same shape and costs nothing extra to apply now).
    """

    def _pull(connector: Connector) -> list[DriverHealthRecord]:
        name = connector.name
        try:
            records = list(connector.fetch_driver_health(window))
        except Exception as exc:  # noqa: BLE001 - secondary signal; never block ingest
            logger.warning("driver_health_pull_failed", connector=name, error=str(exc))
            return []
        if records:
            logger.info("driver_health_fetched", connector=name, rows=len(records))
        return records

    with ThreadPoolExecutor(max_workers=max_workers or _max_workers(len(connectors))) as pool:
        all_records = [record for batch in pool.map(_pull, connectors) for record in batch]

    if not all_records:
        return 0
    written = driver_health.write_driver_health(window, all_records)
    logger.info("driver_health_written", rows=written)
    return written


def _run_policy_config(
    window: IngestWindow, connectors: list[Connector], *, max_workers: int | None = None
) -> int:
    def _pull(connector: Connector) -> list[RedshiftPolicyConfigRecord]:
        try:
            return list(connector.fetch_policy_config(window))
        except Exception as exc:  # noqa: BLE001
            logger.warning("policy_config_pull_failed", connector=connector.name, error=str(exc))
            return []

    with ThreadPoolExecutor(max_workers=max_workers or _max_workers(len(connectors))) as pool:
        records = [record for batch in pool.map(_pull, connectors) for record in batch]
    return redshift_policy_config.write(window, records)


def _run_ai_usage(
    window: IngestWindow, connectors: list[Connector], *, max_workers: int | None = None
) -> int:
    """Pull AI serving-usage records for every connector that exposes them (concurrently,
    bounded), then write them all in one call. Best-effort, same never-block-cost-ingest
    guarantee and the same single-write merge as :func:`_run_driver_health` (the writer
    purges partitions wholesale, so two writes would have the second erase the first).

    Measurement only: these rows carry token/request counts and no dollar figure — the
    endpoint's spend stays canonical in the FOCUS plane and is joined in GOLD.
    """

    def _pull(connector: Connector) -> list[AiUsageRecord]:
        name = connector.name
        try:
            records = list(connector.fetch_ai_usage(window))
        except Exception as exc:  # noqa: BLE001 - secondary signal; never block ingest
            logger.warning("ai_usage_pull_failed", connector=name, error=str(exc))
            return []
        if records:
            logger.info("ai_usage_fetched", connector=name, rows=len(records))
        return records

    with ThreadPoolExecutor(max_workers=max_workers or _max_workers(len(connectors))) as pool:
        all_records = [record for batch in pool.map(_pull, connectors) for record in batch]

    if not all_records:
        return 0
    written = ai_usage.write_ai_usage(window, all_records)
    logger.info("ai_usage_written", rows=written)
    return written


def _run_storage_locations(
    window: IngestWindow, connectors: list[Connector], *, max_workers: int | None = None
) -> int:
    """Pull each platform's cloud object-storage location map (concurrently, bounded),
    then write it all in one call. Best-effort, same never-block-cost-ingest guarantee
    and the same single-write merge as :func:`_run_driver_health`.

    Metadata only — no cost. This is what lets the AWS S3 bill be labelled with the
    Databricks storage behind it (see ``docs/design/backing-storage.md``); the two are
    never summed into one figure.

    ``window`` is passed through for hook uniformity but the hook ignores it (Unity
    Catalog exposes only current state), and the writer is window-free for the same
    reason — see ``lake.storage_locations.write_storage_locations``.
    """

    def _pull(connector: Connector) -> list[StorageLocationRecord]:
        name = connector.name
        try:
            records = list(connector.fetch_storage_locations(window))
        except Exception as exc:  # noqa: BLE001 - secondary signal; never block ingest
            logger.warning("storage_locations_pull_failed", connector=name, error=str(exc))
            return []
        if records:
            logger.info("storage_locations_fetched", connector=name, rows=len(records))
        return records

    with ThreadPoolExecutor(max_workers=max_workers or _max_workers(len(connectors))) as pool:
        all_records = [record for batch in pool.map(_pull, connectors) for record in batch]

    if not all_records:
        return 0
    written = storage_locations.write_storage_locations(all_records)
    logger.info("storage_locations_written", rows=written)
    return written


def _run_compute_instances(
    window: IngestWindow, connectors: list[Connector], *, max_workers: int | None = None
) -> int:
    """Pull each platform's cloud-compute-instance membership map (concurrently,
    bounded), then write it all in one call. Best-effort, same never-block-cost-ingest
    guarantee and the same single-write merge as :func:`_run_driver_health`.

    Metadata only — no cost. This is what lets the AWS EC2 bill be labelled with the
    Databricks cluster behind it (see ``docs/design/backing-compute.md``); the two are
    never summed into one figure.

    Unlike :func:`_run_storage_locations`, ``window`` is genuinely honored by both the
    hook and the writer — ``system.compute.node_timeline`` reports bounded historical
    activity, not present-tense state (see
    ``lake.compute_instances.write_compute_instances``).
    """

    def _pull(connector: Connector) -> list[ComputeInstanceRecord]:
        name = connector.name
        try:
            records = list(connector.fetch_compute_instances(window))
        except Exception as exc:  # noqa: BLE001 - secondary signal; never block ingest
            logger.warning("compute_instances_pull_failed", connector=name, error=str(exc))
            return []
        if records:
            logger.info("compute_instances_fetched", connector=name, rows=len(records))
        return records

    with ThreadPoolExecutor(max_workers=max_workers or _max_workers(len(connectors))) as pool:
        all_records = [record for batch in pool.map(_pull, connectors) for record in batch]

    if not all_records:
        return 0
    written = compute_instances.write_compute_instances(window, all_records)
    logger.info("compute_instances_written", rows=written)
    return written
