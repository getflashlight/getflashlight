"""Full collection pipeline with DAG-based parallel workers.

Orchestrates 8 collection steps that fetch Databricks infrastructure data
into the local database. Each worker tracks its own cursor/watermark so
subsequent runs only fetch new or changed data.
"""

from __future__ import annotations

import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
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
    InfraCostSnapshot,
    InfraResourceMapping,
    JobProfileRecord,
    JobRunRecord,
    QueryHistoryRecord,
    QueryPlan,
    WorkerCursor,
)

logger = structlog.get_logger(__name__)

# Workers that always do a full sync (small datasets)
_FULL_SYNC_WORKERS = {"clusters", "jobs", "policies"}

# Default lookback windows for first run
_DEFAULT_LOOKBACK_DAYS = 90
_DEFAULT_QUERY_LOOKBACK_DAYS = 7


class FullCollector:
    """Orchestrates parallel data collection from a Databricks connection."""

    WORKER_NAMES = [
        "clusters",
        "jobs",
        "job_runs",
        "billing",
        "query_history",
        "query_plans",
        "policies",
        "infra_costs",
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
            f_clusters = pool.submit(
                self._run_worker, run_id, "clusters", worker_statuses, sql_semaphore
            )
            f_jobs = pool.submit(self._run_worker, run_id, "jobs", worker_statuses)
            f_billing = pool.submit(
                self._run_worker, run_id, "billing", worker_statuses, sql_semaphore
            )
            f_queries = pool.submit(self._run_worker, run_id, "query_history", worker_statuses)
            f_policies = pool.submit(self._run_worker, run_id, "policies", worker_statuses)
            f_infra = pool.submit(self._run_worker, run_id, "infra_costs", worker_statuses)

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
                f_clusters,
                f_jobs,
                f_billing,
                f_queries,
                f_policies,
                f_infra,
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
        sem = sql_semaphore if worker_name in ("clusters", "billing", "query_plans") else None
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
            "clusters": self._collect_clusters,
            "jobs": self._collect_jobs,
            "job_runs": self._collect_job_runs,
            "billing": self._collect_billing,
            "query_history": self._collect_query_history,
            "query_plans": self._collect_query_plans,
            "policies": self._collect_policies,
            "infra_costs": self._collect_infra_costs,
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
    # Worker: Clusters (full sync, upsert)
    # ------------------------------------------------------------------

    def _collect_clusters(self, session: Session, cursor: str | None) -> tuple[int, str | None]:
        from auralake_shared.providers import get_provider

        provider = get_provider(self.config.provider, self.config)
        compute = provider.get_compute_client()
        clusters = compute.list_clusters()
        count = 0
        now = datetime.now(UTC)

        for cluster in clusters:
            if self._cancel.is_set():
                break
            try:
                utilization = compute.get_utilization(cluster.cluster_id, days=7)
                hourly_cost = (
                    utilization.total_cost_usd / max(utilization.active_hours, 1)
                    if utilization.total_cost_usd > 0
                    else None
                )

                # Upsert by platform_resource_id
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
                count += 1
            except Exception as exc:
                logger.warning(
                    "cluster_collect_failed",
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
                runs = job_client.get_job_runs(job.job_id, limit=10)
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
            since_ms = int(datetime.fromisoformat(cursor).timestamp() * 1000)
        else:
            since_ms = int(
                (datetime.now(UTC) - timedelta(days=_DEFAULT_QUERY_LOOKBACK_DAYS)).timestamp()
                * 1000
            )

        queries = query_client.get_query_history_since(since_ms)
        count = 0
        latest_end_time_ms: int | None = None

        for q in queries:
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
                        duration_ms=q.get("duration_ms"),
                        rows_produced=q.get("rows_produced"),
                        query_start_time_ms=q.get("query_start_time_ms"),
                        query_end_time_ms=q.get("query_end_time_ms"),
                    )
                )
                count += 1

            end_ms = q.get("query_end_time_ms")
            if end_ms and (latest_end_time_ms is None or end_ms > latest_end_time_ms):
                latest_end_time_ms = end_ms

        new_cursor = (
            datetime.fromtimestamp(latest_end_time_ms / 1000, tz=UTC).isoformat()
            if latest_end_time_ms
            else cursor
        )
        return count, new_cursor

    # ------------------------------------------------------------------
    # Worker: Query Plans (piggybacks on query_history cursor)
    # ------------------------------------------------------------------

    def _collect_query_plans(self, session: Session, cursor: str | None) -> tuple[int, str | None]:
        from auralake_shared.providers import get_provider

        from auralake_backend.agent.plan_parser import PlanParser

        provider = get_provider(self.config.provider, self.config)
        query_client = provider.get_query_client()
        parser = PlanParser()

        # Find queries that don't have plans yet
        recent_queries = session.exec(
            select(QueryHistoryRecord)
            .where(QueryHistoryRecord.connection_id == self.connection_id)
            .where(QueryHistoryRecord.query_text.isnot(None))  # type: ignore[union-attr]
            .order_by(QueryHistoryRecord.created_at.desc())  # type: ignore[union-attr]
            .limit(200)
        ).all()

        existing_plan_query_ids = set()
        if recent_queries:
            query_ids = [q.query_id for q in recent_queries]
            existing_plans = session.exec(
                select(QueryPlan.query_id).where(
                    QueryPlan.query_id.in_(query_ids)  # type: ignore[union-attr]
                )
            ).all()
            existing_plan_query_ids = set(existing_plans)

        count = 0
        for q in recent_queries:
            if self._cancel.is_set():
                break
            if q.query_id in existing_plan_query_ids:
                continue
            if not q.query_text:
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
                        service="storage",
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

        return count, end.isoformat()
