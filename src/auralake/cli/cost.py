"""Cost analysis and reporting commands."""
from __future__ import annotations

from datetime import date, timedelta
from typing import Annotated, Optional

import typer

from auralake.cli._options import (
    ConfigOption,
    DaysOption,
    OutputOption,
    ProviderOption,
    VerboseOption,
    WorkspaceOption,
)
from auralake.core.output import OutputFormat, print_warning, render_recommendations, render_table

cost_app = typer.Typer(no_args_is_help=True)


@cost_app.command("report")
def cost_report(
    provider: ProviderOption = None,
    config: ConfigOption = None,
    workspace: WorkspaceOption = None,
    output: OutputOption = OutputFormat.TABLE,
    verbose: VerboseOption = False,
    days: DaysOption = 30,
    group_by: Annotated[
        Optional[str],
        typer.Option("--group-by", help="Group by: sku, cluster, job, tag."),
    ] = None,
) -> None:
    """Generate a cost report by tag/team/project."""
    from auralake.cli.main import build_context

    ctx = build_context(config, provider, workspace, output, verbose)
    from auralake.analyzers.cost_analyzer import CostAnalyzer

    result = CostAnalyzer(ctx).analyze()
    summary = result.summary
    render_table(
        "Cost Report",
        ["Metric", "Value"],
        [[k, v] for k, v in summary.items()],
        OutputFormat(ctx.output_format),
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
            OutputFormat(ctx.output_format),
        )


@cost_app.command("breakdown")
def cost_breakdown(
    provider: ProviderOption = None,
    config: ConfigOption = None,
    workspace: WorkspaceOption = None,
    output: OutputOption = OutputFormat.TABLE,
    verbose: VerboseOption = False,
    days: DaysOption = 30,
    by: Annotated[
        str,
        typer.Option("--by", help="Breakdown dimension: sku, workspace, team, tag."),
    ] = "sku",
) -> None:
    """Show cost breakdown by dimension (SKU, workspace, team, tag)."""
    from auralake.cli.main import build_context

    ctx = build_context(config, provider, workspace, output, verbose)
    cost_client = ctx.provider.get_cost_client()
    end = date.today()
    start = end - timedelta(days=days)
    breakdown = cost_client.get_cost_breakdown(start, end)

    dimension_map = {
        "sku": ("SKU", breakdown.by_sku),
        "cluster": ("Cluster", breakdown.by_cluster),
        "job": ("Job", breakdown.by_job),
        "tag": ("Tag", breakdown.by_tag),
    }
    label, data = dimension_map.get(by, ("SKU", breakdown.by_sku))

    rows = sorted(data.items(), key=lambda x: x[1], reverse=True)
    render_table(
        f"Cost Breakdown by {label} ({start} to {end})",
        [label, "Cost (USD)"],
        [[name, f"${cost:.2f}"] for name, cost in rows],
        OutputFormat(ctx.output_format),
    )
    render_table(
        "Summary",
        ["Metric", "Value"],
        [
            ["Total Cost", f"${breakdown.total_cost_usd:.2f}"],
            ["Period", f"{start} to {end}"],
            ["Items", str(len(rows))],
        ],
        OutputFormat(ctx.output_format),
    )


@cost_app.command("trend")
def cost_trend(
    provider: ProviderOption = None,
    config: ConfigOption = None,
    workspace: WorkspaceOption = None,
    output: OutputOption = OutputFormat.TABLE,
    verbose: VerboseOption = False,
    days: DaysOption = 90,
    granularity: Annotated[
        str,
        typer.Option("--granularity", "-g", help="Time granularity: daily, weekly, monthly."),
    ] = "daily",
) -> None:
    """Display cost trends over time."""
    from auralake.cli.main import build_context

    ctx = build_context(config, provider, workspace, output, verbose)
    # TODO: Sprint 3 — implement cost trend with time-series aggregation
    print_warning("Cost trend not yet implemented.")


@cost_app.command("forecast")
def cost_forecast(
    provider: ProviderOption = None,
    config: ConfigOption = None,
    workspace: WorkspaceOption = None,
    output: OutputOption = OutputFormat.TABLE,
    verbose: VerboseOption = False,
    days: DaysOption = 30,
    horizon: Annotated[
        int,
        typer.Option("--horizon", help="Forecast horizon in days."),
    ] = 30,
) -> None:
    """Forecast future costs based on historical trends."""
    from auralake.cli.main import build_context

    ctx = build_context(config, provider, workspace, output, verbose)
    # TODO: Sprint 3 — implement cost forecast with linear/exponential models
    print_warning("Cost forecast not yet implemented.")


@cost_app.command("tco")
def cost_tco(
    provider: ProviderOption = None,
    config: ConfigOption = None,
    workspace: WorkspaceOption = None,
    output: OutputOption = OutputFormat.TABLE,
    verbose: VerboseOption = False,
    days: DaysOption = 30,
) -> None:
    """Calculate total cost of ownership (DBU + infrastructure)."""
    from auralake.cli.main import build_context

    ctx = build_context(config, provider, workspace, output, verbose)
    from auralake.analyzers.tco_analyzer import TCOAnalyzer

    result = TCOAnalyzer(ctx).analyze()
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
        OutputFormat(ctx.output_format),
    )


@cost_app.command("infra")
def cost_infra(
    provider: ProviderOption = None,
    config: ConfigOption = None,
    workspace: WorkspaceOption = None,
    output: OutputOption = OutputFormat.TABLE,
    verbose: VerboseOption = False,
    days: DaysOption = 30,
) -> None:
    """Show infrastructure (cloud) costs alongside DBU costs."""
    from auralake.cli.main import build_context

    ctx = build_context(config, provider, workspace, output, verbose)
    from auralake.analyzers.infra_cost_analyzer import InfraCostAnalyzer

    result = InfraCostAnalyzer(ctx).analyze()
    summary = result.summary
    render_table(
        "Infrastructure Cost Report",
        ["Metric", "Value"],
        [[k, v] for k, v in summary.items()],
        OutputFormat(ctx.output_format),
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
            OutputFormat(ctx.output_format),
        )
