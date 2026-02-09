"""Budget management and alerts commands."""
from __future__ import annotations

from typing import Annotated, Optional

import typer

from auralake.cli._options import (
    ConfigOption,
    OutputOption,
    ProviderOption,
    VerboseOption,
    WorkspaceOption,
)
from auralake.core.output import OutputFormat, print_warning, render_table

budgets_app = typer.Typer(no_args_is_help=True)


@budgets_app.command("list")
def list_budgets(
    provider: ProviderOption = None,
    config: ConfigOption = None,
    workspace: WorkspaceOption = None,
    output: OutputOption = OutputFormat.TABLE,
    verbose: VerboseOption = False,
) -> None:
    """List all configured budgets and their current status."""
    from auralake.cli.main import build_context

    ctx = build_context(config, provider, workspace, output, verbose)
    # TODO: implement budget listing with database-backed budgets
    print_warning("Budget listing not yet implemented.")


@budgets_app.command("create")
def create_budget(
    provider: ProviderOption = None,
    config: ConfigOption = None,
    workspace: WorkspaceOption = None,
    verbose: VerboseOption = False,
    name: Annotated[
        str,
        typer.Option("--name", "-n", help="Budget name."),
    ] = "",
    amount: Annotated[
        float,
        typer.Option("--amount", help="Monthly budget amount in dollars."),
    ] = 0.0,
    scope: Annotated[
        Optional[str],
        typer.Option("--scope", help="Budget scope: workspace, team, project, tag."),
    ] = None,
) -> None:
    """Create a new budget with alert thresholds."""
    from auralake.cli.main import build_context

    ctx = build_context(config, provider, workspace, OutputFormat.TABLE, verbose)
    # TODO: implement budget creation with database persistence
    print_warning("Budget creation not yet implemented.")


@budgets_app.command("update")
def update_budget(
    budget_id: Annotated[str, typer.Argument(help="Budget ID to update.")],
    provider: ProviderOption = None,
    config: ConfigOption = None,
    workspace: WorkspaceOption = None,
    verbose: VerboseOption = False,
    amount: Annotated[
        Optional[float],
        typer.Option("--amount", help="Updated monthly budget amount in dollars."),
    ] = None,
    name: Annotated[
        Optional[str],
        typer.Option("--name", "-n", help="Updated budget name."),
    ] = None,
) -> None:
    """Update an existing budget configuration."""
    from auralake.cli.main import build_context

    ctx = build_context(config, provider, workspace, OutputFormat.TABLE, verbose)
    # TODO: implement budget update with database persistence
    print_warning("Budget update not yet implemented.")


@budgets_app.command("alerts")
def budget_alerts(
    provider: ProviderOption = None,
    config: ConfigOption = None,
    workspace: WorkspaceOption = None,
    output: OutputOption = OutputFormat.TABLE,
    verbose: VerboseOption = False,
    status: Annotated[
        Optional[str],
        typer.Option("--status", "-s", help="Filter by alert status: active, acknowledged, all."),
    ] = None,
) -> None:
    """List budget alerts and threshold breaches."""
    from auralake.cli.main import build_context

    ctx = build_context(config, provider, workspace, output, verbose)
    # TODO: implement budget alerts with database-backed alerting
    print_warning("Budget alerts not yet implemented.")
