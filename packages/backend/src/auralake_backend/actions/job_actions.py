"""Job optimization actions."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from auralake_shared.core.logging import get_logger
from auralake_shared.models.recommendations import ActionResult, Recommendation

from auralake_backend.actions.base import AbstractAction, RiskLevel

logger = get_logger(__name__)


class ConsolidateJobsAction(AbstractAction):
    name = "consolidate_jobs"
    risk_level = RiskLevel.MEDIUM

    def execute(self, recommendation: Recommendation) -> ActionResult:
        """Consolidate jobs via DAB config changes and optional PR."""
        if self.context.create_pr:
            return self._create_pr(recommendation)
        logger.info("consolidation_recommendation", title=recommendation.title)
        return ActionResult(
            action_type=self.name,
            resource_id=recommendation.resource_id,
            resource_name=recommendation.resource_name,
            status="applied",
            detail="Recommendation logged (no PR requested)",
        )

    def _create_pr(self, recommendation: Recommendation) -> ActionResult:
        from auralake_backend.git_integration.diff_renderer import render_pr_body
        from auralake_backend.git_integration.pr_builder import PRBuilder
        from auralake_backend.git_integration.repo import RepoManager

        github_config = self.context.config.github
        repo_mgr = RepoManager(github_config)
        repo_mgr.ensure_repo()

        timestamp = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
        branch = f"auralake/consolidate/{timestamp}"
        repo_mgr.create_branch(branch)

        # Apply DAB changes
        diffs = []
        for change in recommendation.recommended_state.get("dab_changes", []):
            config_format = self.context.provider.get_config_format()
            config_format.modify_job(
                Path(change["file_path"]),
                change["job_key"],
                change["changes"],
            )

        commit_msg = (
            f"auralake: {recommendation.title}\n\n"
            f"Estimated savings: ${recommendation.estimated_monthly_savings_usd}/mo"
        )
        repo_mgr.commit(commit_msg)
        repo_mgr.push(branch)

        pr_body = render_pr_body(
            summary=recommendation.description,
            diffs=diffs,
            estimated_savings=float(recommendation.estimated_monthly_savings_usd),
            evidence=recommendation.evidence,
        )
        pr_builder = PRBuilder(github_config)
        pr_url = pr_builder.create_pr(branch, recommendation.title, pr_body)
        recommendation.pr_url = pr_url
        logger.info("consolidation_pr_created", url=pr_url)
        return ActionResult(
            action_type=self.name,
            resource_id=recommendation.resource_id,
            resource_name=recommendation.resource_name,
            status="applied",
            detail="PR created",
            pr_url=pr_url,
        )

    def rollback(self, recommendation: Recommendation) -> None:
        logger.warning("consolidation_rollback_not_supported")


class CancelJobRunAction(AbstractAction):
    name = "cancel_job_run"
    risk_level = RiskLevel.LOW

    def execute(self, recommendation: Recommendation) -> ActionResult:
        job_client = self.context.provider.get_job_client()
        run_id = recommendation.recommended_state.get("run_id")
        if run_id:
            try:
                job_client.cancel_run(run_id)
                logger.info("run_cancelled", run_id=run_id)
                return ActionResult(
                    action_type=self.name,
                    resource_id=recommendation.resource_id,
                    resource_name=recommendation.resource_name,
                    status="applied",
                    detail=f"Run {run_id} cancelled",
                )
            except Exception as exc:
                return ActionResult(
                    action_type=self.name,
                    resource_id=recommendation.resource_id,
                    resource_name=recommendation.resource_name,
                    status="failed",
                    error=str(exc),
                )
        return ActionResult(
            action_type=self.name,
            resource_id=recommendation.resource_id,
            resource_name=recommendation.resource_name,
            status="skipped",
            detail="No run_id specified",
        )

    def rollback(self, recommendation: Recommendation) -> None:
        logger.warning("cannot_rollback_cancel")
