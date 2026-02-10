"""Execution context threaded through every CLI command.

:class:`ExecutionContext` bundles the validated configuration, active
provider, and runtime flags so that individual commands and actions can
operate without global state.
"""

from __future__ import annotations

from dataclasses import dataclass

from auralake_shared.models.config import AuraLakeConfig, AutomationLevel
from auralake_shared.providers.base import AbstractProvider


@dataclass
class ExecutionContext:
    """Immutable bag of state for a single CLI invocation.

    Attributes
    ----------
    config:
        Validated application configuration.
    provider:
        The active lakehouse provider instance.
    automation_level:
        Controls how aggressively actions are auto-applied.
    dry_run:
        When *True*, no mutating operations are performed.
    create_pr:
        When *True*, changes are submitted as a pull request instead of
        being applied directly.
    verbose:
        Enables debug-level logging output.
    workspace:
        Optional workspace or catalog scope for multi-workspace providers.
    """

    config: AuraLakeConfig
    provider: AbstractProvider
    automation_level: AutomationLevel
    dry_run: bool = False
    create_pr: bool = False
    verbose: bool = False
    workspace: str | None = None
