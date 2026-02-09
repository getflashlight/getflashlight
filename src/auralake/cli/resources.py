"""Idle/unused resource management commands."""
from __future__ import annotations

from typing import Annotated, Optional

import typer

from auralake.cli._options import (
    ApplyOption,
    AutoOption,
    ConfigOption,
    DaysOption,
    DryRunOption,
    OutputOption,
    PROption,
    ProviderOption,
    VerboseOption,
    WorkspaceOption,
)
from auralake.core.output import OutputFormat, print_warning, render_recommendations, render_table

resources_app = typer.Typer(no_args_is_help=True)


@resources_app.command("scan")
def resources_scan(
    provider: ProviderOption = None,
    config: ConfigOption = None,
    workspace: WorkspaceOption = None,
    output: OutputOption = OutputFormat.TABLE,
    verbose: VerboseOption = False,
    days: DaysOption = 30,
    resource_type: Annotated[
        Optional[str],
        typer.Option("--type", "-t", help="Resource type to scan: clusters, endpoints, warehouses, all."),
    ] = None,
) -> None:
    """Scan for idle or unused resources."""
    from auralake.cli.main import build_context

    ctx = build_context(config, provider, workspace, output, verbose)

    from auralake.analyzers.idle_resource_analyzer import IdleResourceAnalyzer

    result = IdleResourceAnalyzer(ctx).analyze()

    render_table(
        "Idle Resource Scan",
        ["Metric", "Value"],
        [[k, v] for k, v in result.summary.items()],
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


@resources_app.command("cleanup")
def resources_cleanup(
    provider: ProviderOption = None,
    config: ConfigOption = None,
    workspace: WorkspaceOption = None,
    verbose: VerboseOption = False,
    dry_run: DryRunOption = False,
    apply: ApplyOption = False,
    auto: AutoOption = False,
    pr: PROption = False,
    resource_type: Annotated[
        Optional[str],
        typer.Option("--type", "-t", help="Resource type to clean up: clusters, endpoints, warehouses, all."),
    ] = None,
) -> None:
    """Terminate or downscale idle resources."""
    from auralake.cli.main import build_context

    ctx = build_context(config, provider, workspace, OutputFormat.TABLE, verbose, dry_run, apply, auto, pr)

    from auralake.analyzers.idle_resource_analyzer import IdleResourceAnalyzer

    result = IdleResourceAnalyzer(ctx).analyze()

    if not result.recommendations:
        print_warning("No idle resources found to clean up.")
        return

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

    if ctx.dry_run:
        print_warning("[DRY RUN] No changes applied.")
        return

    from auralake.actions.resource_actions import TerminateIdleClusterAction

    action = TerminateIdleClusterAction(ctx)
    for rec in result.recommendations:
        if rec.type == "idle_cluster":
            action.execute(rec)


@resources_app.command("report")
def resources_report(
    provider: ProviderOption = None,
    config: ConfigOption = None,
    workspace: WorkspaceOption = None,
    output: OutputOption = OutputFormat.TABLE,
    verbose: VerboseOption = False,
    days: DaysOption = 30,
) -> None:
    """Generate a report of idle resources and potential savings."""
    from auralake.cli.main import build_context

    ctx = build_context(config, provider, workspace, output, verbose)

    from auralake.analyzers.idle_resource_analyzer import IdleResourceAnalyzer

    result = IdleResourceAnalyzer(ctx).analyze()

    render_table(
        "Idle Resource Report",
        ["Metric", "Value"],
        [[k, v] for k, v in result.summary.items()],
        OutputFormat(ctx.output_format),
    )
    if result.recommendations:
        total_savings = sum(r.estimated_monthly_savings_usd for r in result.recommendations)
        render_table(
            "Idle Resources",
            ["Resource", "Type", "Idle Time", "Est. Savings"],
            [
                [
                    r.resource_name,
                    r.type,
                    f"{r.current_state.get('idle_minutes', 'N/A')} min",
                    f"${r.estimated_monthly_savings_usd:.2f}/mo",
                ]
                for r in result.recommendations
            ],
            OutputFormat(ctx.output_format),
        )
        render_table(
            "Summary",
            ["Metric", "Value"],
            [["Total Estimated Savings", f"${total_savings:.2f}/mo"]],
            OutputFormat(ctx.output_format),
        )
