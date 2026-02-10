"""Delta Lake table maintenance commands."""

from __future__ import annotations

from typing import Annotated

import typer
from auralake_shared.core.output import (
    OutputFormat,
    print_warning,
    render_recommendations,
    render_table,
)

from auralake_cli._options import (
    OutputOption,
    ServerOption,
    WorkspaceOption,
)

delta_app = typer.Typer(no_args_is_help=True)


@delta_app.command("scan")
def delta_scan(
    server: ServerOption = None,
    workspace: WorkspaceOption = None,
    output: OutputOption = OutputFormat.TABLE,
) -> None:
    """Scan Delta tables for maintenance opportunities (small files, stale stats)."""
    from auralake_cli.main import build_client

    client = build_client(server)
    result = client.delta_scan(workspace=workspace)

    render_table(
        "Delta Table Scan",
        ["Metric", "Value"],
        [[k, v] for k, v in result.summary.items()],
        output,
    )
    if result.recommendations:
        render_recommendations(
            [
                {
                    "resource": r.resource_name,
                    "recommendation": r.title,
                    "estimated_savings": f"${r.estimated_monthly_savings_usd:.2f}/mo",
                    "priority": r.risk_level,
                }
                for r in result.recommendations
            ],
            output,
        )


@delta_app.command("optimize")
def delta_optimize(
    server: ServerOption = None,
    workspace: WorkspaceOption = None,
    table: Annotated[
        str | None,
        typer.Option("--table", "-t", help="Fully qualified table name to optimize."),
    ] = None,
) -> None:
    """Run OPTIMIZE on Delta tables that need compaction."""
    if not table:
        print_warning("Please specify a table with --table.")
        return

    from auralake_cli.main import build_client

    client = build_client(server)
    result = client.delta_optimize(table=table, workspace=workspace)

    render_table(
        "Optimize Result",
        ["Property", "Value"],
        [
            ["Action", result.action_type],
            ["Resource", result.resource_name],
            ["Status", result.status],
            ["Detail", result.detail or ""],
            ["Error", result.error or ""],
        ],
        OutputFormat.TABLE,
    )


@delta_app.command("vacuum")
def delta_vacuum(
    server: ServerOption = None,
    workspace: WorkspaceOption = None,
    table: Annotated[
        str | None,
        typer.Option("--table", "-t", help="Fully qualified table name to vacuum."),
    ] = None,
    retention_hours: Annotated[
        int,
        typer.Option("--retention-hours", help="Retention period in hours for VACUUM."),
    ] = 168,
) -> None:
    """Run VACUUM on Delta tables to reclaim storage."""
    if not table:
        print_warning("Please specify a table with --table.")
        return

    from auralake_cli.main import build_client

    client = build_client(server)
    result = client.delta_vacuum(
        table=table,
        retention_hours=retention_hours,
        workspace=workspace,
    )

    render_table(
        "Vacuum Result",
        ["Property", "Value"],
        [
            ["Action", result.action_type],
            ["Resource", result.resource_name],
            ["Status", result.status],
            ["Detail", result.detail or ""],
            ["Error", result.error or ""],
        ],
        OutputFormat.TABLE,
    )


@delta_app.command("zorder")
def delta_zorder(
    server: ServerOption = None,
    workspace: WorkspaceOption = None,
    table: Annotated[
        str | None,
        typer.Option("--table", "-t", help="Fully qualified table name to Z-ORDER."),
    ] = None,
    columns: Annotated[
        str | None,
        typer.Option("--columns", help="Comma-separated columns for Z-ORDER."),
    ] = None,
) -> None:
    """Apply Z-ORDER optimization to Delta tables."""
    print_warning("Delta Z-ORDER not yet implemented.")
