"""Idle and unused resource detection — DB-first."""

from __future__ import annotations

from decimal import Decimal

from sqlmodel import select

from auralake_shared.models.recommendations import (
    AnalysisResult,
    Recommendation,
    RiskLevel,
    SavingsConfidence,
)

from auralake_backend.analyzers.base import AbstractAnalyzer
from auralake_backend.db.models import ClusterUtilizationSnapshot, ComputeResourceRecord


class IdleResourceAnalyzer(AbstractAnalyzer):
    name = "idle_resources"

    def analyze(self) -> AnalysisResult:
        if self.session is not None:
            return self._analyze_from_db()
        return self._analyze_from_api()

    def _analyze_from_db(self) -> AnalysisResult:
        thresholds = self.context.config.thresholds.idle_resources
        basis = self.pricing_basis()
        recommendations = []

        if not self.rule_enabled("idle_cluster"):
            clusters = self.session.exec(  # type: ignore[union-attr]
                select(ComputeResourceRecord).where(
                    ComputeResourceRecord.resource_type.in_(  # type: ignore[union-attr]
                        ["all_purpose_cluster", "job_cluster"]
                    ),
                )
            ).all()
            return AnalysisResult(
                analyzer_name=self.name,
                provider=self.context.config.provider,
                recommendations=[],
                summary={
                    "running_clusters": len(
                        [c for c in clusters if c.state == "RUNNING"]
                    ),
                    "idle_clusters": 0,
                },
            )

        idle_minutes_threshold = self.rule_threshold(
            "idle_cluster", "idle_cluster_minutes", thresholds.idle_cluster_minutes
        )

        # Get RUNNING clusters
        clusters = self.session.exec(  # type: ignore[union-attr]
            select(ComputeResourceRecord).where(
                ComputeResourceRecord.resource_type.in_(  # type: ignore[union-attr]
                    ["all_purpose_cluster", "job_cluster"]
                ),
                ComputeResourceRecord.state == "RUNNING",
            )
        ).all()

        # Get most recent utilization snapshot per cluster
        util_rows = self.session.exec(  # type: ignore[union-attr]
            select(ClusterUtilizationSnapshot).order_by(
                ClusterUtilizationSnapshot.captured_at.desc()
            )  # type: ignore[union-attr]
        ).all()
        util_by_cluster: dict[str, ClusterUtilizationSnapshot] = {}
        for u in util_rows:
            if u.cluster_id not in util_by_cluster:
                util_by_cluster[u.cluster_id] = u

        for cluster in clusters:
            util = util_by_cluster.get(cluster.resource_id)
            if not util:
                continue
            idle_minutes = util.idle_hours * 60
            if idle_minutes > idle_minutes_threshold:
                recommendations.append(
                    Recommendation(
                        type="idle_cluster",
                        risk_level=RiskLevel.LOW,
                        resource_id=cluster.resource_id,
                        resource_name=cluster.resource_name,
                        title=(
                            f"Idle cluster: '{cluster.resource_name}'"
                            f" ({idle_minutes:.0f} min idle)"
                        ),
                        description=(
                            f"Cluster has been idle for"
                            f" {idle_minutes:.0f} minutes."
                            f" Threshold: {idle_minutes_threshold} min."
                        ),
                        current_state={
                            "state": "RUNNING",
                            "idle_minutes": idle_minutes,
                        },
                        recommended_state={"state": "TERMINATED"},
                        estimated_monthly_savings_usd=Decimal(str(util.total_cost_usd))
                        if util.total_cost_usd
                        else Decimal("100"),
                        savings_confidence=SavingsConfidence.HIGH,
                        pricing_basis=basis,
                    )
                )

        return AnalysisResult(
            analyzer_name=self.name,
            provider=self.context.config.provider,
            recommendations=recommendations,
            summary={
                "running_clusters": len(clusters),
                "idle_clusters": len(recommendations),
            },
        )

    def _analyze_from_api(self) -> AnalysisResult:
        """Fallback: analyze from provider API (legacy path)."""
        compute = self.context.provider.get_compute_client()
        clusters = compute.list_clusters()
        thresholds = self.context.config.thresholds.idle_resources
        recommendations = []
        basis = self.pricing_basis()

        if not self.rule_enabled("idle_cluster"):
            return AnalysisResult(
                analyzer_name=self.name,
                provider=self.context.config.provider,
                recommendations=[],
                summary={
                    "running_clusters": len(
                        [c for c in clusters if c.state == "RUNNING"]
                    ),
                    "idle_clusters": 0,
                },
            )

        idle_minutes_threshold = self.rule_threshold(
            "idle_cluster", "idle_cluster_minutes", thresholds.idle_cluster_minutes
        )

        for cluster in clusters:
            if cluster.state != "RUNNING":
                continue
            util = compute.get_utilization(cluster.cluster_id)
            idle_minutes = util.idle_hours * 60
            if idle_minutes > idle_minutes_threshold:
                recommendations.append(
                    Recommendation(
                        type="idle_cluster",
                        risk_level=RiskLevel.LOW,
                        resource_id=cluster.cluster_id,
                        resource_name=cluster.cluster_name,
                        title=(
                            f"Idle cluster: '{cluster.cluster_name}' ({idle_minutes:.0f} min idle)"
                        ),
                        description=(
                            f"Cluster has been idle for"
                            f" {idle_minutes:.0f} minutes."
                            f" Threshold: {idle_minutes_threshold} min."
                        ),
                        current_state={
                            "state": "RUNNING",
                            "idle_minutes": idle_minutes,
                        },
                        recommended_state={"state": "TERMINATED"},
                        estimated_monthly_savings_usd=Decimal(str(util.total_cost_usd))
                        if util.total_cost_usd
                        else Decimal("100"),
                        savings_confidence=SavingsConfidence.HIGH,
                        pricing_basis=basis,
                    )
                )

        return AnalysisResult(
            analyzer_name=self.name,
            provider=self.context.config.provider,
            recommendations=recommendations,
            summary={
                "running_clusters": len([c for c in clusters if c.state == "RUNNING"]),
                "idle_clusters": len(recommendations),
            },
        )
