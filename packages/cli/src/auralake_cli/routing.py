"""Workload portability and routing analysis commands."""

from __future__ import annotations

import typer
from auralake_shared.core.output import OutputFormat, print_warning, render_table

from auralake_cli._options import (
    OutputOption,
    ServerOption,
    WorkspaceOption,
)

routing_app = typer.Typer(no_args_is_help=True)


@routing_app.command("analyze")
def routing_analyze(
    server: ServerOption = None,
    workspace: WorkspaceOption = None,
    output: OutputOption = OutputFormat.TABLE,
) -> None:
    """Analyze workloads for cross-provider routing opportunities."""
    from auralake_cli.main import build_client

    client = build_client(server)
    result = client.routing_analyze(workspace=workspace)

    render_table(
        "Workload Portability Analysis",
        ["Metric", "Value"],
        [[k, v] for k, v in result.summary.items()],
        output,
    )


@routing_app.command("compare")
def routing_compare(
    server: ServerOption = None,
    workspace: WorkspaceOption = None,
    output: OutputOption = OutputFormat.TABLE,
) -> None:
    """Compare costs across providers for equivalent workloads."""
    print_warning("Routing comparison not yet implemented.")
