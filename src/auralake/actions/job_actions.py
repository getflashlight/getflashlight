"""Job optimization actions."""
from __future__ import annotations

from pathlib import Path
from datetime import datetime

from auralake.actions.base import AbstractAction, RiskLevel
from auralake.core.logging import get_logger
from auralake.models.recommendations import Recommendation

logger = get_logger(__name__)


class ConsolidateJobsAction(AbstractAction):
    name = "consolidate_jobs"
    risk_level = RiskLevel.MEDIUM

    def execute(self, recommendation: Recommendation) -> None:
        """Consolidate jobs via DAB config changes and optional PR."""
        if self.context.create_pr:
            self._create_pr(recommendation)
        else:
            logger.info("consolidation_recommendation", title=recommendation.title)

    def _create_pr(self, recommendation: Recommendation) -> None:
        from auralake.git_integration.repo import RepoManager
        from auralake.git_integration.pr_builder import PRBuilder
        from auralake.git_integration.diff_renderer import render_pr_body

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

    def rollback(self, recommendation: Recommendation) -> None:
        logger.warning("consolidation_rollback_not_supported")


class CancelJobRunAction(AbstractAction):
    name = "cancel_job_run"
    risk_level = RiskLevel.LOW

    def execute(self, recommendation: Recommendation) -> None:
        job_client = self.context.provider.get_job_client()
        run_id = recommendation.recommended_state.get("run_id")
        if run_id:
            job_client.cancel_run(run_id)
            logger.info("run_cancelled", run_id=run_id)

    def rollback(self, recommendation: Recommendation) -> None:
        logger.warning("cannot_rollback_cancel")
