"""Job analysis with consolidation and bin-packing."""

from __future__ import annotations

from decimal import Decimal

from auralake_shared.models.jobs import JobProfile
from auralake_shared.models.recommendations import (
    AnalysisResult,
    Recommendation,
    RiskLevel,
    SavingsConfidence,
)

from auralake_backend.analyzers.base import AbstractAnalyzer


class JobAnalyzer(AbstractAnalyzer):
    name = "job"

    def analyze(self) -> AnalysisResult:
        job_client = self.context.provider.get_job_client()
        jobs = job_client.list_jobs()
        thresholds = self.context.config.thresholds.job_consolidation

        recommendations = []

        # Detect stale jobs
        for job in jobs:
            runs = job_client.get_job_runs(job.job_id, limit=5)
            if not runs:
                recommendations.append(
                    Recommendation(
                        type="job_stale",
                        risk_level=RiskLevel.LOW,
                        resource_id=job.job_id,
                        resource_name=job.job_name,
                        title=f"Stale job: '{job.job_name}' has no recent runs",
                        description="This job has no recent run history. Consider removing it.",
                        estimated_monthly_savings_usd=Decimal("10"),
                        savings_confidence=SavingsConfidence.LOW,
                    )
                )

            # Check for failed runs
            failed = [r for r in runs if r.get("state") == "FAILED"]
            if len(failed) >= 3:
                recommendations.append(
                    Recommendation(
                        type="job_failing",
                        risk_level=RiskLevel.MEDIUM,
                        resource_id=job.job_id,
                        resource_name=job.job_name,
                        title=f"Frequently failing job: '{job.job_name}'",
                        description=f"{len(failed)} of last {len(runs)} runs failed.",
                        estimated_monthly_savings_usd=Decimal(str(job.avg_dbu_cost * 30))
                        if job.avg_dbu_cost
                        else Decimal("0"),
                        savings_confidence=SavingsConfidence.MEDIUM,
                        evidence={"failed_runs": len(failed), "total_runs": len(runs)},
                    )
                )

        # Job consolidation analysis
        groups = self._find_consolidation_groups(
            jobs, thresholds.min_group_size, thresholds.max_group_size
        )
        for group in groups:
            savings = self._estimate_consolidation_savings(group)
            if savings >= thresholds.min_savings_dollars:
                job_names = ", ".join(j.job_name for j in group)
                recommendations.append(
                    Recommendation(
                        type="job_consolidation",
                        risk_level=RiskLevel.MEDIUM,
                        resource_id=group[0].job_id,
                        resource_name=f"Group: {len(group)} jobs",
                        title=f"Consolidate {len(group)} jobs onto shared cluster",
                        description=f"Jobs: {job_names}. Compatible instance types and configs.",
                        current_state={"jobs": [j.model_dump() for j in group]},
                        recommended_state={"shared_cluster": True, "job_count": len(group)},
                        estimated_monthly_savings_usd=Decimal(str(savings)),
                        savings_confidence=SavingsConfidence.MEDIUM,
                    )
                )

        return AnalysisResult(
            analyzer_name=self.name,
            provider=self.context.config.provider,
            recommendations=recommendations,
            summary={
                "total_jobs": len(jobs),
                "consolidation_groups": len(groups),
                "recommendations_count": len(recommendations),
            },
        )

    def _find_consolidation_groups(
        self,
        jobs: list[JobProfile],
        min_size: int,
        max_size: int,
    ) -> list[list[JobProfile]]:
        """Bin-pack compatible jobs into groups."""
        groups: list[list[JobProfile]] = []
        ungrouped = list(jobs)

        while ungrouped:
            seed = ungrouped.pop(0)
            group = [seed]

            remaining = []
            for candidate in ungrouped:
                if len(group) >= max_size:
                    remaining.append(candidate)
                    continue
                if self._are_compatible(seed, candidate):
                    group.append(candidate)
                else:
                    remaining.append(candidate)

            ungrouped = remaining
            if len(group) >= min_size:
                groups.append(group)

        return groups

    @staticmethod
    def _are_compatible(job_a: JobProfile, job_b: JobProfile) -> bool:
        """Check if two jobs can share a cluster."""
        # Same or compatible instance type
        if job_a.instance_type and job_b.instance_type:
            if job_a.instance_type != job_b.instance_type:
                return False

        # No conflicting spark configs
        for key in job_a.spark_config:
            if key in job_b.spark_config and job_a.spark_config[key] != job_b.spark_config[key]:
                return False

        return True

    @staticmethod
    def _estimate_consolidation_savings(group: list[JobProfile]) -> float:
        """Estimate savings from consolidating a group of jobs."""
        if len(group) < 2:
            return 0.0
        # Savings = (N-1) * avg driver cost + reduced startup overhead
        avg_cost = sum(j.avg_dbu_cost for j in group) / len(group) if group else 0
        return (len(group) - 1) * avg_cost * 30  # Monthly estimate
