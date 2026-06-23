"""Cluster utilization analyzer with right-sizing recommendations — DB-first."""

from __future__ import annotations

from decimal import Decimal

from sqlmodel import func, select

from auralake_shared.models.recommendations import (
    AnalysisResult,
    Recommendation,
    RiskLevel,
    SavingsConfidence,
)

from auralake_backend.analyzers.base import AbstractAnalyzer
from auralake_backend.db.models import (
    ClusterUtilizationSnapshot,
    ComputeResourceRecord,
    EnrichedBillingResource,
)


class ClusterAnalyzer(AbstractAnalyzer):
    name = "cluster"

    def analyze(self) -> AnalysisResult:
        if self.session is not None:
            return self._analyze_from_db()
        return self._analyze_from_api()

    def _analyze_from_db(self) -> AnalysisResult:
        thresholds = self.context.config.thresholds.cluster_rightsizing
        basis = self.pricing_basis()
        recommendations = []

        # Get all clusters from DB
        clusters = self.session.exec(  # type: ignore[union-attr]
            select(ComputeResourceRecord).where(
                ComputeResourceRecord.resource_type.in_(  # type: ignore[union-attr]
                    ["all_purpose_cluster", "job_cluster"]
                ),
            )
        ).all()

        # Get most recent utilization snapshot per cluster
        util_rows = self.session.exec(  # type: ignore[union-attr]
            select(ClusterUtilizationSnapshot).order_by(
                ClusterUtilizationSnapshot.captured_at.desc()
            )  # type: ignore[union-attr]
        ).all()
        # Deduplicate: keep most recent per cluster_id
        util_by_cluster: dict[str, ClusterUtilizationSnapshot] = {}
        for u in util_rows:
            if u.cluster_id not in util_by_cluster:
                util_by_cluster[u.cluster_id] = u

        # Get billing cost per cluster
        billing_rows = self.session.exec(  # type: ignore[union-attr]
            select(
                EnrichedBillingResource.resource_id,
                func.sum(EnrichedBillingResource.cost_usd),
            )
            .where(EnrichedBillingResource.resource_type == "cluster")
            .group_by(EnrichedBillingResource.resource_id)
        ).all()
        cost_by_cluster = {r[0]: float(r[1] or 0) for r in billing_rows}

        for cluster in clusters:
            util = util_by_cluster.get(cluster.resource_id)
            total_cost = cost_by_cluster.get(cluster.resource_id, 0)

            # Right-sizing check
            if self.rule_enabled("cluster_rightsize") and util:
                cpu_low = self.rule_threshold(
                    "cluster_rightsize",
                    "cpu_utilization_low",
                    thresholds.cpu_utilization_low,
                )
                min_savings = self.rule_threshold(
                    "cluster_rightsize",
                    "min_savings_dollars",
                    thresholds.min_savings_dollars,
                )
                if 0 < util.avg_cpu_percent < cpu_low:
                    savings = Decimal(str(total_cost * 0.3))
                    if savings >= min_savings:
                        recommendations.append(
                            Recommendation(
                                type="cluster_rightsize",
                                risk_level=RiskLevel.MEDIUM,
                                resource_id=cluster.resource_id,
                                resource_name=cluster.resource_name,
                                workspace_id=cluster.workspace_id,
                                title=f"Right-size cluster '{cluster.resource_name}'",
                                description=(
                                    f"Avg CPU: {util.avg_cpu_percent:.1f}%"
                                    f" (threshold: {cpu_low}%)."
                                    " Consider reducing workers or instance size."
                                ),
                                current_state={
                                    "num_workers": cluster.num_workers,
                                    "worker_node_type": cluster.worker_node_type,
                                    "avg_cpu": util.avg_cpu_percent,
                                },
                                recommended_state={
                                    "num_workers": max(
                                        1, (cluster.num_workers or 1) // 2
                                    ),
                                    "worker_node_type": cluster.worker_node_type,
                                },
                                estimated_monthly_savings_usd=savings,
                                savings_confidence=SavingsConfidence.MEDIUM,
                                pricing_basis=basis,
                                evidence={
                                    "avg_cpu_percent": util.avg_cpu_percent,
                                    "avg_memory_percent": util.avg_memory_percent,
                                    "active_hours": util.active_hours,
                                    "idle_hours": util.idle_hours,
                                },
                            )
                        )

            # Idle cluster check
            if self.rule_enabled("cluster_idle") and util:
                idle_threshold = self.rule_threshold(
                    "cluster_idle",
                    "idle_cluster_minutes",
                    self.context.config.thresholds.idle_resources.idle_cluster_minutes,
                )
                if cluster.state == "RUNNING" and util.idle_hours > (
                    idle_threshold / 60
                ):
                    recommendations.append(
                        Recommendation(
                            type="cluster_idle",
                            risk_level=RiskLevel.LOW,
                            resource_id=cluster.resource_id,
                            resource_name=cluster.resource_name,
                            workspace_id=cluster.workspace_id,
                            title=f"Idle cluster '{cluster.resource_name}'",
                            description=(
                                f"Cluster has been idle for"
                                f" {util.idle_hours:.1f} hours."
                                " Consider terminating."
                            ),
                            current_state={
                                "state": cluster.state,
                                "idle_hours": util.idle_hours,
                            },
                            recommended_state={"state": "TERMINATED"},
                            estimated_monthly_savings_usd=Decimal(str(total_cost))
                            if total_cost
                            else Decimal("100"),
                            savings_confidence=SavingsConfidence.HIGH,
                            pricing_basis=basis,
                        )
                    )

            # Autotermination check
            if self.rule_enabled("cluster_no_autotermination"):
                if (
                    cluster.autotermination_minutes is None
                    or cluster.autotermination_minutes == 0
                ):
                    recommendations.append(
                        Recommendation(
                            type="cluster_no_autotermination",
                            risk_level=RiskLevel.LOW,
                            resource_id=cluster.resource_id,
                            resource_name=cluster.resource_name,
                            workspace_id=cluster.workspace_id,
                            title=f"Enable autotermination on '{cluster.resource_name}'",
                            description=(
                                "Cluster has no autotermination configured."
                                " Enable to prevent idle cost waste."
                            ),
                            current_state={
                                "autotermination_minutes": cluster.autotermination_minutes
                            },
                            recommended_state={"autotermination_minutes": 60},
                            estimated_monthly_savings_usd=Decimal("50"),
                            savings_confidence=SavingsConfidence.LOW,
                            pricing_basis=basis,
                        )
                    )

            # Spot instance check
            if self.rule_enabled("cluster_spot_eligible"):
                if not cluster.spot_enabled and (cluster.num_workers or 0) > 0:
                    recommendations.append(
                        Recommendation(
                            type="cluster_spot_eligible",
                            risk_level=RiskLevel.MEDIUM,
                            resource_id=cluster.resource_id,
                            resource_name=cluster.resource_name,
                            workspace_id=cluster.workspace_id,
                            title=f"Enable spot instances on '{cluster.resource_name}'",
                            description=(
                                f"Cluster has {cluster.num_workers} workers"
                                " on on-demand. Spot can save 50-90%."
                            ),
                            current_state={"spot_enabled": False},
                            recommended_state={
                                "spot_enabled": True,
                                "spot_fallback": True,
                            },
                            estimated_monthly_savings_usd=Decimal(str(total_cost * 0.5))
                            if total_cost
                            else Decimal("200"),
                            savings_confidence=SavingsConfidence.MEDIUM,
                            pricing_basis=basis,
                        )
                    )

        return AnalysisResult(
            analyzer_name=self.name,
            provider=self.context.config.provider,
            recommendations=recommendations,
            summary={
                "total_clusters": len(clusters),
                "running_clusters": len([c for c in clusters if c.state == "RUNNING"]),
                "recommendations_count": len(recommendations),
            },
        )

    def _analyze_from_api(self) -> AnalysisResult:
        """Fallback: analyze from provider API (legacy path)."""
        compute = self.context.provider.get_compute_client()
        clusters = compute.list_clusters()
        thresholds = self.context.config.thresholds.cluster_rightsizing
        basis = self.pricing_basis()

        recommendations = []

        for cluster in clusters:
            utilization = compute.get_utilization(cluster.cluster_id)

            if self.rule_enabled("cluster_rightsize"):
                cpu_low = self.rule_threshold(
                    "cluster_rightsize",
                    "cpu_utilization_low",
                    thresholds.cpu_utilization_low,
                )
                min_savings = self.rule_threshold(
                    "cluster_rightsize",
                    "min_savings_dollars",
                    thresholds.min_savings_dollars,
                )
                if (
                    utilization.avg_cpu_percent > 0
                    and utilization.avg_cpu_percent < cpu_low
                ):
                    savings = Decimal(str(utilization.total_cost_usd * 0.3))
                    if savings >= min_savings:
                        recommendations.append(
                            Recommendation(
                                type="cluster_rightsize",
                                risk_level=RiskLevel.MEDIUM,
                                resource_id=cluster.cluster_id,
                                resource_name=cluster.cluster_name,
                                workspace_id=cluster.workspace_id,
                                title=f"Right-size cluster '{cluster.cluster_name}'",
                                description=(
                                    f"Avg CPU: {utilization.avg_cpu_percent:.1f}%"
                                    f" (threshold: {cpu_low}%)."
                                    " Consider reducing workers or instance size."
                                ),
                                current_state={
                                    "num_workers": cluster.num_workers,
                                    "worker_node_type": cluster.worker_node_type,
                                    "avg_cpu": utilization.avg_cpu_percent,
                                },
                                recommended_state={
                                    "num_workers": max(1, cluster.num_workers // 2),
                                    "worker_node_type": cluster.worker_node_type,
                                },
                                estimated_monthly_savings_usd=savings,
                                savings_confidence=SavingsConfidence.MEDIUM,
                                pricing_basis=basis,
                                evidence={
                                    "avg_cpu_percent": utilization.avg_cpu_percent,
                                    "avg_memory_percent": utilization.avg_memory_percent,
                                    "active_hours": utilization.active_hours,
                                    "idle_hours": utilization.idle_hours,
                                },
                            )
                        )

            if self.rule_enabled("cluster_idle"):
                idle_threshold = self.rule_threshold(
                    "cluster_idle",
                    "idle_cluster_minutes",
                    self.context.config.thresholds.idle_resources.idle_cluster_minutes,
                )
                if cluster.state == "RUNNING" and utilization.idle_hours > (
                    idle_threshold / 60
                ):
                    recommendations.append(
                        Recommendation(
                            type="cluster_idle",
                            risk_level=RiskLevel.LOW,
                            resource_id=cluster.cluster_id,
                            resource_name=cluster.cluster_name,
                            workspace_id=cluster.workspace_id,
                            title=f"Idle cluster '{cluster.cluster_name}'",
                            description=(
                                f"Cluster has been idle for"
                                f" {utilization.idle_hours:.1f} hours."
                                " Consider terminating."
                            ),
                            current_state={
                                "state": cluster.state,
                                "idle_hours": utilization.idle_hours,
                            },
                            recommended_state={"state": "TERMINATED"},
                            estimated_monthly_savings_usd=Decimal(
                                str(utilization.total_cost_usd)
                            ),
                            savings_confidence=SavingsConfidence.HIGH,
                            pricing_basis=basis,
                        )
                    )

            if self.rule_enabled("cluster_no_autotermination"):
                if (
                    cluster.autotermination_minutes is None
                    or cluster.autotermination_minutes == 0
                ):
                    recommendations.append(
                        Recommendation(
                            type="cluster_no_autotermination",
                            risk_level=RiskLevel.LOW,
                            resource_id=cluster.cluster_id,
                            resource_name=cluster.cluster_name,
                            workspace_id=cluster.workspace_id,
                            title=f"Enable autotermination on '{cluster.cluster_name}'",
                            description=(
                                "Cluster has no autotermination configured."
                                " Enable to prevent idle cost waste."
                            ),
                            current_state={
                                "autotermination_minutes": cluster.autotermination_minutes
                            },
                            recommended_state={"autotermination_minutes": 60},
                            estimated_monthly_savings_usd=Decimal("50"),
                            savings_confidence=SavingsConfidence.LOW,
                            pricing_basis=basis,
                        )
                    )

            if self.rule_enabled("cluster_spot_eligible"):
                if not cluster.spot_enabled and cluster.num_workers > 0:
                    recommendations.append(
                        Recommendation(
                            type="cluster_spot_eligible",
                            risk_level=RiskLevel.MEDIUM,
                            resource_id=cluster.cluster_id,
                            resource_name=cluster.cluster_name,
                            workspace_id=cluster.workspace_id,
                            title=f"Enable spot instances on '{cluster.cluster_name}'",
                            description=(
                                f"Cluster has {cluster.num_workers} workers"
                                " on on-demand. Spot can save 50-90%."
                            ),
                            current_state={"spot_enabled": False},
                            recommended_state={
                                "spot_enabled": True,
                                "spot_fallback": True,
                            },
                            estimated_monthly_savings_usd=Decimal(
                                str(utilization.total_cost_usd * 0.5)
                            ),
                            savings_confidence=SavingsConfidence.MEDIUM,
                            pricing_basis=basis,
                        )
                    )

        return AnalysisResult(
            analyzer_name=self.name,
            provider=self.context.config.provider,
            recommendations=recommendations,
            summary={
                "total_clusters": len(clusters),
                "running_clusters": len([c for c in clusters if c.state == "RUNNING"]),
                "recommendations_count": len(recommendations),
            },
        )
