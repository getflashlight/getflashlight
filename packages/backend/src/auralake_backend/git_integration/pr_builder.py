"""Pull request creation via GitHub API."""

from __future__ import annotations

import os

from auralake_shared.core.exceptions import GitIntegrationError
from auralake_shared.core.logging import get_logger
from auralake_shared.models.config import GitHubConfig
from github import Github

logger = get_logger(__name__)


class PRBuilder:
    """Creates pull requests on GitHub."""

    def __init__(self, github_config: GitHubConfig) -> None:
        self._config = github_config
        token = github_config.token or os.environ.get(github_config.token_env, "")
        if not token:
            raise GitIntegrationError(
                f"GitHub token not found in env var '{github_config.token_env}'"
            )
        self._gh = Github(token)

    def create_pr(
        self,
        branch: str,
        title: str,
        body: str,
    ) -> str:
        """Create a PR and return the HTML URL."""
        try:
            repo = self._gh.get_repo(self._config.repo)
            pr = repo.create_pull(
                title=title,
                body=body,
                head=branch,
                base=self._config.base_branch,
            )
            # Add labels
            if self._config.pr_labels:
                pr.add_to_labels(*self._config.pr_labels)

            logger.info("pr_created", url=pr.html_url, number=pr.number)
            return pr.html_url
        except Exception as exc:
            raise GitIntegrationError(f"Failed to create PR: {exc}") from exc
