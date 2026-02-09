"""Spot instance optimization commands."""
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

spot_app = typer.Typer(no_args_is_help=True)


@spot_app.command("analyze")
def spot_analyze(
    provider: ProviderOption = None,
    config: ConfigOption = None,
    workspace: WorkspaceOption = None,
    output: OutputOption = OutputFormat.TABLE,
    verbose: VerboseOption = False,
    days: DaysOption = 30,
    min_savings_pct: Annotated[
        int,
        typer.Option("--min-savings", help="Minimum savings percentage to include."),
    ] = 30,
) -> None:
    """Analyze workloads for spot instance eligibility."""
    from auralake.cli.main import build_context

    ctx = build_context(config, provider, workspace, output, verbose)

    from auralake.analyzers.spot_analyzer import SpotAnalyzer

    result = SpotAnalyzer(ctx).analyze()

    render_table(
        "Spot Analysis",
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


@spot_app.command("recommend")
def spot_recommend(
    provider: ProviderOption = None,
    config: ConfigOption = None,
    workspace: WorkspaceOption = None,
    output: OutputOption = OutputFormat.TABLE,
    verbose: VerboseOption = False,
    days: DaysOption = 30,
) -> None:
    """Generate spot instance recommendations for eligible workloads."""
    from auralake.cli.main import build_context

    ctx = build_context(config, provider, workspace, output, verbose)

    from auralake.analyzers.spot_analyzer import SpotAnalyzer

    result = SpotAnalyzer(ctx).analyze()

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
    else:
        print_warning("No spot recommendations. All eligible clusters already use spot instances.")


@spot_app.command("apply")
def spot_apply(
    provider: ProviderOption = None,
    config: ConfigOption = None,
    workspace: WorkspaceOption = None,
    verbose: VerboseOption = False,
    dry_run: DryRunOption = False,
    apply: ApplyOption = False,
    auto: AutoOption = False,
    pr: PROption = False,
    cluster_id: Annotated[
        Optional[str],
        typer.Option("--cluster-id", help="Target specific cluster for spot conversion."),
    ] = None,
) -> None:
    """Apply spot instance configuration to eligible clusters."""
    from auralake.cli.main import build_context

    ctx = build_context(config, provider, workspace, OutputFormat.TABLE, verbose, dry_run, apply, auto, pr)

    from auralake.analyzers.spot_analyzer import SpotAnalyzer

    result = SpotAnalyzer(ctx).analyze()

    recs = result.recommendations
    if cluster_id:
        recs = [r for r in recs if r.resource_id == cluster_id]

    if not recs:
        print_warning("No spot-eligible clusters found to apply.")
        return

    render_recommendations(
        [
            {
                "resource": r.resource_name,
                "recommendation": r.title,
                "estimated_savings": f"${r.estimated_monthly_savings_usd:.2f}/mo",
                "priority": r.risk_level,
            }
            for r in recs
        ],
        OutputFormat(ctx.output_format),
    )

    if ctx.dry_run:
        print_warning("[DRY RUN] No changes applied.")
        return

    from auralake.actions.spot_actions import EnableSpotAction

    action = EnableSpotAction(ctx)
    for rec in recs:
        action.execute(rec)
