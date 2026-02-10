"""Workload portability / runtime routing analysis."""

from __future__ import annotations

from auralake_shared.models.recommendations import AnalysisResult
from auralake_shared.models.routing import DatabricksFeatureDependency, WorkloadProfile

from auralake_backend.analyzers.base import AbstractAnalyzer


class WorkloadAnalyzer(AbstractAnalyzer):
    name = "workload"

    # Databricks-specific features that indicate lock-in
    PROPRIETARY_FEATURES = {
        "dbutils": "Databricks Utilities (dbutils.* calls)",
        "display()": "Databricks display() function",
        "DLT": "Delta Live Tables",
        "photon": "Photon engine",
        "unity_catalog": "Unity Catalog",
        "autoloader": "Auto Loader (cloudFiles)",
        "mlflow.databricks": "Databricks MLflow integration",
    }

    def analyze(self) -> AnalysisResult:
        job_client = self.context.provider.get_job_client()
        jobs = job_client.list_jobs()

        profiles = []
        for job in jobs:
            features_used = job.databricks_features_used or []
            deps = []
            for feature in features_used:
                if feature in self.PROPRIETARY_FEATURES:
                    deps.append(
                        DatabricksFeatureDependency(
                            feature=feature,
                            usage_count=1,
                            portable_alternative=self._get_alternative(feature),
                        )
                    )

            score = max(0.0, 1.0 - (len(deps) * 0.2))
            profiles.append(
                WorkloadProfile(
                    job_id=job.job_id,
                    job_name=job.job_name,
                    is_portable=len(deps) == 0,
                    feature_dependencies=deps,
                    portability_score=score,
                )
            )

        portable = len([p for p in profiles if p.is_portable])
        return AnalysisResult(
            analyzer_name=self.name,
            provider=self.context.config.provider,
            recommendations=[],
            summary={
                "total_jobs": len(profiles),
                "portable_jobs": portable,
                "locked_in_jobs": len(profiles) - portable,
                "avg_portability_score": sum(p.portability_score for p in profiles) / len(profiles)
                if profiles
                else 0,
            },
        )

    @staticmethod
    def _get_alternative(feature: str) -> str | None:
        alternatives = {
            "dbutils": "Use standard Python os/pathlib for file ops",
            "display()": "Use print() or matplotlib/plotly",
            "DLT": "Use Apache Airflow + Spark Structured Streaming",
            "photon": "Standard Spark (compatible SQL)",
            "autoloader": "Spark Structured Streaming with file source",
            "mlflow.databricks": "Standard MLflow OSS",
        }
        return alternatives.get(feature)
