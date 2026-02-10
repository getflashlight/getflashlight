"""Scheduled analysis runner.

Runs all analyzers on a configurable schedule and stores
recommendations in the ``recommendations`` table for historical
tracking and the dashboard.
"""

from __future__ import annotations

from datetime import UTC, datetime

import structlog
from auralake_shared.core.context import ExecutionContext
from sqlmodel import Session

from auralake_backend.analyzers.cluster_analyzer import ClusterAnalyzer
from auralake_backend.analyzers.cost_analyzer import CostAnalyzer
from auralake_backend.analyzers.delta_analyzer import DeltaAnalyzer
from auralake_backend.analyzers.idle_resource_analyzer import IdleResourceAnalyzer
from auralake_backend.analyzers.s3_tag_analyzer import S3TagAnalyzer
from auralake_backend.analyzers.spot_analyzer import SpotAnalyzer
from auralake_backend.db.models import AnalysisRun, RecommendationRecord

logger = structlog.get_logger(__name__)

# Analyzers to run in scheduled mode
SCHEDULED_ANALYZERS = [
    CostAnalyzer,
    ClusterAnalyzer,
    SpotAnalyzer,
    IdleResourceAnalyzer,
    DeltaAnalyzer,
    S3TagAnalyzer,
]


class AnalysisScheduler:
    """Runs analyzers and persists results to the database."""

    def __init__(self, context: ExecutionContext, session: Session) -> None:
        self.context = context
        self.session = session

    def run_all(self) -> dict[str, int]:
        """Run all scheduled analyzers and store recommendations.

        Returns a dict mapping analyzer name → recommendation count.
        """
        results: dict[str, int] = {}

        for analyzer_cls in SCHEDULED_ANALYZERS:
            name = analyzer_cls.name
            try:
                count = self._run_analyzer(analyzer_cls)
                results[name] = count
            except Exception as exc:
                logger.error("scheduled_analysis_failed", analyzer=name, error=str(exc))
                results[name] = 0

        logger.info("scheduled_analysis_completed", results=results)
        return results

    def _run_analyzer(self, analyzer_cls: type) -> int:
        """Run a single analyzer and persist its recommendations."""
        analyzer = analyzer_cls(self.context)
        name = analyzer.name
        started_at = datetime.now(UTC)

        # Create analysis run record
        run = AnalysisRun(
            analyzer_name=name,
            provider=self.context.config.provider,
            config_snapshot={},
            started_at=started_at,
            status="running",
        )
        self.session.add(run)
        self.session.commit()
        self.session.refresh(run)

        try:
            result = analyzer.analyze()
            run.completed_at = datetime.now(UTC)
            run.status = "completed"
            run.summary = result.summary

            # Persist recommendations
            for rec in result.recommendations:
                record = RecommendationRecord(
                    analysis_run_id=run.id,
                    type=rec.type,
                    risk_level=rec.risk_level.value,
                    resource_id=rec.resource_id,
                    resource_name=rec.resource_name,
                    workspace_id=rec.workspace_id,
                    title=rec.title,
                    description=rec.description,
                    current_state=rec.current_state,
                    recommended_state=rec.recommended_state,
                    estimated_monthly_savings_usd=float(rec.estimated_monthly_savings_usd),
                    savings_confidence=rec.savings_confidence.value,
                    evidence=rec.evidence,
                    status="pending",
                )
                self.session.add(record)

            self.session.add(run)
            self.session.commit()

            logger.info(
                "analyzer_completed",
                analyzer=name,
                recommendations=len(result.recommendations),
            )
            return len(result.recommendations)

        except Exception as exc:
            run.completed_at = datetime.now(UTC)
            run.status = "failed"
            run.summary = {"error": str(exc)}
            self.session.add(run)
            self.session.commit()
            raise
