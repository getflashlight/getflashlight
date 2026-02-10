"""Periodic inventory snapshot collector.

Snapshots the current state of clusters, jobs, costs, and infra resources
into the local database for historical tracking and scheduled analysis.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import structlog
from auralake_shared.core.context import ExecutionContext
from auralake_shared.core.exceptions import AuraLakeError
from sqlmodel import Session

from auralake_backend.db.models import (
    InfraCostSnapshot,
    InfraResourceMapping,
    JobProfileRecord,
)

logger = structlog.get_logger(__name__)


class InventoryCollector:
    """Snapshots provider inventory into the local database."""

    def __init__(self, context: ExecutionContext, session: Session) -> None:
        self.context = context
        self.session = session

    def collect_all(self) -> dict[str, int]:
        """Run all inventory collectors. Returns counts per category."""
        results: dict[str, int] = {}

        results["clusters"] = self._collect_clusters()
        results["jobs"] = self._collect_jobs()
        results["infra_costs"] = self._collect_infra_costs()

        logger.info("inventory_collection_completed", **results)
        return results

    # ------------------------------------------------------------------
    # Clusters
    # ------------------------------------------------------------------

    def _collect_clusters(self) -> int:
        """Snapshot cluster utilization and cost data."""
        try:
            compute = self.context.provider.get_compute_client()
            clusters = compute.list_clusters()
            count = 0

            for cluster in clusters:
                try:
                    utilization = compute.get_utilization(cluster.cluster_id, days=7)
                    # Store as infra resource mapping for tracking
                    mapping = InfraResourceMapping(
                        workspace_id=cluster.workspace_id,
                        provider=self.context.config.provider,
                        platform_resource_type="cluster",
                        platform_resource_id=cluster.cluster_id,
                        infra_resource_type="cluster",
                        infra_resource_id=cluster.cluster_id,
                        infra_resource_tags=cluster.tags,
                        hourly_cost_usd=(
                            utilization.total_cost_usd / max(utilization.active_hours, 1)
                            if utilization.total_cost_usd > 0
                            else None
                        ),
                        last_seen_at=datetime.now(UTC),
                    )
                    self.session.add(mapping)
                    count += 1
                except Exception as exc:
                    logger.warning(
                        "cluster_snapshot_failed",
                        cluster_id=cluster.cluster_id,
                        error=str(exc),
                    )

            self.session.commit()
            logger.info("clusters_collected", count=count)
            return count
        except AuraLakeError as exc:
            logger.error("cluster_collection_failed", error=str(exc))
            return 0

    # ------------------------------------------------------------------
    # Jobs
    # ------------------------------------------------------------------

    def _collect_jobs(self) -> int:
        """Snapshot job profiles."""
        try:
            job_client = self.context.provider.get_job_client()
            jobs = job_client.list_jobs()
            count = 0

            for job in jobs:
                try:
                    runs = job_client.get_job_runs(job.job_id, limit=10)

                    avg_duration = 0.0
                    if runs:
                        durations = [
                            r.get("run_duration_ms", 0) / 60_000
                            for r in runs
                            if r.get("run_duration_ms")
                        ]
                        avg_duration = sum(durations) / len(durations) if durations else 0.0

                    record = JobProfileRecord(
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
                        last_analyzed_at=datetime.now(UTC),
                    )
                    self.session.add(record)
                    count += 1
                except Exception as exc:
                    logger.warning(
                        "job_snapshot_failed",
                        job_id=job.job_id,
                        error=str(exc),
                    )

            self.session.commit()
            logger.info("jobs_collected", count=count)
            return count
        except AuraLakeError as exc:
            logger.error("job_collection_failed", error=str(exc))
            return 0

    # ------------------------------------------------------------------
    # Infrastructure costs
    # ------------------------------------------------------------------

    def _collect_infra_costs(self) -> int:
        """Snapshot infrastructure costs from AWS/cloud provider."""
        try:
            infra_client = self.context.provider.get_infra_cost_client()
            end = date.today()
            start = end - timedelta(days=7)

            count = 0
            for cost in infra_client.get_compute_costs(start, end):
                snapshot = InfraCostSnapshot(
                    provider=self.context.config.provider,
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
                self.session.add(snapshot)
                count += 1

            for cost in infra_client.get_storage_costs(start, end):
                snapshot = InfraCostSnapshot(
                    provider=self.context.config.provider,
                    period_start=start,
                    period_end=end,
                    service="storage",
                    resource_id=cost.bucket_or_volume,
                    cost_usd=float(cost.cost_usd),
                    usage_quantity=cost.storage_gb,
                    usage_unit="gb",
                    tags={},
                )
                self.session.add(snapshot)
                count += 1

            self.session.commit()
            logger.info("infra_costs_collected", count=count)
            return count
        except AuraLakeError as exc:
            logger.error("infra_cost_collection_failed", error=str(exc))
            return 0
