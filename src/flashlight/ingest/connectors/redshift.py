"""Redshift connector — efficiency/waste telemetry only, no cost pull.

Redshift billing already flows through ``aws_focus`` (AWS Data Exports FOCUS
carries Redshift's own SKUs under ``ServiceName="Amazon Redshift"``/``"Amazon
Redshift Spectrum"``) — pulling it again here would double-count. So ``fetch()``
yields nothing; the real work is ``fetch_efficiency()``, which pulls Redshift-
native telemetry the billing export can't carry:

  * Query/WLM/concurrency-scaling activity, via the Redshift Data API (IAM-based,
    no persistent DB credentials) running the vendored ``redshift_efficiency.sql``
    against system tables (STL_*/SVL_*/SVCS_*).
  * A dollar breakdown by cost subcategory (compute / concurrency-scaling / storage
    / Spectrum scan), read from the **already-ingested aws_focus BRONZE rows** for
    this window — not a second AWS API call. ``aws_focus`` already stamps
    ``x_cost_subcategory`` on every Redshift row (via
    ``aws_focus._classify_redshift_cost_category``), and ``ingest/runner.py`` runs
    every connector's ``fetch()`` (writing BRONZE) to completion before any
    connector's ``fetch_efficiency()`` runs — so by the time this executes,
    ``aws_focus``'s Redshift rows for this same window are already on disk. This
    connector's whole premise ("Redshift cost already flows through aws_focus", see
    ``fetch()`` below) means aws_focus being enabled isn't an extra dependency, it's
    already required. Real per-resource FOCUS attribution, not Cost Explorer's
    account-wide ``UsageType`` text match — and one less IAM permission
    (``ce:GetCostAndUsage``) this connector needs.
  * Reserved-node vs on-demand coverage, via ``describe_reserved_nodes``/
    ``describe_clusters``.
  * Table inventory (size/skew/encoding/usage/owner), via SVV_TABLE_INFO + STL_SCAN +
    PG_TABLES (for ``tableowner``) — a generous cap (not a small "top interesting N"),
    since the waste rules (``redshift_table_unused`` etc.) do the real filtering
    downstream and the dashboard leaderboard already paginates. A small size-based
    cut would silently exclude the tables that matter most for "unfavorable
    inventory": a completely unused table is very often a *small* one.
  * Per-external-table Spectrum scan usage, via SVL_S3QUERY_SUMMARY — which table is
    actually driving the cluster-level Spectrum scan $ above, unpriced (that $ figure
    already carries the real cost; this is visibility, not a second estimate).

All of the above run over the Data API by default (no network path to the cluster
needed — AWS proxies the call internally). If ``RedshiftConfig.bastion_host`` is set
(a provisioned cluster locked down in a private VPC where even the Data API isn't
reachable/allowed), the same queries instead run over a direct SQL connection through
an SSH tunnel — see ``_bastion_connection``. If ``bastion_host`` is unset but the
cluster is reachable directly (just without IAM set up), they run over a direct SQL
connection with no tunnel instead — see ``_direct_connection``. Either way,
``db_password_env`` (if set) authenticates that SQL connection with a static
Redshift DB password instead of the default IAM-temp-credential flow. One
connection is opened for the whole ``fetch_efficiency()`` pull and reused across
all seven queries, rather than reconnecting per query. Both SQL paths need the
``redshift-bastion`` extra (``pip install "getflashlight[redshift-bastion]"``),
lazily imported so the default install doesn't need it.
"""

from __future__ import annotations

import hashlib
import json
import time
import warnings
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import AbstractContextManager, contextmanager, nullcontext
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from flashlight.core.exceptions import ConnectorError
from flashlight.core.logging import get_logger
from flashlight.efficiency.model import EfficiencyRecord, EntityType
from flashlight.focus.model import FocusRecord
from flashlight.ingest._redshift_service_names import REDSHIFT_SERVICE_NAMES
from flashlight.ingest.base import Connector, IngestWindow
from flashlight.ingest.config import RedshiftConfig, aws_client, effective_connector_name, env
from flashlight.lake import duck as lake_duck
from flashlight.lake import paths as lake_paths
from flashlight.lake.driver_health_schema import DriverHealthRecord
from flashlight.lake.redshift_policy_config_schema import RedshiftPolicyConfigRecord

logger = get_logger(__name__)

_EFFICIENCY_QUERY_PATH = Path(__file__).parent / "sql" / "redshift_efficiency.sql"
_QUERY_PATTERN_QUERY_PATH = Path(__file__).parent / "sql" / "redshift_query_pattern_metrics.sql"
_USER_ACTIVITY_QUERY_PATH = Path(__file__).parent / "sql" / "redshift_user_activity.sql"
_SPECTRUM_TABLE_QUERY_PATH = Path(__file__).parent / "sql" / "redshift_spectrum_table_usage.sql"
_DRIVER_HEALTH_QUERY_PATH = Path(__file__).parent / "sql" / "redshift_driver_health.sql"
_TERMINAL_STATES = {"FINISHED", "ABORTED", "FAILED"}

# Floor/cap for the query-pattern pull — a cluster can have thousands of distinct query
# shapes; this is a triage signal, not an exhaustive audit (same bounded-pool reasoning
# as the table inventory below). ponytail: fixed constants, not per-connector config —
# revisit if a workload needs a different floor/cap than a general OLAP cluster.
_QUERY_PATTERN_MIN_DURATION_SECS = 30
_QUERY_PATTERN_TOP_N = 200

# Same bounded-pool reasoning, keyed to exec time so the heaviest users are never
# dropped even on a cluster with many distinct DB logins (one per job/service account).
_USER_ACTIVITY_TOP_N = 50

# Bounds every ThreadPoolExecutor this connector opens for its OWN internal query
# fan-out (not ingest/runner.py's outer per-connector pool) — same reasoning as
# databricks.py's _TABLE_INVENTORY_CONCURRENCY: this connector's own concurrency
# must not recreate the WLM contention the fan-out is meant to fix. 3 covers the
# widest fan-out used here (query_patterns/user_activity/spectrum_table_usage, and
# separately table_inventory/table_usage/table_owner).
_EFFICIENCY_CONCURRENCY = 3

# Detailed per-user/pattern/Spectrum telemetry is useful only when there is enough
# retained query history to be representative.  The cluster-level row still records
# the partial measurement, but skipping these three broad system-view queries avoids
# spending minutes producing a misleadingly short sample.
_MIN_DETAIL_ACTIVITY_COVERAGE_DAYS = 14

# SVV_TABLE_INFO is a present-tense catalog snapshot and was the slowest query in the
# parallel table lane.  Reusing a day-old snapshot keeps normal ingests from sorting
# the full catalog every time while still refreshing table shape/maintenance evidence
# daily.  Recent STL_SCAN usage deliberately remains live and is never cached.
_TABLE_INVENTORY_CACHE_TTL = timedelta(days=1)

# Table-access history, joined to the table inventory by table_id (STL_SCAN.tbl ==
# SVV_TABLE_INFO.table_id, same join key the runbook's table_usage load script uses).
# No :start_date filter — STL_SCAN's own retention (typically 2-5 days unless the
# customer exports STL logs) already bounds this; filtering further would only lose
# signal, not gain honesty.
_TABLE_USAGE_SQL = """
-- Recent retained workload only.  STL_SCAN does not include concurrency-scaling
-- queries, so these figures diagnose main-cluster table compute; they must never be
-- presented as an allocation of the full cluster or concurrency-scaling bill.
WITH scans AS (
    SELECT
        s.query,
        s.tbl AS table_id,
        sum(greatest(coalesce(s.bytes, 0), 0)) AS scan_bytes,
        sum(greatest(coalesce(s.rows_pre_filter, 0), 0)) AS rows_pre_filter,
        sum(greatest(coalesce(s.rows, 0), 0)) AS rows_returned,
        max(s.starttime) AS last_access_at
    FROM stl_scan s
    WHERE s.userid > 1
      AND s.perm_table_name NOT IN ('Internal Worktable', 'S3', 'Runtime Filter')
    GROUP BY s.query, s.tbl
), per_query AS (
    SELECT
        query,
        sum(scan_bytes) AS total_scan_bytes,
        sum(rows_pre_filter) AS total_rows_pre_filter,
        count(*) AS table_count
    FROM scans
    GROUP BY query
), table_workload AS (
    SELECT
        s.table_id,
        count(DISTINCT s.query) AS query_count,
        max(s.last_access_at) AS last_access_at,
        sum(s.scan_bytes) AS scan_bytes,
        sum(s.rows_pre_filter) AS rows_pre_filter,
        sum(s.rows_returned) AS rows_returned,
        sum(coalesce(w.total_exec_time, 0) * CASE
            WHEN p.total_scan_bytes > 0 THEN s.scan_bytes::DOUBLE PRECISION / p.total_scan_bytes
            WHEN p.total_rows_pre_filter > 0
                THEN s.rows_pre_filter::DOUBLE PRECISION / p.total_rows_pre_filter
            ELSE 1.0 / nullif(p.table_count, 0)
        END) / 1000000.0 AS weighted_exec_seconds
    FROM scans s
    JOIN per_query p USING (query)
    LEFT JOIN stl_wlm_query w USING (query)
    GROUP BY s.table_id
)
SELECT * FROM table_workload
"""

# Cap on tables to inventory — a pathological-catalog safety valve, NOT a "top
# interesting N" curation cut. The waste rules (redshift_table_unused,
# redshift_table_maintenance_stale, redshift_stale_compression_encoding) already do
# the real filtering downstream, and the dashboard leaderboard already paginates —
# so this stays generous. A size-based top-50 would have silently excluded exactly
# the tables "unfavorable inventory" cares about most: a completely unused table is
# very often small, not large.
_TABLE_INVENTORY_TOP_N = 5000
_TABLE_INVENTORY_SQL = f"""
SELECT table_id, database, "schema", "table", encoded, diststyle, size, unsorted,
       stats_off, tbl_rows
FROM svv_table_info
ORDER BY size DESC
LIMIT {_TABLE_INVENTORY_TOP_N}
"""

# Table owner — a separate query, not a JOIN onto svv_table_info: PG_TABLES is a
# leader-node-only catalog view, and Redshift rejects a single query that mixes a
# leader-node-only object with a compute-node-distributed one ("Specified types or
# functions ... not supported on Redshift tables"), same reasoning as keeping
# _TABLE_USAGE_SQL (STL_SCAN) a separate query merged in Python below.
_TABLE_OWNER_SQL = """
SELECT schemaname, tablename, tableowner
FROM pg_tables
"""

# The cheapest possible read of STL_QUERY's retention floor — a lone MIN(), no
# joins, no window predicate, no percentile sort. Run before the full
# cluster_activity query (redshift_efficiency.sql: a 3-way join + two
# percentile_cont() calls) so an unmeasurable window skips that query's real cost
# too — on a busy production cluster it was observed taking 400s+ even though its
# own WHERE-filtered CTEs matched zero rows (STL_* system views aren't sorted/
# zone-mapped like a regular user table, so filter selectivity doesn't reliably
# translate into a cheap plan). See fetch_efficiency.
_EARLIEST_RETAINED_SQL = "SELECT min(starttime) AS earliest_retained_query_ts FROM stl_query"


def _opt_float(value: object) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _opt_int(value: object) -> int | None:
    f = _opt_float(value)
    return None if f is None else int(f)


def _opt_date(value: object) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):  # check before `date` — datetime is itself a subclass
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _activity_unmeasurable(window_end: date, earliest_retained: date | None) -> bool:
    """True when STL_QUERY's retention doesn't reach *any* part of the window — every
    row that would answer "activity in this window" has already rolled off (STL_*
    tables typically keep only a handful of days), so ``count(*) == 0`` here is
    indistinguishable from "genuinely idle" and gets nulled out rather than guessed.

    Retention reaching only *partway* into the window (the common case for a
    calendar-month ingest window against a multi-day-retention system table) is NOT
    this case — the retained days carry real, measured activity and are used as-is
    (see :func:`_activity`'s ``activity_measured_since``), not discarded just because
    they don't cover the window's earlier days too.
    """
    return earliest_retained is None or earliest_retained > window_end


def _unmeasurable_activity() -> dict[str, Any]:
    """The activity dict for a window entirely past STL_QUERY's retention.

    Every field the full cluster_activity query's joins/percentiles would have
    produced is NULL when their source CTEs are empty (see
    redshift_efficiency.sql) — so this is the exact result, not an approximation,
    for the case where the cheap :data:`_EARLIEST_RETAINED_SQL` probe already
    proved the window is unmeasurable and the real query is skipped entirely.
    """
    return {
        "query_count": None,
        "wlm_queue_wait_ms_p95": None,
        "wlm_queue_wait_ms_p99": None,
        "wlm_wait_to_exec_ratio": None,
        "disk_spill_query_count": None,
        "concurrency_scaling_active_seconds": None,
        "activity_measured_since": None,
        "activity_window_unmeasurable": True,
    }


class RedshiftConnector(Connector):
    name = "redshift"
    cost_pull_note = "cost flows through aws_focus — efficiency telemetry only"

    def __init__(self, config: RedshiftConfig) -> None:
        self._config = config
        # Instance-level, shadowing the class constant above — see aws_focus.py's
        # AwsFocusConnector.__init__ for why (BRONZE partitioning stays distinct
        # across multiple Redshift-cluster connections).
        self.name = effective_connector_name(config)
        self._data = aws_client(
            "redshift-data",
            region=config.region,
            profile=config.aws_profile,
            access_key_env=config.access_key_env,
            secret_key_env=config.secret_key_env,
        )
        self._redshift = aws_client(
            "redshift",
            region=config.region,
            profile=config.aws_profile,
            access_key_env=config.access_key_env,
            secret_key_env=config.secret_key_env,
        )

    def fetch(self, window: IngestWindow) -> Iterator[FocusRecord]:
        """No-op: Redshift cost already flows through ``aws_focus``. See module docstring."""
        return iter(())

    def fetch_driver_health(self, window: IngestWindow) -> Iterator[DriverHealthRecord]:
        """Yield client-driver use from Redshift's connection log.

        This is deliberately separate from the efficiency pull: connection-log access
        is superuser-only, while the rest of the optimization telemetry can be useful
        with narrower system-table permissions.  The ingest runner treats it as the
        same best-effort, non-cost signal as Databricks driver health.
        """
        sql = (
            _DRIVER_HEALTH_QUERY_PATH.read_text()
            .replace(":start_date", f"'{window.start}'")
            .replace(":end_date", f"'{window.end}'")
        )
        # Match fetch_efficiency's connection selection.  A private cluster that
        # requires a bastion cannot reach the Data API merely because this is a
        # smaller, independent pull.
        if self._config.bastion_host is not None:
            mode = "bastion_tunnel"
        elif self._config.db_password_env is not None:
            mode = "direct_sql"
        else:
            mode = "data_api"
        if mode == "data_api":
            rows = self._execute(sql, name="driver_health")
        else:
            with self._lane_connection_factory(mode) as lane_conn, lane_conn() as conn:
                rows = self._execute(sql, conn, name="driver_health")
        for row in rows:
            charge_month = _opt_date(row.get("charge_month"))
            if charge_month is None:
                continue
            yield DriverHealthRecord(
                provider_name="AWS",
                charge_month=charge_month,
                client_driver=str(row["client_driver"]) if row.get("client_driver") else None,
                client_application=(
                    str(row["client_application"]) if row.get("client_application") else None
                ),
                executed_by=str(row["executed_by"]) if row.get("executed_by") else None,
                query_count=_opt_int(row.get("query_count")) or 0,
                x_source_connector=self.name,
            )

    def fetch_policy_config(self, window: IngestWindow) -> Iterator[RedshiftPolicyConfigRecord]:
        """Snapshot policy-relevant control-plane configuration into typed Bronze."""
        try:
            clusters = self._redshift.describe_clusters(
                ClusterIdentifier=self._config.cluster_identifier
            ).get("Clusters", [])
        except Exception as exc:  # noqa: BLE001
            raise ConnectorError(
                self.name, f"describe_clusters for policy config failed: {exc}"
            ) from exc
        if not clusters:
            return
        cluster = clusters[0]
        parameter_groups = cluster.get("ClusterParameterGroups") or []
        group_name = parameter_groups[0].get("ParameterGroupName") if parameter_groups else None
        require_ssl: bool | None = None
        if group_name:
            try:
                params = self._redshift.describe_cluster_parameters(
                    ParameterGroupName=group_name
                ).get("Parameters", [])
                value = next(
                    (
                        p.get("ParameterValue")
                        for p in params
                        if p.get("ParameterName") == "require_ssl"
                    ),
                    None,
                )
                require_ssl = None if value is None else str(value).lower() == "true"
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "redshift_require_ssl_unavailable", connector=self.name, error=str(exc)
                )
        tags = cluster.get("Tags")
        yield RedshiftPolicyConfigRecord(
            snapshot_month=window.end.replace(day=1),
            cluster_id=str(cluster.get("ClusterIdentifier") or self._config.cluster_identifier),
            cluster_name=cluster.get("ClusterIdentifier"),
            encrypted=cluster.get("Encrypted"),
            publicly_accessible=cluster.get("PubliclyAccessible"),
            enhanced_vpc_routing=cluster.get("EnhancedVpcRouting"),
            automated_snapshot_retention_days=_opt_int(cluster.get("AutomatedSnapshotRetentionPeriod")),
            require_ssl=require_ssl,
            tag_count=len(tags) if isinstance(tags, list) else None,
            x_source_connector=self.name,
        )

    def fetch_efficiency(self, window: IngestWindow) -> Iterator[EfficiencyRecord]:
        month = window.start.replace(day=1)
        entity_id = self._config.cluster_identifier

        if self._config.bastion_host is not None:
            mode = "bastion_tunnel"
        elif self._config.db_password_env is not None:
            mode = "direct_sql"
        else:
            mode = "data_api"
        logger.info(
            "redshift_efficiency_start",
            mode=mode,
            entity_id=entity_id,
            window_start=str(window.start),
            window_end=str(window.end),
        )

        cost = self._cost_breakdown(window, entity_id)
        reserved = self._reserved_node_coverage()
        # The activity chain (probe -> cluster_activity -> gated query_patterns/
        # user_activity/spectrum_table_usage) and the table-inventory chain
        # (table_inventory/table_usage/table_owner, unconditional and independent of
        # the activity gate) don't depend on each other — see _run_lanes. Each lane
        # opens its own connection(s) via lane_conn(), a tunneled connection if
        # `bastion_host` is set, a direct (no-tunnel) connection if `db_password_env`
        # is set instead, or a no-op yielding None for the Data API path, where
        # every call is already an independently authenticated request with no
        # connection to share.
        with self._lane_connection_factory(mode) as lane_conn:
            activity, activity_records, table_records = self._run_lanes(
                window, entity_id, month, cost, lane_conn
            )

        allocation_available = not (
            activity.get("activity_window_unmeasurable") or activity.get("activity_measured_since")
        )
        table_records, spectrum_allocated = self._allocate_spectrum_cost(
            table_records, cost.get("spectrum_scan"), allocation_available
        )

        cause: dict[str, Any] = {
            "compute_cost": cost.get("compute"),
            "concurrency_scaling_cost": cost.get("concurrency_scaling"),
            "storage_cost": cost.get("storage"),
            # The cluster-level total is only emitted where table allocation could
            # not safely reconcile it. Otherwise the per-table rows carry the same
            # invoice charge exactly once.
            "spectrum_scan_cost": None if spectrum_allocated else cost.get("spectrum_scan"),
            "wlm_queue_wait_ms_p95": activity.get("wlm_queue_wait_ms_p95"),
            "wlm_queue_wait_ms_p99": activity.get("wlm_queue_wait_ms_p99"),
            "wlm_wait_to_exec_ratio": activity.get("wlm_wait_to_exec_ratio"),
            "disk_spill_query_count": activity.get("disk_spill_query_count"),
            # Reuses the same cause_detail key Databricks sql_warehouse rules
            # already unpack (see waste_rules.py's `e` CTE) — the disk-spill
            # rule's denominator.
            "query_count": activity.get("query_count"),
            "concurrency_scaling_active_seconds": activity.get(
                "concurrency_scaling_active_seconds"
            ),
            "on_demand_node_hours": reserved.get("on_demand_node_hours"),
            "reserved_node_hours": reserved.get("reserved_node_hours"),
            # Only carried when True — a clean cause_detail should call out the
            # unusual case, not assert the normal one on every record.
            "activity_window_unmeasurable": activity.get("activity_window_unmeasurable")
            or None,
            # Only carried on a partial window (retention didn't reach back to
            # window.start) — the caveat "idle"/disk-spill/etc. need to avoid
            # implying full-window coverage over what's really a shorter measured
            # span.
            "activity_measured_since": activity.get("activity_measured_since"),
        }
        total_cost = sum(v for v in cost.values() if v is not None)
        yield EfficiencyRecord(
            provider_name="AWS",
            charge_month=month,
            # Reused, not a new entity type: a Redshift cluster is
            # SQL-warehouse-shaped (shared compute, cost-attributable per owner,
            # no honest per-entity utilization%) — see EntityType's own
            # docstring and CLAUDE.md's "SQL warehouses have no per-entity
            # utilization" invariant. utilization_pct is deliberately left
            # unset here for the same reason.
            entity_type=EntityType.SQL_WAREHOUSE,
            entity_id=entity_id,
            entity_name=entity_id,
            billed_cost=Decimal(str(total_cost)) if total_cost else Decimal("0"),
            activity_count=activity.get("query_count"),
            cause_detail={k: v for k, v in cause.items() if v is not None},
            x_source_connector=self.name,
        )
        if activity.get("activity_window_unmeasurable"):
            logger.info(
                "redshift_windowed_queries_skipped",
                reason="activity_window_unmeasurable",
            )
        yield from activity_records
        yield from table_records

    def _lane_connection_factory(
        self, mode: str
    ) -> AbstractContextManager[Callable[[], AbstractContextManager[Any]]]:
        """Resolves whatever setup a lane connection needs ONCE (SSH tunnel,
        cluster endpoint, credentials), then yields a zero-arg callable that opens
        ONE fresh connection per call — cheap, since the expensive part already
        happened. Each of ``fetch_efficiency()``'s lanes calls this once per
        connection it needs, safe to do concurrently since every call returns its
        own independent connection object, never a shared one.
        """
        if mode == "bastion_tunnel":
            return self._bastion_lane_connections()
        if mode == "direct_sql":
            return self._direct_lane_connections()
        return nullcontext(lambda: nullcontext(None))

    @contextmanager
    def _bastion_lane_connections(self) -> Iterator[Callable[[], AbstractContextManager[Any]]]:
        with self._bastion_tunnel() as tunnel:
            creds = self._bastion_credentials()
            yield lambda: self._open_sql_connection(
                host="127.0.0.1",
                port=tunnel.local_bind_port,
                user=creds["user"],
                password=creds["password"],
                error_prefix="bastion SQL connection failed",
            )

    @contextmanager
    def _direct_lane_connections(self) -> Iterator[Callable[[], AbstractContextManager[Any]]]:
        try:
            import redshift_connector  # noqa: F401
        except ImportError as exc:
            raise ConnectorError(
                self.name,
                "db_password_env is configured but the 'redshift-bastion' extra "
                "isn't installed (it also provides the direct-connection driver) — "
                'run: pip install "getflashlight[redshift-bastion]"',
            ) from exc

        assert self._config.db_password_env is not None  # caller checks this
        assert self._config.db_user is not None  # RedshiftConfig enforces this
        password = env(self._config.db_password_env)
        if not password:
            raise ConnectorError(
                self.name,
                f"db_password_env={self._config.db_password_env!r} is set but "
                "empty/unset in the environment",
            )
        endpoint = self._cluster_endpoint()
        db_user = self._config.db_user
        yield lambda: self._open_sql_connection(
            host=endpoint["host"],
            port=endpoint["port"],
            user=db_user,
            password=password,
            error_prefix="direct SQL connection failed",
        )

    def _run_lanes(
        self,
        window: IngestWindow,
        entity_id: str,
        month: date,
        cost: dict[str, float],
        lane_conn: Callable[[], AbstractContextManager[Any]],
    ) -> tuple[dict[str, Any], list[EfficiencyRecord], list[EfficiencyRecord]]:
        """Runs the activity chain and the table-inventory chain concurrently — they
        don't depend on each other (see fetch_efficiency's docstring comment). The
        table lane runs on a background thread while the activity lane runs here;
        each opens its own lane_conn() connection(s), so no connection is ever
        touched from more than one thread.
        """
        with ThreadPoolExecutor(max_workers=1) as pool:
            table_future = pool.submit(
                self._run_table_inventory_lane, window, entity_id, month, lane_conn
            )
            activity, activity_records = self._run_activity_lane(
                window, entity_id, month, cost.get("compute", 0.0), lane_conn
            )
            table_records = table_future.result()
        return activity, activity_records, table_records

    def _run_activity_lane(
        self,
        window: IngestWindow,
        entity_id: str,
        month: date,
        compute_cost: float,
        lane_conn: Callable[[], AbstractContextManager[Any]],
    ) -> tuple[dict[str, Any], list[EfficiencyRecord]]:
        # Cheap probe first: a lone MIN() on stl_query, not the full joined
        # percentile query. Lets an unmeasurable window skip cluster_activity's
        # real cost too (see _EARLIEST_RETAINED_SQL), not just the other three
        # windowed queries below.
        with lane_conn() as conn:
            earliest_retained = self._probe_earliest_retained(conn)
            unmeasurable = _activity_unmeasurable(window.end, earliest_retained)
            if unmeasurable:
                logger.warning(
                    "redshift_activity_window_unmeasurable",
                    window_start=str(window.start),
                    window_end=str(window.end),
                    earliest_retained=str(earliest_retained) if earliest_retained else None,
                )
                activity: dict[str, Any] = _unmeasurable_activity()
            else:
                activity = self._activity(window, conn, name="cluster_activity")

        # query_patterns/user_activity/spectrum_table_usage all filter on the same
        # starttime BETWEEN :start_date/:end_date against the same STL_*/SVL_*
        # system tables _activity() already probed above — if that probe found the
        # window entirely past retention (activity_window_unmeasurable), these three
        # are guaranteed empty too. Skipping them turns a ~5min blind rescan into a
        # log line.
        if activity.get("activity_window_unmeasurable"):
            return activity, []

        measured_since = _opt_date(activity.get("activity_measured_since"))
        detail_coverage_days = (
            (window.end - measured_since).days + 1 if measured_since is not None else None
        )
        if (
            detail_coverage_days is not None
            and detail_coverage_days < _MIN_DETAIL_ACTIVITY_COVERAGE_DAYS
        ):
            logger.info(
                "redshift_windowed_detail_queries_skipped",
                reason="partial_activity_window",
                measured_since=str(measured_since),
                coverage_days=detail_coverage_days,
                minimum_coverage_days=_MIN_DETAIL_ACTIVITY_COVERAGE_DAYS,
            )
            return activity, []

        def _patterns() -> list[EfficiencyRecord]:
            with lane_conn() as conn:
                return list(self._fetch_query_patterns(window, entity_id, month, conn))

        def _users() -> list[EfficiencyRecord]:
            with lane_conn() as conn:
                return list(
                    self._fetch_user_activity(window, entity_id, month, compute_cost, conn)
                )

        def _spectrum() -> list[EfficiencyRecord]:
            with lane_conn() as conn:
                return list(self._fetch_spectrum_table_usage(window, entity_id, month, conn))

        with ThreadPoolExecutor(max_workers=_EFFICIENCY_CONCURRENCY) as pool:
            futures = [pool.submit(fn) for fn in (_patterns, _users, _spectrum)]
            records = [rec for f in futures for rec in f.result()]
        return activity, records

    def _run_table_inventory_lane(
        self,
        window: IngestWindow,
        entity_id: str,
        month: date,
        lane_conn: Callable[[], AbstractContextManager[Any]],
    ) -> list[EfficiencyRecord]:
        """table_inventory isn't window-filtered (current catalog state), so it
        always runs, independent of the activity gate above. table_usage/
        table_owner are independent enrichments joined onto table_inventory's rows
        in Python (by :meth:`_build_table_inventory_records`), not each other's
        dependency — so all three run concurrently. One difference from the old
        fully-serial version: previously a table_inventory failure skipped
        table_usage/table_owner too (saving two round trips); now all three are
        already in flight before any failure is known, so a table_inventory
        failure no longer saves that work. Each still degrades independently on
        its own failure, same as before.
        """

        def _inventory() -> list[dict[str, Any]]:
            with lane_conn() as conn:
                try:
                    return self._execute(_TABLE_INVENTORY_SQL, conn, name="table_inventory")
                except ConnectorError as exc:
                    logger.warning("redshift_table_inventory_failed", error=str(exc))
                    return []

        def _usage() -> list[dict[str, Any]]:
            with lane_conn() as conn:
                try:
                    return self._execute(_TABLE_USAGE_SQL, conn, name="table_usage")
                except ConnectorError as exc:
                    logger.warning("redshift_table_usage_failed", error=str(exc))
                    return []

        def _owner() -> list[dict[str, Any]]:
            with lane_conn() as conn:
                try:
                    return self._execute(_TABLE_OWNER_SQL, conn, name="table_owner")
                except ConnectorError as exc:
                    logger.warning("redshift_table_owner_failed", error=str(exc))
                    return []

        cached = self._load_table_inventory_cache()
        if cached is not None:
            rows, owner_rows = cached
            with ThreadPoolExecutor(max_workers=1) as pool:
                usage_rows = pool.submit(_usage).result()
            logger.info(
                "redshift_table_inventory_cache_hit",
                tables=len(rows),
                owners=len(owner_rows),
            )
        else:
            with ThreadPoolExecutor(max_workers=_EFFICIENCY_CONCURRENCY) as pool:
                inv_f = pool.submit(_inventory)
                usage_f = pool.submit(_usage)
                owner_f = pool.submit(_owner)
                rows, usage_rows, owner_rows = inv_f.result(), usage_f.result(), owner_f.result()
            if rows:
                self._store_table_inventory_cache(rows, owner_rows)

        return list(
            self._build_table_inventory_records(
                window, entity_id, month, rows, usage_rows, owner_rows
            )
        )

    def _table_inventory_cache_path(self) -> Path:
        """Stable, filesystem-safe cache location for this cluster's catalog."""
        key = "\x00".join(
            (self._config.region, self._config.database, self._config.cluster_identifier)
        )
        digest = hashlib.sha256(key.encode()).hexdigest()
        return lake_paths.redshift_table_inventory_cache_dir() / f"{digest}.json"

    def _load_table_inventory_cache(
        self,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]] | None:
        path = self._table_inventory_cache_path()
        try:
            age = datetime.now() - datetime.fromtimestamp(path.stat().st_mtime)
            if age > _TABLE_INVENTORY_CACHE_TTL:
                return None
            payload = json.loads(path.read_text())
            rows = payload["inventory"]
            owners = payload["owners"]
            if not isinstance(rows, list) or not isinstance(owners, list):
                raise ValueError("cache payload is not row lists")
            return rows, owners
        except FileNotFoundError:
            return None
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            logger.warning("redshift_table_inventory_cache_ignored", path=str(path), error=str(exc))
            return None

    def _store_table_inventory_cache(
        self, rows: list[dict[str, Any]], owner_rows: list[dict[str, Any]]
    ) -> None:
        path = self._table_inventory_cache_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = path.with_suffix(".tmp")
            payload = json.dumps({"inventory": rows, "owners": owner_rows}, default=str)
            tmp_path.write_text(payload)
            tmp_path.replace(path)
            logger.info(
                "redshift_table_inventory_cache_written", tables=len(rows), owners=len(owner_rows)
            )
        except OSError as exc:
            logger.warning(
                "redshift_table_inventory_cache_write_failed", path=str(path), error=str(exc)
            )

    @staticmethod
    def _allocate_spectrum_cost(
        records: list[EfficiencyRecord], spectrum_cost: float | None, measurable: bool
    ) -> tuple[list[EfficiencyRecord], bool]:
        """Allocate a target-scoped Spectrum invoice charge by scanned bytes.

        The scan telemetry comes from short-retention system tables.  A partial or
        unavailable window cannot honestly divide a whole billing-period charge, so
        this deliberately fails closed and leaves the cluster-level finding in place.
        """
        if not measurable or spectrum_cost is None or spectrum_cost <= 0:
            return records, False
        spectrum_rows = [
            rec
            for rec in records
            if rec.entity_id.find(":spectrum:") != -1
            and isinstance(rec.cause_detail.get("spectrum_scanned_gb"), (int, float))
            and float(rec.cause_detail["spectrum_scanned_gb"]) > 0
        ]
        total_scanned = sum(float(rec.cause_detail["spectrum_scanned_gb"]) for rec in spectrum_rows)
        if not spectrum_rows or total_scanned <= 0:
            return records, False

        allocated: list[EfficiencyRecord] = []
        for rec in records:
            if rec not in spectrum_rows:
                allocated.append(rec)
                continue
            scanned = float(rec.cause_detail["spectrum_scanned_gb"])
            pct = scanned / total_scanned
            cause = {
                **rec.cause_detail,
                "spectrum_allocated_cost": spectrum_cost * pct,
                "spectrum_allocation_pct": pct * 100.0,
                "spectrum_allocation_status": "allocated",
            }
            allocated.append(rec.model_copy(update={"cause_detail": cause}))
        return allocated, True

    def test_connection(self) -> str:
        """Resolves the configured cluster via AWS, then exercises whichever
        connection mode ``RedshiftConfig`` selects with a throwaway ``SELECT 1`` — the
        dashboard's "Test connection" button's one call. Mirrors ``fetch_efficiency``'s
        own mode dispatch below (``bastion_host`` -> SSH tunnel, ``db_password_env`` ->
        direct SQL, else -> Data API) so a pass here means a real sync would use the
        same path successfully.
        """
        cfg = self._config
        endpoint = self._cluster_endpoint()
        resolved = f"cluster resolved to {endpoint['host']}:{endpoint['port']}"

        # ponytail: two describe_clusters calls on this path (here + inside
        # _bastion_connection/_direct_connection) — a manual one-click test, not the
        # hot fetch_efficiency path, so passing the endpoint through isn't worth the
        # signature churn.
        if cfg.bastion_host is not None:
            with self._bastion_connection() as conn:
                self._execute("SELECT 1", conn, name="test_connection")
            return f"{resolved} · SSH tunnel + SQL SELECT 1 succeeded"
        elif cfg.db_password_env is not None:
            with self._direct_connection() as conn:
                self._execute("SELECT 1", conn, name="test_connection")
            return f"{resolved} · direct SQL SELECT 1 succeeded"
        else:
            self._execute("SELECT 1", name="test_connection")
            return f"{resolved} · Data API SELECT 1 succeeded"

    # ── Data API (system-table query) ───────────────────────────────────────
    def _probe_earliest_retained(self, conn: Any = None) -> date | None:
        """The cheapest possible read of STL_QUERY's retention floor — see
        :data:`_EARLIEST_RETAINED_SQL`.
        """
        rows = self._execute(_EARLIEST_RETAINED_SQL, conn, name="earliest_retained_probe")
        if not rows:
            return None
        return _opt_date(rows[0].get("earliest_retained_query_ts"))

    def _activity(
        self, window: IngestWindow, conn: Any = None, *, name: str = "cluster_activity"
    ) -> dict[str, Any]:
        sql = (
            _EFFICIENCY_QUERY_PATH.read_text()
            .replace(":start_date", f"'{window.start}'")
            .replace(":end_date", f"'{window.end + timedelta(days=1)}'")
        )
        rows = self._execute(sql, conn, name=name)
        if not rows:
            return {}
        row = rows[0]
        earliest_retained = _opt_date(row.get("earliest_retained_query_ts"))
        unmeasurable = _activity_unmeasurable(window.end, earliest_retained)
        # Retention reaches into the window but not back to its start — the counts
        # below are real, just for [earliest_retained, window.end] rather than the
        # full requested window. Surfaced so a consumer can caveat "measured since
        # <date>" instead of implying full-window coverage.
        partial = (
            not unmeasurable and earliest_retained is not None and earliest_retained > window.start
        )
        if unmeasurable:
            logger.warning(
                "redshift_activity_window_unmeasurable",
                window_start=str(window.start),
                window_end=str(window.end),
                earliest_retained=str(earliest_retained) if earliest_retained else None,
            )
        elif partial:
            logger.info(
                "redshift_activity_window_partial",
                window_start=str(window.start),
                earliest_retained=str(earliest_retained),
            )
        wait_us_p95 = _opt_float(row.get("wlm_queue_wait_us_p95"))
        wait_us_p99 = _opt_float(row.get("wlm_queue_wait_us_p99"))
        return {
            # count(*)-shaped fields read 0 for "confirmed empty" and "log rolled off
            # entirely" alike — null them out only when retention covers none of the
            # window (can't tell the two apart there). A partial window still has real
            # rows for its retained days, so it's used as-is, not discarded.
            # The wlm_* percentile/ratio fields already return NULL (not 0) from SQL
            # for an empty window, so they don't need the same guard.
            "query_count": None if unmeasurable else _opt_int(row.get("query_count")),
            "wlm_queue_wait_ms_p95": wait_us_p95 / 1000.0 if wait_us_p95 is not None else None,
            "wlm_queue_wait_ms_p99": wait_us_p99 / 1000.0 if wait_us_p99 is not None else None,
            "wlm_wait_to_exec_ratio": _opt_float(row.get("wlm_wait_to_exec_ratio")),
            "disk_spill_query_count": (
                None if unmeasurable else _opt_int(row.get("disk_spill_query_count"))
            ),
            "concurrency_scaling_active_seconds": (
                None if unmeasurable else _opt_float(row.get("concurrency_scaling_active_seconds"))
            ),
            "activity_measured_since": str(earliest_retained) if partial else None,
            "activity_window_unmeasurable": unmeasurable,
        }

    def _fetch_query_patterns(
        self, window: IngestWindow, cluster_id: str, month: date, conn: Any = None
    ) -> Iterator[EfficiencyRecord]:
        """Per-query-pattern runtime/spill/skew distribution — the drill-down the
        single cluster-level row in fetch_efficiency() can't give (which query, not
        just "is the cluster spilling"). Best-effort: logged and skipped on failure,
        same as table inventory below.
        """
        sql = (
            _QUERY_PATTERN_QUERY_PATH.read_text()
            .replace(":start_date", f"'{window.start}'")
            .replace(":end_date", f"'{window.end + timedelta(days=1)}'")
            .replace(":min_duration_secs", str(_QUERY_PATTERN_MIN_DURATION_SECS))
            .replace(":top_n", str(_QUERY_PATTERN_TOP_N))
        )
        try:
            rows = self._execute(sql, conn, name="query_patterns")
        except ConnectorError as exc:
            logger.warning("redshift_query_pattern_metrics_failed", error=str(exc))
            return
        for row in rows:
            qry_md5 = row.get("qry_md5")
            if not qry_md5:
                continue
            cause = {
                "run_count": _opt_int(row.get("run_count")),
                "total_run_min": _opt_float(row.get("total_run_min")),
                "avg_exec_min": _opt_float(row.get("avg_exec_min")),
                "avg_queue_min": _opt_float(row.get("avg_queue_min")),
                "pct_runs_spilling": _opt_float(row.get("pct_runs_spilling")),
                "avg_disk_spill_gb": _opt_float(row.get("avg_disk_spill_gb")),
                "avg_workmem_gb": _opt_float(row.get("avg_workmem_gb")),
                "avg_skew_ratio": _opt_float(row.get("avg_skew_ratio")),
                "max_skew_ratio": _opt_float(row.get("max_skew_ratio")),
                "avg_slices_in_use": _opt_float(row.get("avg_slices_in_use")),
            }
            yield EfficiencyRecord(
                provider_name="AWS",
                charge_month=month,
                entity_type=EntityType.QUERY_PATTERN,
                entity_id=f"{cluster_id}:{qry_md5}",
                entity_name=qry_md5,
                owner_user=row.get("top_user"),
                activity_count=_opt_int(row.get("run_count")),
                cause_detail={k: v for k, v in cause.items() if v is not None},
                x_source_connector=self.name,
            )

    def _fetch_user_activity(
        self,
        window: IngestWindow,
        cluster_id: str,
        month: date,
        compute_cost: float,
        conn: Any = None,
    ) -> Iterator[EfficiencyRecord]:
        """Per-user CPU/scan/spill pressure + duration_share_pct — the latter reuses
        the existing sql_warehouse_user_concentration waste rule for free (see
        redshift_user_activity.sql's header). Best-effort, same failure handling as
        the other efficiency fetches.
        """
        sql = (
            _USER_ACTIVITY_QUERY_PATH.read_text()
            .replace(":start_date", f"'{window.start}'")
            .replace(":end_date", f"'{window.end + timedelta(days=1)}'")
            .replace(":top_n", str(_USER_ACTIVITY_TOP_N))
        )
        try:
            rows = self._execute(sql, conn, name="user_activity")
        except ConnectorError as exc:
            logger.warning("redshift_user_activity_failed", error=str(exc))
            return
        for row in rows:
            username = row.get("username")
            if not username:
                continue
            exec_us = _opt_float(row.get("exec_microseconds")) or 0.0
            # total_exec_microseconds is a window aggregate over ALL users (computed
            # before the SQL's LIMIT truncates to the top N) — so share_pct stays
            # correct relative to the true cluster total, not just the returned rows.
            total_exec_us = _opt_float(row.get("total_exec_microseconds")) or 0.0
            share_pct = (100.0 * exec_us / total_exec_us) if total_exec_us else None
            cpu_us = _opt_float(row.get("cpu_microseconds"))
            cause = {
                "duration_share_pct": share_pct,
                "query_count": _opt_int(row.get("query_count")),
                "warehouse_type": "PROVISIONED",
                "cpu_seconds": cpu_us / 1_000_000.0 if cpu_us is not None else None,
                "exec_seconds": exec_us / 1_000_000.0 if exec_us else None,
                "blocks_read": _opt_int(row.get("blocks_read")),
                "temp_blocks_to_disk": _opt_int(row.get("temp_blocks_to_disk")),
                "scan_rows": _opt_int(row.get("scan_rows")),
                "spectrum_scan_rows": _opt_int(row.get("spectrum_scan_rows")),
                "spectrum_scan_mb": _opt_float(row.get("spectrum_scan_mb")),
                "spill_gb": _opt_float(row.get("spill_gb")),
            }
            yield EfficiencyRecord(
                provider_name="AWS",
                charge_month=month,
                entity_type=EntityType.SQL_WAREHOUSE_USER,
                entity_id=f"{cluster_id}:{username}",
                entity_name=username,
                owner_user=username,
                billed_cost=Decimal(str(compute_cost * (share_pct / 100.0)))
                if share_pct and compute_cost
                else Decimal("0"),
                activity_count=_opt_int(row.get("query_count")),
                cause_detail={k: v for k, v in cause.items() if v is not None},
                x_source_connector=self.name,
            )

    def _build_table_inventory_records(
        self,
        window: IngestWindow,
        cluster_id: str,
        month: date,
        rows: list[dict[str, Any]],
        usage_rows: list[dict[str, Any]],
        owner_rows: list[dict[str, Any]],
    ) -> Iterator[EfficiencyRecord]:
        """Joins the three independently-fetched query results in Python — unchanged
        from the old ``_fetch_table_inventory``, just no longer calling ``_execute``
        inline (that now happens concurrently in :meth:`_run_table_inventory_lane`).
        """
        usage_by_table_id: dict[Any, dict[str, Any]] = {}
        for usage_row in usage_rows:
            table_id = usage_row.get("table_id")
            if table_id is not None:
                usage_by_table_id[table_id] = usage_row
        owner_by_schema_table: dict[tuple[Any, Any], Any] = {}
        for owner_row in owner_rows:
            owner_by_schema_table[(owner_row.get("schemaname"), owner_row.get("tablename"))] = (
                owner_row.get("tableowner")
            )
        total_weighted_exec_seconds = sum(
            _opt_float(row.get("weighted_exec_seconds")) or 0.0 for row in usage_rows
        )
        for row in rows:
            full_name = f"{row.get('database')}.{row.get('schema')}.{row.get('table')}"
            size_mb = _opt_float(row.get("size"))
            usage = usage_by_table_id.get(row.get("table_id"), {})
            query_count = _opt_int(usage.get("query_count"))
            last_access_at = usage.get("last_access_at")
            days_since_last_access = None
            if last_access_at is not None:
                last_access_date = (
                    last_access_at.date()
                    if hasattr(last_access_at, "date")
                    else date.fromisoformat(str(last_access_at)[:10])
                )
                days_since_last_access = (window.end - last_access_date).days
            cause = {
                "encoded": row.get("encoded"),
                "diststyle": row.get("diststyle"),
                "unsorted_pct": _opt_float(row.get("unsorted")),
                "stats_off_pct": _opt_float(row.get("stats_off")),
                "tbl_rows": _opt_int(row.get("tbl_rows")),
                "last_access_date": str(last_access_at) if last_access_at is not None else None,
                "days_since_last_access": days_since_last_access,
                # A query's execution time is split across every table it scanned by
                # bytes (then pre-filter rows when bytes are zero). This is evidence
                # of table-associated main-cluster workload, not a per-table bill.
                "table_weighted_exec_seconds": _opt_float(usage.get("weighted_exec_seconds")),
                "table_compute_share_pct": (
                    100.0
                    * (_opt_float(usage.get("weighted_exec_seconds")) or 0.0)
                    / total_weighted_exec_seconds
                    if total_weighted_exec_seconds > 0
                    else None
                ),
                "table_scan_gb": (
                    (_opt_float(usage.get("scan_bytes")) or 0.0) / 1024.0 / 1024 / 1024
                    if usage.get("scan_bytes") is not None
                    else None
                ),
                "table_rows_pre_filter": _opt_int(usage.get("rows_pre_filter")),
                "table_rows_returned": _opt_int(usage.get("rows_returned")),
            }
            yield EfficiencyRecord(
                provider_name="AWS",
                charge_month=month,
                entity_type=EntityType.TABLE,
                # Cluster-prefixed, same as query_pattern/sql_warehouse_user above — a
                # table's db.schema.table name alone isn't unique across clusters, and
                # the prefix is what lets the dashboard filter a cluster's own tables.
                entity_id=f"{cluster_id}:{full_name}",
                entity_name=full_name,
                owner_user=owner_by_schema_table.get((row.get("schema"), row.get("table"))),
                native_quantity=size_mb,
                native_unit="MB",
                activity_count=query_count,
                cause_detail={k: v for k, v in cause.items() if v is not None},
                x_source_connector=self.name,
            )

    def _fetch_spectrum_table_usage(
        self, window: IngestWindow, cluster_id: str, month: date, conn: Any = None
    ) -> Iterator[EfficiencyRecord]:
        """Which external table is driving Spectrum scan spend, per table — the
        raw usage that is later allocated from an already-ingested, target-scoped
        Spectrum invoice charge when the activity window is complete. Best-effort,
        same failure handling as the other efficiency fetches.
        """
        sql = (
            _SPECTRUM_TABLE_QUERY_PATH.read_text()
            .replace(":start_date", f"'{window.start}'")
            .replace(":end_date", f"'{window.end + timedelta(days=1)}'")
        )
        try:
            rows = self._execute(sql, conn, name="spectrum_table_usage")
        except ConnectorError as exc:
            logger.warning("redshift_spectrum_table_usage_failed", error=str(exc))
            return
        for row in rows:
            table_name = row.get("external_table_name")
            if not table_name:
                continue
            cause = {
                "spectrum_scan_count": _opt_int(row.get("scan_count")),
                "spectrum_scanned_gb": _opt_float(row.get("scanned_gb")),
                "spectrum_returned_gb": _opt_float(row.get("returned_gb")),
            }
            yield EfficiencyRecord(
                provider_name="AWS",
                charge_month=month,
                entity_type=EntityType.TABLE,
                entity_id=f"{cluster_id}:spectrum:{table_name}",
                entity_name=table_name,
                activity_count=_opt_int(row.get("scan_count")),
                cause_detail={k: v for k, v in cause.items() if v is not None},
                x_source_connector=self.name,
            )

    def _execute(self, sql: str, conn: Any = None, *, name: str = "query") -> list[dict[str, Any]]:
        """Run one query — over ``conn`` (an open bastion connection, reused across
        the whole ``fetch_efficiency()`` pull) if given, else the Data API. Every
        query this connector issues routes through here, so this is the one place
        that needs to log what ran and how long it took.
        """
        started = time.monotonic()
        via = "sql" if conn is not None else "data_api"
        logger.info("redshift_query_start", name=name, via=via)
        try:
            if conn is not None:
                with conn.cursor() as cur:
                    cur.execute(sql)
                    cols = [c[0] for c in (cur.description or [])]
                    rows = cur.fetchall()
                result = [dict(zip(cols, row, strict=False)) for row in rows]
            else:
                result = self._execute_via_data_api(sql)
        except Exception as exc:  # noqa: BLE001
            if conn is not None:
                # A failed statement aborts the connection's current transaction — the
                # same connection is reused across every query in this fetch_efficiency()
                # pull, so without a rollback every later query fails with "current
                # transaction is aborted", not its own error. Best-effort: don't let a
                # rollback failure mask the original exception below.
                try:
                    conn.rollback()
                except Exception:  # noqa: BLE001
                    pass
            logger.warning(
                "redshift_query_failed",
                name=name,
                via=via,
                error=str(exc),
                elapsed_s=round(time.monotonic() - started, 2),
            )
            if isinstance(exc, ConnectorError):
                raise
            raise ConnectorError(self.name, f"bastion SQL query failed: {exc}") from exc
        logger.info(
            "redshift_query_done",
            name=name,
            rows=len(result),
            elapsed_s=round(time.monotonic() - started, 2),
        )
        return result

    def _execute_via_data_api(self, sql: str) -> list[dict[str, Any]]:
        kwargs: dict[str, Any] = {"Database": self._config.database, "Sql": sql}
        if self._config.cluster_identifier:
            kwargs["ClusterIdentifier"] = self._config.cluster_identifier
        if self._config.db_user:
            kwargs["DbUser"] = self._config.db_user
        if self._config.secret_arn:
            kwargs["SecretArn"] = self._config.secret_arn
        try:
            statement_id = self._data.execute_statement(**kwargs)["Id"]
            self._await_completion(statement_id)
            return self._collect_results(statement_id)
        except ConnectorError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ConnectorError(self.name, f"Data API statement failed: {exc}") from exc

    def _await_completion(self, statement_id: str) -> None:
        for _ in range(120):  # ~2 min ceiling
            desc = self._data.describe_statement(Id=statement_id)
            status = desc.get("Status")
            if status in _TERMINAL_STATES:
                if status != "FINISHED":
                    raise ConnectorError(
                        self.name,
                        f"Statement {status}: {desc.get('Error', '(no detail)')}",
                    )
                return
            time.sleep(1)
        raise ConnectorError(self.name, "Statement did not complete in time")

    def _collect_results(self, statement_id: str) -> list[dict[str, Any]]:
        cols: list[str] = []
        rows: list[dict[str, Any]] = []
        next_token: str | None = None
        while True:
            kwargs: dict[str, Any] = {"Id": statement_id}
            if next_token:
                kwargs["NextToken"] = next_token
            resp = self._data.get_statement_result(**kwargs)
            if not cols:
                cols = [c.get("name", "") for c in resp.get("ColumnMetadata", [])]
            for record in resp.get("Records", []):
                rows.append(
                    {col: _field_value(field) for col, field in zip(cols, record, strict=False)}
                )
            next_token = resp.get("NextToken")
            if not next_token:
                return rows

    def _run_session_init(self, conn: Any) -> None:
        """Run ``RedshiftConfig.session_init_sql`` once, right after a SQL connection
        opens — before any of the real efficiency queries reuse it. Deployment-
        specific WLM tuning (e.g. ``SET query_group TO 'my_wlm_queue';`` so this
        connector's queries get priority over a busy production cluster's other
        traffic) lives in the operator's own connections.yml, not this codebase —
        see ``RedshiftConfig.session_init_sql``'s own docstring. A failure here
        surfaces the same way any other query failure does (``_execute`` isn't used
        because this runs before the connection is handed to callers, but the
        rollback-on-failure reasoning is the same: an unset/typo'd WLM queue name
        would otherwise abort the connection's transaction and silently break every
        query after it).
        """
        if not self._config.session_init_sql:
            return
        try:
            with conn.cursor() as cur:
                cur.execute(self._config.session_init_sql)
            logger.info("redshift_session_init_applied", sql=self._config.session_init_sql)
        except Exception as exc:  # noqa: BLE001
            raise ConnectorError(self.name, f"session_init_sql failed: {exc}") from exc

    # ── Direct SQL over an SSH bastion tunnel (RedshiftConfig.bastion_host) ──────
    @contextmanager
    def _bastion_tunnel(self) -> Iterator[Any]:
        """Opens just the SSH tunnel (no DB connection) and yields the
        ``SSHTunnelForwarder`` — ``sshtunnel`` forwards each new inbound
        connection over its own SSH channel on the same already-open transport,
        so several DB connections can be opened through this one tunnel via
        :meth:`_open_sql_connection` (see :meth:`_bastion_lane_connections`),
        without repeating the SSH handshake or cluster-endpoint resolution below.
        """
        # redshift_connector is checked here too (even though this method never
        # calls it) so a missing extra fails before opening any tunnel, not on the
        # first lane connection.
        try:
            # sshtunnel 0.4.0 predates Python 3.14 and emits a SyntaxWarning for
            # its internal ``return`` inside ``finally`` on import. This is neither
            # a connection error nor our code, so suppress only that exact warning.
            with warnings.catch_warnings():
                warnings.filterwarnings(
                    "ignore",
                    message="'return' in a 'finally' block",
                    category=SyntaxWarning,
                    module="sshtunnel",
                )
                import paramiko
                import redshift_connector  # noqa: F401
                from sshtunnel import SSHTunnelForwarder
        except ImportError as exc:
            raise ConnectorError(
                self.name,
                "bastion_host is configured but the 'redshift-bastion' extra isn't "
                'installed — run: pip install "getflashlight[redshift-bastion]"',
            ) from exc

        # ponytail: sshtunnel 0.4.0 (its latest release) unconditionally references
        # paramiko.DSSKey when identifying a private key's type, even for non-DSA
        # keys — paramiko >=3 dropped DSSKey (DSA deprecated). Shim it back so key
        # loading doesn't crash before ever inspecting the actual (RSA/ECDSA/Ed25519)
        # key. Drop once sshtunnel ships a paramiko>=3-compatible release.
        if not hasattr(paramiko, "DSSKey"):
            paramiko.DSSKey = paramiko.RSAKey

        cfg = self._config
        assert cfg.bastion_host is not None  # caller checks this
        assert cfg.bastion_user is not None  # RedshiftConfig enforces this
        assert cfg.bastion_private_key_path is not None  # RedshiftConfig enforces this
        endpoint = self._cluster_endpoint()
        key_pass = (
            env(cfg.bastion_private_key_passphrase_env)
            if cfg.bastion_private_key_passphrase_env
            else None
        )
        try:
            with SSHTunnelForwarder(
                (cfg.bastion_host, cfg.bastion_port),
                ssh_username=cfg.bastion_user,
                ssh_pkey=cfg.bastion_private_key_path,
                ssh_private_key_password=key_pass,
                remote_bind_address=(endpoint["host"], endpoint["port"]),
            ) as tunnel:
                yield tunnel
        except ConnectorError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ConnectorError(self.name, f"bastion tunnel failed: {exc}") from exc

    @contextmanager
    def _bastion_connection(self) -> Iterator[Any]:
        """One SSH tunnel + one DB connection — the single-connection convenience
        wrapper :meth:`test_connection` uses. ``fetch_efficiency()`` instead opens
        the tunnel once via :meth:`_bastion_tunnel` and fans several connections
        through it (see :meth:`_bastion_lane_connections`).
        """
        with self._bastion_tunnel() as tunnel:
            creds = self._bastion_credentials()
            with self._open_sql_connection(
                host="127.0.0.1",
                port=tunnel.local_bind_port,
                user=creds["user"],
                password=creds["password"],
                error_prefix="bastion SQL connection failed",
            ) as conn:
                yield conn

    @contextmanager
    def _open_sql_connection(
        self, *, host: str, port: int, user: str, password: str, error_prefix: str
    ) -> Iterator[Any]:
        """Connect, run session_init, yield, close — the reusable body every SQL
        connection this connector opens shares. Callers (:meth:`_bastion_connection`,
        :meth:`_bastion_lane_connections`, :meth:`_direct_connection`,
        :meth:`_direct_lane_connections`) have already verified the
        'redshift-bastion' extra is importable before reaching here, and already
        resolved whatever's expensive (tunnel, endpoint, credentials) — this only
        does the one still-per-connection thing: opening a new socket.
        """
        import redshift_connector

        try:
            conn = redshift_connector.connect(
                host=host,
                port=port,
                database=self._config.database,
                user=user,
                password=password,
                ssl=True,
            )
        except Exception as exc:  # noqa: BLE001
            raise ConnectorError(self.name, f"{error_prefix}: {exc}") from exc
        try:
            self._run_session_init(conn)
            yield conn
        finally:
            conn.close()

    # ── Direct SQL, no tunnel (RedshiftConfig.db_password_env) ──────────────────
    @contextmanager
    def _direct_connection(self) -> Iterator[Any]:
        """One direct SQL connection straight to the cluster's real endpoint — no
        SSH tunnel, for a cluster reachable directly. Authenticates with a
        local/native DB account password (``RedshiftConfig.db_password_env`` —
        RedshiftConfig enforces ``db_user`` alongside it). The single-connection
        convenience wrapper :meth:`test_connection` uses; ``fetch_efficiency()``
        instead uses :meth:`_direct_lane_connections`.
        """
        try:
            import redshift_connector  # noqa: F401
        except ImportError as exc:
            raise ConnectorError(
                self.name,
                "db_password_env is configured but the 'redshift-bastion' extra "
                "isn't installed (it also provides the direct-connection driver) — "
                'run: pip install "getflashlight[redshift-bastion]"',
            ) from exc

        assert self._config.db_password_env is not None  # caller checks this
        assert self._config.db_user is not None  # RedshiftConfig enforces this
        password = env(self._config.db_password_env)
        if not password:
            raise ConnectorError(
                self.name,
                f"db_password_env={self._config.db_password_env!r} is set but "
                "empty/unset in the environment",
            )
        endpoint = self._cluster_endpoint()
        with self._open_sql_connection(
            host=endpoint["host"],
            port=endpoint["port"],
            user=self._config.db_user,
            password=password,
            error_prefix="direct SQL connection failed",
        ) as conn:
            yield conn

    def _cluster_endpoint(self) -> dict[str, Any]:
        """The Redshift cluster's own host/port — not the bastion's. Uses the
        explicit ``db_host``/``db_port`` override if set (skips the AWS API call
        entirely), else auto-discovers via ``describe_clusters``.
        """
        if self._config.db_host is not None:
            return {"host": self._config.db_host, "port": self._config.db_port or 5439}
        assert self._config.cluster_identifier is not None  # RedshiftConfig enforces this
        try:
            clusters = self._redshift.describe_clusters(
                ClusterIdentifier=self._config.cluster_identifier
            ).get("Clusters", [])
        except Exception as exc:  # noqa: BLE001
            raise ConnectorError(self.name, f"describe_clusters failed: {exc}") from exc
        if not clusters:
            raise ConnectorError(self.name, f"cluster {self._config.cluster_identifier} not found")
        endpoint = clusters[0].get("Endpoint", {})
        return {"host": endpoint["Address"], "port": endpoint["Port"]}

    def _bastion_credentials(self) -> dict[str, str]:
        """DB credentials for the bastion connection — static password if configured,
        else the default short-lived IAM-authenticated credential.
        """
        if self._config.db_password_env:
            password = env(self._config.db_password_env)
            if not password:
                raise ConnectorError(
                    self.name,
                    f"db_password_env={self._config.db_password_env!r} is "
                    "set but empty/unset in the environment",
                )
            assert self._config.db_user is not None  # RedshiftConfig enforces this with bastion
            return {"user": self._config.db_user, "password": password}
        return self._iam_db_credentials()

    def _iam_db_credentials(self) -> dict[str, str]:
        """Short-lived DB credentials via IAM — no static password ever configured."""
        assert self._config.cluster_identifier is not None
        assert self._config.db_user is not None  # RedshiftConfig enforces this with bastion
        resp = self._redshift.get_cluster_credentials(
            DbUser=self._config.db_user,
            DbName=self._config.database,
            ClusterIdentifier=self._config.cluster_identifier,
            AutoCreate=False,
        )
        return {"user": resp["DbUser"], "password": resp["DbPassword"]}

    # ── Cost breakdown (read from already-ingested aws_focus BRONZE) ─────────
    def _cost_breakdown(self, window: IngestWindow, cluster_identifier: str) -> dict[str, float]:
        """Real, target-scoped $ per cost subcategory for this Redshift cluster.

        Reads BRONZE FOCUS rows written by ``aws_focus`` for this same window —
        no Cost Explorer call. ``aws_focus`` already stamps ``x_cost_subcategory``
        on every Redshift row via ``_classify_redshift_cost_category``, and
        ``ingest/runner.py`` runs every connector's ``fetch()`` to completion
        before any connector's ``fetch_efficiency()`` runs, so those BRONZE rows
        are guaranteed on disk by the time this executes. A local read, not a
        second AWS API call/permission — see the module docstring.
        """
        try:
            con = lake_duck.connect()
            try:
                lake_duck.register_bronze(con)
                # charge_month is the Hive partition column — bounding it lets DuckDB
                # skip whole month directories instead of opening every month any
                # connector has ever written.
                rows = con.execute(
                    "SELECT x_cost_subcategory, sum(effective_cost) AS cost "
                    "FROM raw.focus_record "
                    "WHERE service_name IN (?, ?) "
                    "AND resource_id LIKE ? "
                    "AND charge_month >= ? AND charge_month <= ? "
                    "AND charge_period_start >= ? AND charge_period_start < ? "
                    "GROUP BY x_cost_subcategory",
                    [
                        *REDSHIFT_SERVICE_NAMES,
                        f"%:cluster:{cluster_identifier}",
                        window.start.strftime("%Y-%m"),
                        window.end.strftime("%Y-%m"),
                        window.start,
                        window.end + timedelta(days=1),
                    ],
                ).fetchall()
            finally:
                con.close()
        except Exception as exc:  # noqa: BLE001 - cost breakdown is best-effort
            logger.warning("redshift_cost_bronze_read_failed", error=str(exc))
            return {}

        # Only the buckets this connector's cause_detail carries.  Resource-less
        # account commitments/credits remain in the invoice views but are intentionally
        # not assigned to one cluster's telemetry.
        buckets = {"compute": 0.0, "concurrency_scaling": 0.0, "storage": 0.0, "spectrum_scan": 0.0}
        for subcategory, cost in rows:
            amount = _opt_float(cost)
            if subcategory in buckets and amount:
                buckets[subcategory] = amount
        return {k: v for k, v in buckets.items() if v}

    # ── Reserved-node coverage ───────────────────────────────────────────────
    def _reserved_node_coverage(self) -> dict[str, float]:
        """On-demand vs reserved node-hours right now, from cluster/reservation state.

        A snapshot (current node count vs current active reservations), not a
        month-long integral — good enough to flag "some on-demand capacity exists
        alongside/instead of reserved coverage", the deck's own RI-term finding.
        """
        if not self._config.cluster_identifier:
            return {}  # reserved nodes are a provisioned-cluster concept only
        try:
            clusters = self._redshift.describe_clusters(
                ClusterIdentifier=self._config.cluster_identifier
            ).get("Clusters", [])
            # ponytail: unpaginated — accounts rarely hold more than a handful of
            # Redshift reservations; add MaxRecords/Marker paging if that changes.
            reservations = self._redshift.describe_reserved_nodes().get("ReservedNodes", [])
        except Exception as exc:  # noqa: BLE001 - best-effort
            logger.warning("redshift_reserved_node_lookup_failed", error=str(exc))
            return {}
        if not clusters:
            return {}
        node_count = clusters[0].get("NumberOfNodes", 0)
        active_reserved = sum(
            r.get("NodeCount", 0) for r in reservations if r.get("State") == "active"
        )
        on_demand = max(node_count - active_reserved, 0)
        return {
            "on_demand_node_hours": float(on_demand * 24 * 30),
            "reserved_node_hours": float(min(active_reserved, node_count) * 24 * 30),
        }


def _field_value(field: dict[str, Any]) -> Any:
    """Unwrap a Redshift Data API ``Field`` union dict to its plain Python value."""
    if field.get("isNull"):
        return None
    for key in ("stringValue", "longValue", "doubleValue", "booleanValue"):
        if key in field:
            return field[key]
    return None
