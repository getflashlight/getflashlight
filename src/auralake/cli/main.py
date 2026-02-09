"""Aura Lake CLI — lakehouse cost optimization tool."""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer

from auralake.cli._options import (
    ConfigOption,
    OutputOption,
    ProviderOption,
    VerboseOption,
    WorkspaceOption,
)
from auralake.core.config import load_config
from auralake.core.context import ExecutionContext
from auralake.core.logging import setup_logging
from auralake.core.output import OutputFormat, print_error
from auralake.models.config import AutomationLevel
from auralake.providers import get_provider

app = typer.Typer(
    name="auralake",
    help="Aura Lake — multi-platform lakehouse cost optimization CLI.",
    no_args_is_help=True,
    rich_markup_mode="rich",
)


def build_context(
    config_path: str | None = None,
    provider: str | None = None,
    workspace: str | None = None,
    output: OutputFormat = OutputFormat.TABLE,
    verbose: bool = False,
    dry_run: bool = False,
    apply: bool = False,
    auto: bool = False,
    create_pr: bool = False,
) -> ExecutionContext:
    """Build an ExecutionContext from CLI flags + config file."""
    cfg = load_config(Path(config_path) if config_path else None)

    provider_name = provider or cfg.provider
    prov = get_provider(provider_name, cfg)

    if auto:
        level = AutomationLevel.AUTO
    elif apply:
        level = AutomationLevel.APPLY
    elif dry_run:
        level = AutomationLevel.DRY_RUN
    else:
        level = cfg.defaults.automation_level

    setup_logging(verbose)

    return ExecutionContext(
        config=cfg,
        provider=prov,
        automation_level=level,
        output_format=output.value,
        dry_run=dry_run,
        create_pr=create_pr,
        verbose=verbose,
        workspace=workspace,
    )


# Import and register sub-command groups
from auralake.cli.cost import cost_app
from auralake.cli.clusters import clusters_app
from auralake.cli.resources import resources_app
from auralake.cli.spot import spot_app
from auralake.cli.delta import delta_app
from auralake.cli.jobs import jobs_app
from auralake.cli.query import query_app
from auralake.cli.policies import policies_app
from auralake.cli.budgets import budgets_app
from auralake.cli.tags import tags_app
from auralake.cli.routing import routing_app
from auralake.cli.agent import agent_app
from auralake.cli.db import db_app

app.add_typer(cost_app, name="cost", help="Cost analysis and reporting.")
app.add_typer(clusters_app, name="clusters", help="Cluster analysis and optimization.")
app.add_typer(resources_app, name="resources", help="Idle/unused resource management.")
app.add_typer(spot_app, name="spot", help="Spot instance optimization.")
app.add_typer(delta_app, name="delta", help="Delta Lake table maintenance.")
app.add_typer(jobs_app, name="jobs", help="Job/workflow optimization.")
app.add_typer(query_app, name="query", help="Query analysis and optimization.")
app.add_typer(policies_app, name="policies", help="Cluster policy management.")
app.add_typer(budgets_app, name="budgets", help="Budget management and alerts.")
app.add_typer(tags_app, name="tags", help="Tag governance.")
app.add_typer(routing_app, name="routing", help="Workload portability analysis.")
app.add_typer(agent_app, name="agent", help="Collector agent management.")
app.add_typer(db_app, name="db", help="Database management.")


@app.callback()
def main_callback() -> None:
    """Aura Lake — multi-platform lakehouse cost optimization."""
