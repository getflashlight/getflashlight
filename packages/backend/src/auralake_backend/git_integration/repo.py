"""Git repository management for PR workflows."""

from __future__ import annotations

import os
from pathlib import Path

from auralake_shared.core.exceptions import GitIntegrationError
from auralake_shared.core.logging import get_logger
from auralake_shared.models.config import GitHubConfig
from git import Repo

logger = get_logger(__name__)


class RepoManager:
    """Manages a local clone of the GitHub monorepo."""

    def __init__(self, github_config: GitHubConfig) -> None:
        self._config = github_config
        self._repo: Repo | None = None

    @property
    def repo(self) -> Repo:
        if self._repo is None:
            raise GitIntegrationError("Repository not initialized. Call ensure_repo() first.")
        return self._repo

    @property
    def repo_path(self) -> Path:
        return Path(self.repo.working_dir)

    def ensure_repo(self) -> Path:
        """Ensure local clone exists and is up-to-date. Returns the repo path."""
        if self._config.local_path:
            path = Path(self._config.local_path)
            if path.exists() and (path / ".git").exists():
                self._repo = Repo(str(path))
                self._pull()
                return path
            raise GitIntegrationError(f"Local path {path} is not a valid git repo")

        # Clone from GitHub
        clone_dir = Path.home() / ".auralake" / "repos" / self._config.repo.replace("/", "_")
        if clone_dir.exists() and (clone_dir / ".git").exists():
            self._repo = Repo(str(clone_dir))
            self._pull()
        else:
            clone_dir.parent.mkdir(parents=True, exist_ok=True)
            token = os.environ.get(self._config.token_env, "")
            url = (
                f"https://{token}@github.com/{self._config.repo}.git"
                if token
                else f"https://github.com/{self._config.repo}.git"
            )
            try:
                self._repo = Repo.clone_from(url, str(clone_dir))
            except Exception as exc:
                raise GitIntegrationError(f"Failed to clone {self._config.repo}: {exc}") from exc

        return clone_dir

    def create_branch(self, branch_name: str) -> str:
        """Create and checkout a new branch from base_branch."""
        try:
            base = self._config.base_branch
            self.repo.git.checkout(base)
            self._pull()
            self.repo.git.checkout("-b", branch_name)
            logger.info("branch_created", branch=branch_name)
            return branch_name
        except Exception as exc:
            raise GitIntegrationError(f"Failed to create branch '{branch_name}': {exc}") from exc

    def commit(self, message: str, files: list[str] | None = None) -> str:
        """Stage files and commit. Returns the commit hash."""
        try:
            if files:
                self.repo.index.add(files)
            else:
                self.repo.git.add("-A")
            self.repo.index.commit(message)
            return str(self.repo.head.commit.hexsha)
        except Exception as exc:
            raise GitIntegrationError(f"Failed to commit: {exc}") from exc

    def push(self, branch_name: str) -> None:
        """Push branch to remote."""
        try:
            self.repo.git.push("origin", branch_name, "--set-upstream")
            logger.info("branch_pushed", branch=branch_name)
        except Exception as exc:
            raise GitIntegrationError(f"Failed to push branch '{branch_name}': {exc}") from exc

    def _pull(self) -> None:
        """Pull latest from remote."""
        try:
            self.repo.git.pull("--ff-only")
        except Exception:
            logger.warning(
                "pull_failed", msg="Could not fast-forward pull; proceeding with current state"
            )
