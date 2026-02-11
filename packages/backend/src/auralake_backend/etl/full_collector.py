"""Full collection pipeline with DAG-based parallel workers.

Orchestrates 8 collection steps that fetch Databricks infrastructure data
into the local database. Each worker tracks its own cursor/watermark so
subsequent runs only fetch new or changed data.
"""

from __future__ import annotations

import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from datetime import UTC, date, datetime, timedelta
from threading import Event, Semaphore
from typing import Any

import structlog
from sqlmodel import Session, select

from auralake_backend.db.engine import get_engine
from auralake_backend.db.models import (
    BillingRecord,
    ClusterPolicyRecord,
    CollectionRun,
    ComputeResourceRecord,
    InfraCostSnapshot,
    InfraResourceMapping,
    JobProfileRecord,
    JobRunRecord,
    QueryHistoryRecord,
    QueryPlan,
    UnityCatalogTableRecord,
    WorkerCursor,
)

logger = structlog.get_logger(__name__)

# Workers that always do a full sync (small datasets)
_FULL_SYNC_WORKERS = {"compute", "jobs", "policies", "catalog_tables"}

# Default lookback windows for first run
_DEFAULT_LOOKBACK_DAYS = 90


def _safe_int(val: Any) -> int | None:
    """Convert a value to int, handling string representations from SQL results."""
    if val is None:
        return None
    try:
        return int(float(val))
    except (ValueError, TypeError):
        return None


class FullCollector:
    """Orchestrates parallel data collection from a Databricks connection."""

    WORKER_NAMES = [
        "compute",
        "jobs",
        "job_runs",
        "billing",
        "query_history",
        "query_plans",
        "policies",
        "infra_costs",
        "catalog_tables",
    ]

    def __init__(
        self,
        connection_id: uuid.UUID,
        config: Any,
        cancel_event: Event | None = None,
    ) -> None:
        self.connection_id = connection_id
        self.config = config
        self._cancel = cancel_event or Event()

    def run(self, run_id: uuid.UUID) -> dict[str, Any]:
        """Execute the full collection pipeline. Returns worker_statuses dict."""
        worker_statuses: dict[str, dict[str, Any]] = {
            name: {"status": "pending"} for name in self.WORKER_NAMES
        }

        sql_semaphore = Semaphore(2)

        with ThreadPoolExecutor(max_workers=6) as pool:
            # Wave 1: independent workers
            f_compute = pool.submit(
                self._run_worker, run_id, "compute", worker_statuses, sql_semaphore
            )
            f_jobs = pool.submit(self._run_worker, run_id, "jobs", worker_statuses)
            f_billing = pool.submit(
                self._run_worker, run_id, "billing", worker_statuses, sql_semaphore
            )
            f_queries = pool.submit(
                self._run_worker, run_id, "query_history", worker_statuses, sql_semaphore
            )
            f_policies = pool.submit(self._run_worker, run_id, "policies", worker_statuses)
            f_infra = pool.submit(self._run_worker, run_id, "infra_costs", worker_statuses)
            f_tables = pool.submit(
                self._run_worker, run_id, "catalog_tables", worker_statuses, sql_semaphore
            )

            # Wave 2: dependent workers
            f_job_runs = pool.submit(self._after, f_jobs, run_id, "job_runs", worker_statuses)
            f_plans = pool.submit(
                self._after,
                f_queries,
                run_id,
                "query_plans",
                worker_statuses,
                sql_semaphore,
            )

            # Wait for all
            for f in [
                f_compute,
                f_jobs,
                f_billing,
                f_queries,
                f_policies,
                f_infra,
                f_tables,
                f_job_runs,
                f_plans,
            ]:
                f.result()

        # Run analysis after collection completes
        if not self._cancel.is_set():
            self._run_analysis(run_id, worker_statuses)

        return worker_statuses

    def run_single_worker(self, run_id: uuid.UUID, worker_name: str) -> dict[str, Any]:
        """Run a single worker (for retry). Returns the worker status dict."""
        worker_statuses: dict[str, dict[str, Any]] = {worker_name: {"status": "pending"}}
        sql_semaphore = Semaphore(2)
        sem = (
            sql_semaphore
            if worker_name
            in ("compute", "billing", "query_history", "query_plans", "catalog_tables")
            else None
        )
        self._run_worker(run_id, worker_name, worker_statuses, sem)
        return worker_statuses

    def _run_analysis(self, run_id: uuid.UUID, worker_statuses: dict) -> None:
        """Run all analyzers after collection completes."""
        from auralake_shared.core.context import ExecutionContext
        from auralake_shared.models.config import AutomationLevel
        from auralake_shared.providers import get_provider

        from auralake_backend.etl.analysis_scheduler import AnalysisScheduler

        try:
            provider = get_provider(self.config.provider, self.config)
            context = ExecutionContext(
                config=self.config,
                provider=provider,
                automation_level=AutomationLevel.RECOMMEND,
                dry_run=True,
            )

            with Session(get_engine()) as session:
                scheduler = AnalysisScheduler(context, session)
                results = scheduler.run_all()

            worker_statuses["analysis"] = {
                "status": "completed",
                "analyzers": results,
            }
            self._update_run_statuses(run_id, worker_statuses)
            logger.info("post_collection_analysis_completed", results=results)
        except Exception as exc:
            worker_statuses["analysis"] = {
                "status": "failed",
                "error": str(exc),
            }
            self._update_run_statuses(run_id, worker_statuses)
            logger.error("post_collection_analysis_failed", error=str(exc))

    # ------------------------------------------------------------------
    # Internal: worker orchestration
    # ------------------------------------------------------------------

    def _after(
        self,
        prerequisite: Future,
        run_id: uuid.UUID,
        worker_name: str,
        worker_statuses: dict,
        sql_semaphore: Semaphore | None = None,
    ) -> None:
        """Wait for a prerequisite future, then run a worker."""
        try:
            prerequisite.result()
        except Exception:
            # Prerequisite failed — still try (cursor may have old data)
            pass
        if self._cancel.is_set():
            worker_statuses[worker_name] = {"status": "cancelled"}
            return
        self._run_worker(run_id, worker_name, worker_statuses, sql_semaphore)

    def _run_worker(
        self,
        run_id: uuid.UUID,
        worker_name: str,
        worker_statuses: dict,
        sql_semaphore: Semaphore | None = None,
    ) -> None:
        """Execute a single worker with its own session and error boundary."""
        if self._cancel.is_set():
            worker_statuses[worker_name] = {"status": "cancelled"}
            return

        worker_statuses[worker_name] = {"status": "running", "count": 0}
        start = time.monotonic()

        try:
            with Session(get_engine()) as session:
                cursor = self._read_cursor(session, worker_name)

                if sql_semaphore:
                    sql_semaphore.acquire()
                try:
                    count, new_cursor = self._dispatch_worker(session, worker_name, cursor)
                finally:
                    if sql_semaphore:
                        sql_semaphore.release()

                if new_cursor is not None:
                    self._write_cursor(session, worker_name, new_cursor)

                session.commit()

            duration_ms = int((time.monotonic() - start) * 1000)
            worker_statuses[worker_name] = {
                "status": "completed",
                "count": count,
                "duration_ms": duration_ms,
            }
            self._update_run_statuses(run_id, worker_statuses)
            logger.info(
                "worker_completed",
                worker=worker_name,
                count=count,
                duration_ms=duration_ms,
            )

        except Exception as exc:
            duration_ms = int((time.monotonic() - start) * 1000)
            worker_statuses[worker_name] = {
                "status": "failed",
                "error": str(exc),
                "count": 0,
                "duration_ms": duration_ms,
            }
            self._update_run_statuses(run_id, worker_statuses)
            logger.error(
                "worker_failed",
                worker=worker_name,
                error=str(exc),
                duration_ms=duration_ms,
            )

    def _dispatch_worker(
        self, session: Session, worker_name: str, cursor: str | None
    ) -> tuple[int, str | None]:
        """Route to the correct worker method. Returns (count, new_cursor)."""
        dispatch = {
            "compute": self._collect_compute,
            "jobs": self._collect_jobs,
            "job_runs": self._collect_job_runs,
            "billing": self._collect_billing,
            "query_history": self._collect_query_history,
            "query_plans": self._collect_query_plans,
            "policies": self._collect_policies,
            "infra_costs": self._collect_infra_costs,
            "catalog_tables": self._collect_catalog_tables,
        }
        return dispatch[worker_name](session, cursor)

    # ------------------------------------------------------------------
    # Cursor management
    # ------------------------------------------------------------------

    def _read_cursor(self, session: Session, worker_name: str) -> str | None:
        stmt = select(WorkerCursor).where(
            WorkerCursor.connection_id == self.connection_id,
            WorkerCursor.worker_name == worker_name,
        )
        cursor = session.exec(stmt).first()
        return cursor.cursor_value if cursor else None

    def _write_cursor(self, session: Session, worker_name: str, value: str) -> None:
        stmt = select(WorkerCursor).where(
            WorkerCursor.connection_id == self.connection_id,
            WorkerCursor.worker_name == worker_name,
        )
        cursor = session.exec(stmt).first()
        if cursor:
            cursor.cursor_value = value
            cursor.updated_at = datetime.now(UTC)
            session.add(cursor)
        else:
            session.add(
                WorkerCursor(
                    connection_id=self.connection_id,
                    worker_name=worker_name,
                    cursor_value=value,
                )
            )

    def _update_run_statuses(self, run_id: uuid.UUID, worker_statuses: dict) -> None:
        """Persist worker_statuses back to the CollectionRun row."""
        try:
            with Session(get_engine()) as session:
                run = session.get(CollectionRun, run_id)
                if run:
                    run.worker_statuses = dict(worker_statuses)
                    session.add(run)
                    session.commit()
        except Exception as exc:
            logger.warning("run_status_update_failed", error=str(exc))

    # ------------------------------------------------------------------
    # Worker: Compute (full sync — clusters + warehouses + infra mappings)
    # ------------------------------------------------------------------

    def _collect_compute(self, session: Session, cursor: str | None) -> tuple[int, str | None]:
        from auralake_shared.providers import get_provider

        provider = get_provider(self.config.provider, self.config)
        compute = provider.get_compute_client()
        now = datetime.now(UTC)
        count = 0

        # Phase 1: All clusters (all states) → compute_resources
        running_clusters = []
        try:
            clusters_with_config = compute.list_all_clusters_with_config()
        except Exception as exc:
            logger.warning("cluster_list_failed", error=str(exc))
            clusters_with_config = []

        for cluster, raw_config in clusters_with_config:
            if self._cancel.is_set():
                break
            try:
                resource_type = (
                    "job_cluster" if cluster.cluster_source == "JOB" else "all_purpose_cluster"
                )
                existing = session.exec(
                    select(ComputeResourceRecord).where(
                        ComputeResourceRecord.connection_id == self.connection_id,
                        ComputeResourceRecord.resource_type == resource_type,
                        ComputeResourceRecord.resource_id == cluster.cluster_id,
                    )
                ).first()

                if existing:
                    existing.resource_name = cluster.cluster_name
                    existing.state = cluster.state
                    existing.creator = cluster.creator
                    existing.driver_node_type = cluster.driver_node_type
                    existing.worker_node_type = cluster.worker_node_type
                    existing.num_workers = cluster.num_workers
                    existing.min_workers = cluster.min_workers
                    existing.max_workers = cluster.max_workers
                    existing.autoscale = cluster.autoscale
                    existing.spot_enabled = cluster.spot_enabled
                    existing.spot_fallback = cluster.spot_fallback
                    existing.autotermination_minutes = cluster.autotermination_minutes
                    existing.cluster_source = cluster.cluster_source
                    existing.tags = cluster.tags
                    existing.spark_config = raw_config.get("spark_conf", {})
                    existing.config = raw_config
                    existing.started_at = cluster.started_at
                    existing.last_activity_at = cluster.last_activity_at
                    existing.last_seen_at = now
                    session.add(existing)
                else:
                    session.add(
                        ComputeResourceRecord(
                            connection_id=self.connection_id,
                            workspace_id=cluster.workspace_id,
                            resource_type=resource_type,
                            resource_id=cluster.cluster_id,
                            resource_name=cluster.cluster_name,
                            state=cluster.state,
                            creator=cluster.creator,
                            driver_node_type=cluster.driver_node_type,
                            worker_node_type=cluster.worker_node_type,
                            num_workers=cluster.num_workers,
                            min_workers=cluster.min_workers,
                            max_workers=cluster.max_workers,
                            autoscale=cluster.autoscale,
                            spot_enabled=cluster.spot_enabled,
                            spot_fallback=cluster.spot_fallback,
                            autotermination_minutes=cluster.autotermination_minutes,
                            cluster_source=cluster.cluster_source,
                            tags=cluster.tags,
                            spark_config=raw_config.get("spark_conf", {}),
                            config=raw_config,
                            started_at=cluster.started_at,
                            last_activity_at=cluster.last_activity_at,
                            last_seen_at=now,
                        )
                    )
                count += 1

                if cluster.state in ("RUNNING", "PENDING", "RESTARTING", "RESIZING"):
                    running_clusters.append(cluster)
            except Exception as exc:
                logger.warning(
                    "cluster_collect_failed",
                    cluster_id=cluster.cluster_id,
                    error=str(exc),
                )

        # Phase 2: SQL warehouses → compute_resources
        try:
            warehouses = compute.list_warehouses()
            for wh in warehouses:
                if self._cancel.is_set():
                    break
                try:
                    wh_id = wh["warehouse_id"]
                    existing = session.exec(
                        select(ComputeResourceRecord).where(
                            ComputeResourceRecord.connection_id == self.connection_id,
                            ComputeResourceRecord.resource_type == "sql_warehouse",
                            ComputeResourceRecord.resource_id == wh_id,
                        )
                    ).first()

                    spot_enabled = wh.get("spot_instance_policy") in (
                        "COST_OPTIMIZED",
                        "RELIABILITY_OPTIMIZED",
                    )

                    if existing:
                        existing.resource_name = wh["name"]
                        existing.state = wh["state"]
                        existing.creator = wh.get("creator_name")
                        existing.warehouse_type = wh.get("warehouse_type")
                        existing.warehouse_size = wh.get("cluster_size")
                        existing.autotermination_minutes = wh.get("auto_stop_mins")
                        existing.min_workers = wh.get("min_num_clusters")
                        existing.max_workers = wh.get("max_num_clusters")
                        existing.spot_enabled = spot_enabled
                        existing.tags = wh.get("tags", {})
                        existing.config = wh
                        existing.last_seen_at = now
                        session.add(existing)
                    else:
                        session.add(
                            ComputeResourceRecord(
                                connection_id=self.connection_id,
                                resource_type="sql_warehouse",
                                resource_id=wh_id,
                                resource_name=wh["name"],
                                state=wh["state"],
                                creator=wh.get("creator_name"),
                                warehouse_type=wh.get("warehouse_type"),
                                warehouse_size=wh.get("cluster_size"),
                                autotermination_minutes=wh.get("auto_stop_mins"),
                                min_workers=wh.get("min_num_clusters"),
                                max_workers=wh.get("max_num_clusters"),
                                spot_enabled=spot_enabled,
                                tags=wh.get("tags", {}),
                                config=wh,
                                last_seen_at=now,
                            )
                        )
                    count += 1
                except Exception as exc:
                    logger.warning(
                        "warehouse_collect_failed",
                        warehouse_id=wh.get("warehouse_id"),
                        error=str(exc),
                    )
        except Exception as exc:
            logger.warning("warehouse_list_failed", error=str(exc))

        # Phase 2b: DLT Pipelines → compute_resources
        try:
            pipelines = compute.list_pipelines()
            for pl in pipelines:
                if self._cancel.is_set():
                    break
                try:
                    pl_id = pl["pipeline_id"]
                    existing = session.exec(
                        select(ComputeResourceRecord).where(
                            ComputeResourceRecord.connection_id == self.connection_id,
                            ComputeResourceRecord.resource_type == "dlt_pipeline",
                            ComputeResourceRecord.resource_id == pl_id,
                        )
                    ).first()

                    if existing:
                        existing.resource_name = pl["name"]
                        existing.state = pl["state"]
                        existing.creator = pl.get("creator")
                        existing.config = pl.get("config", {})
                        existing.last_seen_at = now
                        session.add(existing)
                    else:
                        session.add(
                            ComputeResourceRecord(
                                connection_id=self.connection_id,
                                resource_type="dlt_pipeline",
                                resource_id=pl_id,
                                resource_name=pl["name"],
                                state=pl["state"],
                                creator=pl.get("creator"),
                                config=pl.get("config", {}),
                                last_seen_at=now,
                            )
                        )
                    count += 1
                except Exception as exc:
                    logger.warning(
                        "pipeline_collect_failed",
                        pipeline_id=pl.get("pipeline_id"),
                        error=str(exc),
                    )
        except Exception as exc:
            logger.warning("pipeline_list_failed", error=str(exc))

        # Phase 2c: Model Serving Endpoints → compute_resources
        try:
            serving_endpoints = compute.list_serving_endpoints()
            for ep in serving_endpoints:
                if self._cancel.is_set():
                    break
                try:
                    ep_name = ep["endpoint_name"]
                    existing = session.exec(
                        select(ComputeResourceRecord).where(
                            ComputeResourceRecord.connection_id == self.connection_id,
                            ComputeResourceRecord.resource_type == "serving_endpoint",
                            ComputeResourceRecord.resource_id == ep_name,
                        )
                    ).first()

                    if existing:
                        existing.resource_name = ep_name
                        existing.state = ep["state"]
                        existing.creator = ep.get("creator")
                        existing.config = ep.get("config", {})
                        existing.last_seen_at = now
                        session.add(existing)
                    else:
                        session.add(
                            ComputeResourceRecord(
                                connection_id=self.connection_id,
                                resource_type="serving_endpoint",
                                resource_id=ep_name,
                                resource_name=ep_name,
                                state=ep["state"],
                                creator=ep.get("creator"),
                                config=ep.get("config", {}),
                                last_seen_at=now,
                            )
                        )
                    count += 1
                except Exception as exc:
                    logger.warning(
                        "serving_endpoint_collect_failed",
                        endpoint_name=ep.get("endpoint_name"),
                        error=str(exc),
                    )
        except Exception as exc:
            logger.warning("serving_endpoint_list_failed", error=str(exc))

        # Phase 2d: Vector Search Endpoints → compute_resources
        try:
            vs_endpoints = compute.list_vector_search_endpoints()
            for ep in vs_endpoints:
                if self._cancel.is_set():
                    break
                try:
                    ep_name = ep["endpoint_name"]
                    existing = session.exec(
                        select(ComputeResourceRecord).where(
                            ComputeResourceRecord.connection_id == self.connection_id,
                            ComputeResourceRecord.resource_type == "vector_search_endpoint",
                            ComputeResourceRecord.resource_id == ep_name,
                        )
                    ).first()

                    if existing:
                        existing.resource_name = ep_name
                        existing.state = ep["state"]
                        existing.creator = ep.get("creator")
                        existing.config = ep.get("config", {})
                        existing.last_seen_at = now
                        session.add(existing)
                    else:
                        session.add(
                            ComputeResourceRecord(
                                connection_id=self.connection_id,
                                resource_type="vector_search_endpoint",
                                resource_id=ep_name,
                                resource_name=ep_name,
                                state=ep["state"],
                                creator=ep.get("creator"),
                                config=ep.get("config", {}),
                                last_seen_at=now,
                            )
                        )
                    count += 1
                except Exception as exc:
                    logger.warning(
                        "vector_search_endpoint_collect_failed",
                        endpoint_name=ep.get("endpoint_name"),
                        error=str(exc),
                    )
        except Exception as exc:
            logger.warning("vector_search_endpoint_list_failed", error=str(exc))

        # Phase 3: Keep existing infra_resource_mappings for RUNNING clusters
        for cluster in running_clusters:
            if self._cancel.is_set():
                break
            try:
                utilization = compute.get_utilization(cluster.cluster_id, days=7)
                hourly_cost = (
                    utilization.total_cost_usd / max(utilization.active_hours, 1)
                    if utilization.total_cost_usd > 0
                    else None
                )

                existing = session.exec(
                    select(InfraResourceMapping).where(
                        InfraResourceMapping.platform_resource_id == cluster.cluster_id,
                        InfraResourceMapping.platform_resource_type == "cluster",
                    )
                ).first()

                if existing:
                    existing.infra_resource_tags = cluster.tags
                    existing.hourly_cost_usd = hourly_cost
                    existing.last_seen_at = now
                    session.add(existing)
                else:
                    session.add(
                        InfraResourceMapping(
                            workspace_id=cluster.workspace_id,
                            provider=self.config.provider,
                            platform_resource_type="cluster",
                            platform_resource_id=cluster.cluster_id,
                            infra_resource_type="cluster",
                            infra_resource_id=cluster.cluster_id,
                            infra_resource_tags=cluster.tags,
                            hourly_cost_usd=hourly_cost,
                            last_seen_at=now,
                        )
                    )
            except Exception as exc:
                logger.warning(
                    "cluster_utilization_failed",
                    cluster_id=cluster.cluster_id,
                    error=str(exc),
                )

        return count, now.isoformat()

    # ------------------------------------------------------------------
    # Worker: Jobs (full sync, upsert)
    # ------------------------------------------------------------------

    def _collect_jobs(self, session: Session, cursor: str | None) -> tuple[int, str | None]:
        from auralake_shared.providers import get_provider

        provider = get_provider(self.config.provider, self.config)
        job_client = provider.get_job_client()
        jobs = job_client.list_jobs()
        count = 0
        now = datetime.now(UTC)

        for job in jobs:
            if self._cancel.is_set():
                break
            try:
                runs = job_client.get_job_runs(job.job_id)
                avg_duration = 0.0
                if runs:
                    durations = [
                        r.get("execution_duration_ms", 0) / 60_000
                        for r in runs
                        if r.get("execution_duration_ms")
                    ]
                    avg_duration = sum(durations) / len(durations) if durations else 0.0

                # Upsert by job_id
                existing = session.exec(
                    select(JobProfileRecord).where(JobProfileRecord.job_id == job.job_id)
                ).first()

                if existing:
                    existing.job_name = job.job_name
                    existing.schedule_cron = job.schedule_cron
                    existing.avg_duration_minutes = avg_duration
                    existing.avg_dbu_cost = job.avg_dbu_cost
                    existing.instance_type = job.instance_type
                    existing.worker_count = job.worker_count
                    existing.spark_config = job.spark_config or {}
                    existing.data_sources = {"sources": job.data_sources}
                    existing.databricks_features_used = {"features": job.databricks_features_used}
                    existing.is_portable = job.is_portable
                    existing.last_analyzed_at = now
                    session.add(existing)
                else:
                    session.add(
                        JobProfileRecord(
                            workspace_id=job.workspace_id,
                            job_id=job.job_id,
                            job_name=job.job_name,
                            schedule_cron=job.schedule_cron,
                            avg_duration_minutes=avg_duration,
                            avg_dbu_cost=job.avg_dbu_cost,
                            instance_type=job.instance_type,
                            worker_count=job.worker_count,
                            spark_config=job.spark_config or {},
                            data_sources={"sources": job.data_sources},
                            databricks_features_used={"features": job.databricks_features_used},
                            is_portable=job.is_portable,
                            last_analyzed_at=now,
                        )
                    )
                count += 1
            except Exception as exc:
                logger.warning(
                    "job_collect_failed",
                    job_id=job.job_id,
                    error=str(exc),
                )

        return count, now.isoformat()

    # ------------------------------------------------------------------
    # Worker: Job Runs (incremental from cursor)
    # ------------------------------------------------------------------

    def _collect_job_runs(self, session: Session, cursor: str | None) -> tuple[int, str | None]:
        from auralake_shared.providers import get_provider

        provider = get_provider(self.config.provider, self.config)
        job_client = provider.get_job_client()

        if cursor:
            since_ms = int(datetime.fromisoformat(cursor).timestamp() * 1000)
        else:
            since_ms = int(
                (datetime.now(UTC) - timedelta(days=_DEFAULT_LOOKBACK_DAYS)).timestamp() * 1000
            )

        # Get all job IDs from the database
        job_records = session.exec(select(JobProfileRecord)).all()
        count = 0
        latest_end_time: int | None = None

        for job_rec in job_records:
            if self._cancel.is_set():
                break
            try:
                runs = job_client.get_job_runs_since(job_rec.job_id, since_ms)
                for run in runs:
                    run_id = run.get("run_id", "")
                    if not run_id:
                        continue

                    start_time = run.get("start_time")
                    end_time = run.get("end_time")

                    # Upsert by run_id
                    existing = session.exec(
                        select(JobRunRecord).where(JobRunRecord.run_id == run_id)
                    ).first()

                    start_dt = (
                        datetime.fromtimestamp(start_time / 1000, tz=UTC) if start_time else None
                    )
                    end_dt = datetime.fromtimestamp(end_time / 1000, tz=UTC) if end_time else None

                    if existing:
                        existing.state = run.get("state", "UNKNOWN")
                        existing.start_time = start_dt
                        existing.end_time = end_dt
                        existing.duration_ms = run.get("execution_duration_ms")
                        existing.cluster_id = run.get("cluster_id")
                        existing.trigger = run.get("trigger")
                        existing.error_message = run.get("error_message")
                        session.add(existing)
                    else:
                        session.add(
                            JobRunRecord(
                                connection_id=self.connection_id,
                                workspace_id=job_rec.workspace_id,
                                job_id=job_rec.job_id,
                                run_id=run_id,
                                state=run.get("state", "UNKNOWN"),
                                start_time=start_dt,
                                end_time=end_dt,
                                duration_ms=run.get("execution_duration_ms"),
                                cluster_id=run.get("cluster_id"),
                                trigger=run.get("trigger"),
                                error_message=run.get("error_message"),
                            )
                        )
                    count += 1

                    if start_time and (latest_end_time is None or start_time > latest_end_time):
                        latest_end_time = start_time

            except Exception as exc:
                logger.warning(
                    "job_runs_collect_failed",
                    job_id=job_rec.job_id,
                    error=str(exc),
                )

        new_cursor = (
            datetime.fromtimestamp(latest_end_time / 1000, tz=UTC).isoformat()
            if latest_end_time
            else cursor
        )
        return count, new_cursor

    # ------------------------------------------------------------------
    # Worker: Billing (incremental from cursor)
    # ------------------------------------------------------------------

    def _collect_billing(self, session: Session, cursor: str | None) -> tuple[int, str | None]:
        from auralake_shared.providers import get_provider

        provider = get_provider(self.config.provider, self.config)
        cost_client = provider.get_cost_client()

        if cursor:
            since = date.fromisoformat(cursor)
        else:
            since = date.today() - timedelta(days=_DEFAULT_LOOKBACK_DAYS)

        records = cost_client.get_usage_since(since)
        count = 0

        for r in records:
            if self._cancel.is_set():
                break
            session.add(
                BillingRecord(
                    connection_id=self.connection_id,
                    usage_date=r.date,
                    sku=r.sku or "unknown",
                    cluster_id=r.cluster_id,
                    job_id=r.job_id,
                    warehouse_id=r.warehouse_id,
                    endpoint_id=r.endpoint_id,
                    pipeline_id=r.pipeline_id,
                    notebook_id=r.notebook_id,
                    dbu_usage=r.dbu_usage,
                    cost_usd=float(r.cost_usd),
                )
            )
            count += 1

        new_cursor = date.today().isoformat()
        return count, new_cursor

    # ------------------------------------------------------------------
    # Worker: Query History (incremental from cursor)
    # ------------------------------------------------------------------

    def _collect_query_history(
        self, session: Session, cursor: str | None
    ) -> tuple[int, str | None]:
        from auralake_shared.providers import get_provider

        provider = get_provider(self.config.provider, self.config)
        query_client = provider.get_query_client()

        if cursor:
            since_date = cursor  # ISO date string, e.g. "2026-02-04"
        else:
            since_date = (date.today() - timedelta(days=_DEFAULT_LOOKBACK_DAYS)).isoformat()

        records = query_client.get_query_history_sql(since_date)
        count = 0

        for q in records:
            if self._cancel.is_set():
                break
            query_id = q.get("query_id", "")
            if not query_id:
                continue

            # Upsert by query_id
            existing = session.exec(
                select(QueryHistoryRecord).where(QueryHistoryRecord.query_id == query_id)
            ).first()

            if not existing:
                session.add(
                    QueryHistoryRecord(
                        connection_id=self.connection_id,
                        workspace_id=q.get("warehouse_id"),
                        query_id=query_id,
                        query_text=q.get("query_text"),
                        status=q.get("status"),
                        user_name=q.get("user_name"),
                        warehouse_id=q.get("warehouse_id"),
                        duration_ms=_safe_int(q.get("duration_ms")),
                        rows_produced=_safe_int(q.get("rows_produced")),
                        query_start_time_ms=_safe_int(q.get("query_start_time_ms")),
                        query_end_time_ms=_safe_int(q.get("query_end_time_ms")),
                    )
                )
                count += 1

        new_cursor = date.today().isoformat()
        return count, new_cursor

    # ------------------------------------------------------------------
    # Worker: Query Plans (piggybacks on query_history cursor)
    # ------------------------------------------------------------------

    # Statements that cannot be EXPLAIN'd — skip to avoid wasted API calls
    _SKIP_PREFIXES = (
        "SHOW",
        "DESCRIBE",
        "DESC",
        "USE",
        "SET",
        "CREATE",
        "DROP",
        "ALTER",
        "GRANT",
        "REVOKE",
        "EXPLAIN",
        "CACHE",
        "UNCACHE",
    )

    def _collect_query_plans(self, session: Session, cursor: str | None) -> tuple[int, str | None]:
        from auralake_shared.providers import get_provider

        from auralake_backend.agent.plan_parser import PlanParser

        provider = get_provider(self.config.provider, self.config)
        query_client = provider.get_query_client()
        parser = PlanParser()

        # Incremental: only process queries created since the last cursor
        query_stmt = (
            select(QueryHistoryRecord)
            .where(QueryHistoryRecord.connection_id == self.connection_id)
            .where(QueryHistoryRecord.query_text.isnot(None))  # type: ignore[union-attr]
            .order_by(QueryHistoryRecord.created_at.asc())  # type: ignore[attr-defined]
        )
        if cursor:
            cursor_dt = datetime.fromisoformat(cursor)
            query_stmt = query_stmt.where(QueryHistoryRecord.created_at >= cursor_dt)

        recent_queries = session.exec(query_stmt).all()

        # Filter out queries that already have plans
        existing_plan_query_ids: set[str] = set()
        if recent_queries:
            query_ids = [q.query_id for q in recent_queries]
            # Check in batches to avoid overly long IN clauses
            for i in range(0, len(query_ids), 500):
                batch = query_ids[i : i + 500]
                existing_plans = session.exec(
                    select(QueryPlan.query_id).where(
                        QueryPlan.query_id.in_(batch)  # type: ignore[attr-defined]
                    )
                ).all()
                existing_plan_query_ids.update(existing_plans)

        count = 0
        flush_interval = 100
        for q in recent_queries:
            if self._cancel.is_set():
                break
            if q.query_id in existing_plan_query_ids:
                continue
            if not q.query_text:
                continue

            # Skip non-EXPLAIN-able statements
            trimmed = q.query_text.strip().upper()
            if any(trimmed.startswith(p) for p in self._SKIP_PREFIXES):
                continue

            try:
                plan_text = query_client.explain_query(q.query_text)
                if not plan_text:
                    continue

                spark_plan = parser.parse(q.query_id, plan_text, q.query_text)
                session.add(
                    QueryPlan(
                        workspace_id=q.warehouse_id or "",
                        query_id=q.query_id,
                        query_text=spark_plan.query_text,
                        physical_plan=spark_plan.physical_plan,
                        parsed_plan=[n.model_dump() for n in spark_plan.parsed_nodes],
                        anti_patterns=[a.model_dump() for a in spark_plan.anti_patterns],
                        duration_ms=q.duration_ms,
                        rows_scanned=spark_plan.rows_scanned,
                        bytes_read=spark_plan.bytes_read,
                        shuffle_bytes=spark_plan.shuffle_bytes,
                        spill_bytes=spark_plan.spill_bytes,
                        captured_at=datetime.now(UTC),
                    )
                )
                count += 1

                # Flush to DB periodically for progress visibility
                if count % flush_interval == 0:
                    session.flush()
                    logger.info("query_plans_progress", count=count)

            except Exception as exc:
                logger.warning(
                    "query_plan_collect_failed",
                    query_id=q.query_id,
                    error=str(exc),
                )

        return count, datetime.now(UTC).isoformat()

    # ------------------------------------------------------------------
    # Worker: Cluster Policies (full sync, upsert)
    # ------------------------------------------------------------------

    def _collect_policies(self, session: Session, cursor: str | None) -> tuple[int, str | None]:
        from auralake_backend.providers.databricks.policy_client import (
            DatabricksPolicyClient,
        )

        policy_client = DatabricksPolicyClient(self.config.databricks)
        policies = policy_client.list_policies()
        count = 0
        now = datetime.now(UTC)

        for p in policies:
            if self._cancel.is_set():
                break
            policy_id = p.get("policy_id", "")
            if not policy_id:
                continue

            existing = session.exec(
                select(ClusterPolicyRecord).where(
                    ClusterPolicyRecord.connection_id == self.connection_id,
                    ClusterPolicyRecord.policy_id == policy_id,
                )
            ).first()

            if existing:
                existing.name = p.get("name", "")
                existing.description = p.get("description", "")
                existing.definition = p.get("definition") or {}
                existing.last_seen_at = now
                session.add(existing)
            else:
                session.add(
                    ClusterPolicyRecord(
                        connection_id=self.connection_id,
                        policy_id=policy_id,
                        name=p.get("name", ""),
                        description=p.get("description", ""),
                        definition=p.get("definition") or {},
                        last_seen_at=now,
                    )
                )
            count += 1

        return count, now.isoformat()

    # ------------------------------------------------------------------
    # Worker: Infra Costs (incremental from cursor)
    # ------------------------------------------------------------------

    def _collect_infra_costs(self, session: Session, cursor: str | None) -> tuple[int, str | None]:
        from auralake_shared.providers import get_provider

        provider = get_provider(self.config.provider, self.config)
        try:
            infra_client = provider.get_infra_cost_client()
        except Exception:
            # AWS infra costs are optional
            return 0, cursor

        if cursor:
            start = date.fromisoformat(cursor)
        else:
            start = date.today() - timedelta(days=_DEFAULT_LOOKBACK_DAYS)
        end = date.today()

        count = 0
        for cost in infra_client.get_compute_costs(start, end):
            if self._cancel.is_set():
                break
            session.add(
                InfraCostSnapshot(
                    provider=self.config.provider,
                    period_start=start,
                    period_end=end,
                    service=cost.service,
                    resource_id=cost.resource_id,
                    platform_resource_id=cost.platform_resource_id,
                    cost_usd=float(cost.cost_usd),
                    usage_quantity=cost.usage_hours,
                    usage_unit="hours",
                    tags={},
                )
            )
            count += 1

        try:
            for cost in infra_client.get_storage_costs(start, end):
                if self._cancel.is_set():
                    break
                session.add(
                    InfraCostSnapshot(
                        provider=self.config.provider,
                        period_start=start,
                        period_end=end,
                        service=cost.service,
                        resource_id=cost.bucket_or_volume,
                        cost_usd=float(cost.cost_usd),
                        usage_quantity=cost.storage_gb,
                        usage_unit="gb",
                        tags={},
                    )
                )
                count += 1
        except Exception as exc:
            logger.warning("storage_cost_collection_failed", error=str(exc))

        try:
            for xfer in infra_client.get_data_transfer_costs(start, end):
                if self._cancel.is_set():
                    break
                session.add(
                    InfraCostSnapshot(
                        provider=self.config.provider,
                        period_start=start,
                        period_end=end,
                        service="data_transfer",
                        resource_id=xfer.source,
                        cost_usd=float(xfer.cost_usd),
                        usage_quantity=xfer.transfer_gb,
                        usage_unit="gb",
                        tags={},
                    )
                )
                count += 1
        except Exception as exc:
            logger.warning("data_transfer_cost_collection_failed", error=str(exc))

        now = datetime.now(UTC)
        try:
            for mapping in infra_client.map_platform_resources_to_infra():
                if self._cancel.is_set():
                    break
                existing = session.exec(
                    select(InfraResourceMapping).where(
                        InfraResourceMapping.platform_resource_id == mapping.platform_resource_id,
                        InfraResourceMapping.infra_resource_id == mapping.infra_resource_id,
                    )
                ).first()
                if existing:
                    existing.infra_resource_tags = mapping.tags
                    existing.hourly_cost_usd = (
                        float(mapping.hourly_cost_usd) if mapping.hourly_cost_usd else None
                    )
                    existing.last_seen_at = now
                    session.add(existing)
                else:
                    session.add(
                        InfraResourceMapping(
                            provider=self.config.provider,
                            platform_resource_type=mapping.platform_resource_type,
                            platform_resource_id=mapping.platform_resource_id,
                            infra_resource_type=mapping.infra_resource_type,
                            infra_resource_id=mapping.infra_resource_id,
                            infra_resource_tags=mapping.tags,
                            hourly_cost_usd=(
                                float(mapping.hourly_cost_usd) if mapping.hourly_cost_usd else None
                            ),
                            last_seen_at=now,
                        )
                    )
        except Exception as exc:
            logger.warning("infra_resource_mapping_collection_failed", error=str(exc))

        return count, end.isoformat()

    # ------------------------------------------------------------------
    # Worker: Catalog Tables (full sync, upsert)
    # ------------------------------------------------------------------

    def _collect_catalog_tables(
        self, session: Session, cursor: str | None
    ) -> tuple[int, str | None]:
        from auralake_shared.providers import get_provider

        provider = get_provider(self.config.provider, self.config)
        storage = provider.get_storage_client()
        tables = storage.discover_all_tables()
        now = datetime.now(UTC)

        # Split: only Delta tables need DESCRIBE DETAIL for physical stats
        delta_tables = []
        non_delta_tables = []
        for t in tables:
            fmt = (t.get("data_source_format") or "").upper()
            if fmt == "DELTA" or not fmt:
                delta_tables.append(t)
            else:
                non_delta_tables.append(t)

        logger.info(
            "catalog_tables_discovered",
            total=len(tables),
            delta=len(delta_tables),
            non_delta=len(non_delta_tables),
        )

        count = 0

        # Non-Delta tables: upsert with discovery metadata only (no DESCRIBE DETAIL)
        for t in non_delta_tables:
            if self._cancel.is_set():
                break
            self._upsert_catalog_table(session, t, {}, now)
            count += 1

        # Delta tables: fetch stats + upsert in parallel, each thread owns its session
        engine = get_engine()

        def _fetch_and_upsert(t: dict[str, Any]) -> bool:
            stats: dict[str, Any] = {}
            stats_error: str | None = None
            history: list[dict[str, Any]] = []
            history_error: str | None = None

            try:
                stats = storage.get_table_stats(t["full_name"])
            except Exception as exc:
                stats_error = str(exc)
                logger.warning(
                    "catalog_table_stats_failed",
                    table=t["full_name"],
                    error=stats_error,
                )

            try:
                history = storage.get_table_history(t["full_name"])
            except Exception as exc:
                history_error = str(exc)
                logger.warning(
                    "catalog_table_history_failed",
                    table=t["full_name"],
                    error=history_error,
                )

            with Session(engine) as thread_session:
                self._upsert_catalog_table(
                    thread_session, t, stats, now, stats_error, history, history_error
                )
                thread_session.commit()
            return True

        with ThreadPoolExecutor(max_workers=10) as pool:
            futures = {pool.submit(_fetch_and_upsert, t): t for t in delta_tables}
            for future in as_completed(futures):
                if self._cancel.is_set():
                    break
                try:
                    future.result()
                    count += 1
                except Exception as exc:
                    t = futures[future]
                    logger.warning(
                        "catalog_table_collect_failed",
                        table=t["full_name"],
                        error=str(exc),
                    )

        return count, now.isoformat()

    def _parse_maintenance_history(self, history: list[dict[str, Any]]) -> dict[str, Any]:
        """Extract summary fields from DESCRIBE HISTORY rows (newest-first)."""
        import json

        last_optimized_at: datetime | None = None
        last_vacuumed_at: datetime | None = None
        optimize_count_30d = 0
        vacuum_count_30d = 0
        last_optimize_removed_files: int | None = None
        last_optimize_added_bytes: int | None = None
        uses_liquid_clustering = False
        uses_zordering = False

        cutoff_30d = datetime.now(UTC) - timedelta(days=30)

        for row in history:
            op_name = row.get("operation", "")
            timestamp_raw = row.get("timestamp")
            ts: datetime | None = None
            if timestamp_raw:
                try:
                    ts = datetime.fromisoformat(str(timestamp_raw))
                except (ValueError, TypeError):
                    pass

            if op_name == "OPTIMIZE":
                if last_optimized_at is None:
                    last_optimized_at = ts

                    # Parse operationMetrics for compaction stats
                    metrics = row.get("operationMetrics")
                    if isinstance(metrics, str):
                        try:
                            metrics = json.loads(metrics)
                        except (json.JSONDecodeError, TypeError):
                            metrics = {}
                    if isinstance(metrics, dict):
                        try:
                            last_optimize_removed_files = int(metrics.get("numRemovedFiles", 0))
                        except (ValueError, TypeError):
                            pass
                        try:
                            last_optimize_added_bytes = int(metrics.get("numAddedBytes", 0))
                        except (ValueError, TypeError):
                            pass

                    # Parse operationParameters for clustering info
                    params = row.get("operationParameters")
                    if isinstance(params, str):
                        try:
                            params = json.loads(params)
                        except (json.JSONDecodeError, TypeError):
                            params = {}
                    if isinstance(params, dict):
                        z_order_by = params.get("zOrderBy", "")
                        if z_order_by and z_order_by != "[]":
                            uses_zordering = True
                        cluster_by = params.get("clusterBy", "")
                        if cluster_by and cluster_by != "[]":
                            uses_liquid_clustering = True

                if ts and ts >= cutoff_30d:
                    optimize_count_30d += 1

            elif op_name == "VACUUM END":
                if last_vacuumed_at is None:
                    last_vacuumed_at = ts
                if ts and ts >= cutoff_30d:
                    vacuum_count_30d += 1

            elif op_name == "VACUUM START":
                # Count towards vacuum if no VACUUM END found
                if ts and ts >= cutoff_30d:
                    # Only count if not already counted via VACUUM END
                    pass

        return {
            "last_optimized_at": last_optimized_at,
            "last_vacuumed_at": last_vacuumed_at,
            "optimize_count_30d": optimize_count_30d,
            "vacuum_count_30d": vacuum_count_30d,
            "last_optimize_removed_files": last_optimize_removed_files,
            "last_optimize_added_bytes": last_optimize_added_bytes,
            "uses_liquid_clustering": uses_liquid_clustering,
            "uses_zordering": uses_zordering,
        }

    def _upsert_catalog_table(
        self,
        session: Session,
        t: dict[str, Any],
        stats: dict[str, Any],
        now: datetime,
        stats_error: str | None = None,
        history: list[dict[str, Any]] | None = None,
        history_error: str | None = None,
    ) -> None:
        """Parse fields from discovery metadata + DESCRIBE DETAIL stats and upsert."""
        full_name = t["full_name"]

        # Prefer DESCRIBE DETAIL stats, fall back to discovery metadata
        last_modified_at = None
        last_mod_raw = (
            stats.get("lastModified") or stats.get("last_modified") or t.get("last_altered")
        )
        if last_mod_raw:
            try:
                last_modified_at = datetime.fromisoformat(str(last_mod_raw))
            except (ValueError, TypeError):
                pass

        size_bytes = None
        raw_size = stats.get("sizeInBytes") or stats.get("size_in_bytes")
        if raw_size is not None:
            try:
                size_bytes = int(raw_size)
            except (ValueError, TypeError):
                pass

        num_files = None
        raw_files = stats.get("numFiles") or stats.get("num_files")
        if raw_files is not None:
            try:
                num_files = int(raw_files)
            except (ValueError, TypeError):
                pass

        location = stats.get("location")
        data_format = stats.get("format") or stats.get("data_format") or t.get("data_source_format")

        partition_cols = stats.get("partitionColumns") or stats.get("partition_columns", [])
        if isinstance(partition_cols, str):
            partition_cols = [c.strip() for c in partition_cols.split(",") if c.strip()]

        clustering_cols = stats.get("clusteringColumns") or stats.get("clustering_columns", [])
        if isinstance(clustering_cols, str):
            clustering_cols = [c.strip() for c in clustering_cols.split(",") if c.strip()]

        properties = stats.get("properties") or {}
        if isinstance(properties, str):
            properties = {}

        table_features = stats.get("tableFeatures") or stats.get("table_features", [])
        if isinstance(table_features, str):
            table_features = [f.strip() for f in table_features.split(",") if f.strip()]

        owner = stats.get("owner") or t.get("owner")

        # Parse maintenance history
        maint = self._parse_maintenance_history(history or [])

        # Upsert by (connection_id, full_name)
        existing = session.exec(
            select(UnityCatalogTableRecord).where(
                UnityCatalogTableRecord.connection_id == self.connection_id,
                UnityCatalogTableRecord.full_name == full_name,
            )
        ).first()

        if existing:
            existing.catalog_name = t["catalog"]
            existing.schema_name = t["schema"]
            existing.table_name = t["name"]
            existing.table_type = t.get("table_type", "UNKNOWN")
            existing.data_format = data_format
            existing.location = location
            existing.size_bytes = size_bytes
            existing.num_files = num_files
            existing.owner = owner
            existing.last_modified_at = last_modified_at
            existing.partition_columns = partition_cols
            existing.clustering_columns = clustering_cols
            existing.properties = properties
            existing.table_features = table_features
            existing.stats_error = stats_error
            existing.last_optimized_at = maint["last_optimized_at"]
            existing.last_vacuumed_at = maint["last_vacuumed_at"]
            existing.optimize_count_30d = maint["optimize_count_30d"]
            existing.vacuum_count_30d = maint["vacuum_count_30d"]
            existing.last_optimize_removed_files = maint["last_optimize_removed_files"]
            existing.last_optimize_added_bytes = maint["last_optimize_added_bytes"]
            existing.uses_liquid_clustering = maint["uses_liquid_clustering"]
            existing.uses_zordering = maint["uses_zordering"]
            existing.history_error = history_error
            existing.last_seen_at = now
            session.add(existing)
        else:
            session.add(
                UnityCatalogTableRecord(
                    connection_id=self.connection_id,
                    catalog_name=t["catalog"],
                    schema_name=t["schema"],
                    table_name=t["name"],
                    full_name=full_name,
                    table_type=t.get("table_type", "UNKNOWN"),
                    data_format=data_format,
                    location=location,
                    size_bytes=size_bytes,
                    num_files=num_files,
                    owner=owner,
                    last_modified_at=last_modified_at,
                    partition_columns=partition_cols,
                    clustering_columns=clustering_cols,
                    properties=properties,
                    table_features=table_features,
                    stats_error=stats_error,
                    last_optimized_at=maint["last_optimized_at"],
                    last_vacuumed_at=maint["last_vacuumed_at"],
                    optimize_count_30d=maint["optimize_count_30d"],
                    vacuum_count_30d=maint["vacuum_count_30d"],
                    last_optimize_removed_files=maint["last_optimize_removed_files"],
                    last_optimize_added_bytes=maint["last_optimize_added_bytes"],
                    uses_liquid_clustering=maint["uses_liquid_clustering"],
                    uses_zordering=maint["uses_zordering"],
                    history_error=history_error,
                    last_seen_at=now,
                )
            )
