"""Cost analysis and reporting commands."""

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
    DaysOption,
    OutputOption,
    ServerOption,
    WorkspaceOption,
)

cost_app = typer.Typer(no_args_is_help=True)


@cost_app.command("report")
def cost_report(
    server: ServerOption = None,
    workspace: WorkspaceOption = None,
    output: OutputOption = OutputFormat.TABLE,
    days: DaysOption = 30,
) -> None:
    """Generate a cost report by tag/team/project."""
    from auralake_cli.main import build_client

    client = build_client(server)
    result = client.cost_report(workspace=workspace)
    render_table(
        "Cost Report",
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


@cost_app.command("breakdown")
def cost_breakdown(
    server: ServerOption = None,
    workspace: WorkspaceOption = None,
    output: OutputOption = OutputFormat.TABLE,
    days: DaysOption = 30,
    by: Annotated[
        str,
        typer.Option("--by", help="Breakdown dimension: sku, workspace, team, tag."),
    ] = "sku",
) -> None:
    """Show cost breakdown by dimension (SKU, workspace, team, tag)."""
    from auralake_cli.main import build_client

    client = build_client(server)
    data = client.cost_breakdown(days=days, by=by, workspace=workspace)
    # data is a dict from the server
    render_table(
        f"Cost Breakdown by {by.upper()}",
        ["Dimension", "Cost (USD)"],
        [
            [k, f"${v:.2f}" if isinstance(v, (int, float)) else str(v)]
            for k, v in data.items()
            if k != "total_cost_usd"
        ],
        output,
    )


@cost_app.command("trend")
def cost_trend(
    server: ServerOption = None,
    workspace: WorkspaceOption = None,
    output: OutputOption = OutputFormat.TABLE,
    days: DaysOption = 90,
    granularity: Annotated[
        str,
        typer.Option("--granularity", "-g", help="Time granularity: daily, weekly, monthly."),
    ] = "daily",
) -> None:
    """Display cost trends over time."""
    print_warning("Cost trend not yet implemented.")


@cost_app.command("forecast")
def cost_forecast(
    server: ServerOption = None,
    workspace: WorkspaceOption = None,
    output: OutputOption = OutputFormat.TABLE,
    days: DaysOption = 30,
    horizon: Annotated[
        int,
        typer.Option("--horizon", help="Forecast horizon in days."),
    ] = 30,
) -> None:
    """Forecast future costs based on historical trends."""
    print_warning("Cost forecast not yet implemented.")


@cost_app.command("tco")
def cost_tco(
    server: ServerOption = None,
    workspace: WorkspaceOption = None,
    output: OutputOption = OutputFormat.TABLE,
    days: DaysOption = 30,
) -> None:
    """Calculate total cost of ownership (DBU + infrastructure)."""
    from auralake_cli.main import build_client

    client = build_client(server)
    result = client.cost_tco(workspace=workspace)
    summary = result.summary
    render_table(
        "Total Cost of Ownership",
        ["Metric", "Value"],
        [
            ["DBU Cost", f"${summary.get('total_dbu_usd', '0')}"],
            ["EC2 Cost", f"${summary.get('total_ec2_usd', '0')}"],
            ["Storage Cost", f"${summary.get('total_storage_usd', '0')}"],
            ["Transfer Cost", f"${summary.get('total_transfer_usd', '0')}"],
            ["Total TCO", f"${summary.get('total_tco_usd', '0')}"],
            ["Clusters", str(summary.get("cluster_count", 0))],
        ],
        output,
    )


@cost_app.command("infra")
def cost_infra(
    server: ServerOption = None,
    workspace: WorkspaceOption = None,
    output: OutputOption = OutputFormat.TABLE,
    days: DaysOption = 30,
) -> None:
    """Show infrastructure (cloud) costs alongside DBU costs."""
    from auralake_cli.main import build_client

    client = build_client(server)
    result = client.cost_infra(workspace=workspace)
    render_table(
        "Infrastructure Cost Report",
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
