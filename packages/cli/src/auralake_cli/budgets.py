"""Budget management and alerts commands."""

from __future__ import annotations

from typing import Annotated

import typer
from auralake_shared.core.output import OutputFormat, print_warning

from auralake_cli._options import (
    OutputOption,
    ServerOption,
    WorkspaceOption,
)

budgets_app = typer.Typer(no_args_is_help=True)


@budgets_app.command("list")
def list_budgets(
    server: ServerOption = None,
    workspace: WorkspaceOption = None,
    output: OutputOption = OutputFormat.TABLE,
) -> None:
    """List all configured budgets and their current status."""
    print_warning("Budget listing not yet implemented.")


@budgets_app.command("create")
def create_budget(
    server: ServerOption = None,
    name: Annotated[
        str,
        typer.Option("--name", "-n", help="Budget name."),
    ] = "",
    amount: Annotated[
        float,
        typer.Option("--amount", help="Monthly budget amount in dollars."),
    ] = 0.0,
    scope: Annotated[
        str | None,
        typer.Option("--scope", help="Budget scope: workspace, team, project, tag."),
    ] = None,
) -> None:
    """Create a new budget with alert thresholds."""
    print_warning("Budget creation not yet implemented.")


@budgets_app.command("update")
def update_budget(
    budget_id: Annotated[str, typer.Argument(help="Budget ID to update.")],
    server: ServerOption = None,
    amount: Annotated[
        float | None,
        typer.Option("--amount", help="Updated monthly budget amount in dollars."),
    ] = None,
    name: Annotated[
        str | None,
        typer.Option("--name", "-n", help="Updated budget name."),
    ] = None,
) -> None:
    """Update an existing budget configuration."""
    print_warning("Budget update not yet implemented.")


@budgets_app.command("alerts")
def budget_alerts(
    server: ServerOption = None,
    workspace: WorkspaceOption = None,
    output: OutputOption = OutputFormat.TABLE,
) -> None:
    """List budget alerts and threshold breaches."""
    print_warning("Budget alerts not yet implemented.")
