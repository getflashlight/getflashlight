"""Aura Lake CLI — lakehouse cost optimization tool."""

from __future__ import annotations

import os
from pathlib import Path

import typer
from auralake_shared.core.config import load_config
from auralake_shared.core.context import ExecutionContext
from auralake_shared.core.logging import setup_logging
from auralake_shared.models.config import AutomationLevel
from auralake_shared.providers import get_provider

from auralake_cli.agent import agent_app
from auralake_cli.auth import auth_app
from auralake_cli.budgets import budgets_app
from auralake_cli.client import AuralakeClient
from auralake_cli.clusters import clusters_app
from auralake_cli.connections import connections_app
from auralake_cli.cost import cost_app
from auralake_cli.db import db_app
from auralake_cli.delta import delta_app
from auralake_cli.jobs import jobs_app
from auralake_cli.policies import policies_app
from auralake_cli.query import query_app
from auralake_cli.resources import resources_app
from auralake_cli.routing import routing_app
from auralake_cli.spot import spot_app
from auralake_cli.tags import tags_app

app = typer.Typer(
    name="auralake",
    help="Aura Lake — multi-platform lakehouse cost optimization CLI.",
    no_args_is_help=True,
    rich_markup_mode="rich",
)

DEFAULT_SERVER_URL = "http://localhost:8000"


def build_client(server: str | None = None) -> AuralakeClient:
    """Build an HTTP client pointing at the Auralake server.

    Resolution priority (highest to lowest):
    1. Explicit ``server`` argument (from CLI flags)
    2. ``AURALAKE_SERVER_URL`` / ``AURALAKE_API_KEY`` environment variables
    3. ``~/.auralake/credentials.json`` file
    """
    from auralake_cli.auth import load_credentials

    creds = load_credentials()

    url = (
        server
        or os.environ.get("AURALAKE_SERVER_URL")
        or creds.get("server_url")
        or DEFAULT_SERVER_URL
    )
    api_key = os.environ.get("AURALAKE_API_KEY") or creds.get("api_key")
    return AuralakeClient(base_url=url, api_key=api_key)


def build_context(
    config_path: str | None = None,
    provider: str | None = None,
    workspace: str | None = None,
    verbose: bool = False,
    dry_run: bool = False,
    apply: bool = False,
    auto: bool = False,
    create_pr: bool = False,
) -> ExecutionContext:
    """Build an ExecutionContext from CLI flags + config file.

    This is the legacy path used when talking directly to providers
    (without the server). Kept for backward compatibility and local dev.
    """
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
        dry_run=dry_run,
        create_pr=create_pr,
        verbose=verbose,
        workspace=workspace,
    )


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
app.add_typer(auth_app, name="auth", help="Authentication and API key management.")
app.add_typer(connections_app, name="connections", help="Provider connection management.")


@app.callback()
def main_callback() -> None:
    """Aura Lake — multi-platform lakehouse cost optimization."""
