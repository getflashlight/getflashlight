"""Job/workflow optimization CLI."""
from __future__ import annotations

from typing import Annotated, Optional

import typer

from auralake.cli._options import (
    ApplyOption, AutoOption, ConfigOption, DaysOption, DryRunOption,
    OutputOption, PROption, ProviderOption, VerboseOption, WorkspaceOption,
)
from auralake.core.output import OutputFormat, print_warning, render_recommendations, render_table

jobs_app = typer.Typer(no_args_is_help=True)


@jobs_app.command("analyze")
def jobs_analyze(
    provider: ProviderOption = None,
    config: ConfigOption = None,
    workspace: WorkspaceOption = None,
    output: OutputOption = OutputFormat.TABLE,
    verbose: VerboseOption = False,
    days: DaysOption = 30,
) -> None:
    """Analyze job runs for optimization opportunities."""
    from auralake.cli.main import build_context
    ctx = build_context(config, provider, workspace, output, verbose)
    from auralake.analyzers.job_analyzer import JobAnalyzer
    result = JobAnalyzer(ctx).analyze()

    render_table(
        "Job Analysis",
        ["Metric", "Value"],
        [[k, v] for k, v in result.summary.items()],
        OutputFormat(ctx.output_format),
    )
    if result.recommendations:
        render_recommendations(
            [{"resource": r.resource_name, "recommendation": r.title,
              "estimated_savings": f"${r.estimated_monthly_savings_usd:.2f}/mo",
              "priority": r.risk_level} for r in result.recommendations],
            OutputFormat(ctx.output_format),
        )


@jobs_app.command("consolidate")
def jobs_consolidate(
    provider: ProviderOption = None,
    config: ConfigOption = None,
    workspace: WorkspaceOption = None,
    output: OutputOption = OutputFormat.TABLE,
    verbose: VerboseOption = False,
    dry_run: DryRunOption = False,
    apply: ApplyOption = False,
    pr: PROption = False,
) -> None:
    """Identify and consolidate overlapping or redundant jobs."""
    from auralake.cli.main import build_context
    ctx = build_context(config, provider, workspace, output, verbose, dry_run, apply, create_pr=pr)
    from auralake.analyzers.job_analyzer import JobAnalyzer
    result = JobAnalyzer(ctx).analyze()

    consolidation_recs = [r for r in result.recommendations if r.type == "job_consolidation"]
    if not consolidation_recs:
        print_warning("No consolidation opportunities found.")
        return

    render_recommendations(
        [{"resource": r.resource_name, "recommendation": r.title,
          "estimated_savings": f"${r.estimated_monthly_savings_usd:.2f}/mo",
          "priority": r.risk_level} for r in consolidation_recs],
        OutputFormat(ctx.output_format),
    )

    if ctx.create_pr:
        from auralake.actions.job_actions import ConsolidateJobsAction
        action = ConsolidateJobsAction(ctx)
        for rec in consolidation_recs:
            action.execute(rec)


@jobs_app.command("stale")
def jobs_stale(
    provider: ProviderOption = None,
    config: ConfigOption = None,
    workspace: WorkspaceOption = None,
    output: OutputOption = OutputFormat.TABLE,
    verbose: VerboseOption = False,
    days: DaysOption = 90,
) -> None:
    """Identify stale or unused jobs that can be decommissioned."""
    from auralake.cli.main import build_context
    ctx = build_context(config, provider, workspace, output, verbose)
    from auralake.analyzers.job_analyzer import JobAnalyzer
    result = JobAnalyzer(ctx).analyze()

    stale_recs = [r for r in result.recommendations if r.type == "job_stale"]
    if not stale_recs:
        print_warning("No stale jobs found.")
        return

    render_table(
        "Stale Jobs",
        ["Job", "Recommendation", "Savings"],
        [[r.resource_name, r.title, f"${r.estimated_monthly_savings_usd:.2f}/mo"] for r in stale_recs],
        OutputFormat(ctx.output_format),
    )


@jobs_app.command("recommend")
def jobs_recommend(
    provider: ProviderOption = None,
    config: ConfigOption = None,
    workspace: WorkspaceOption = None,
    output: OutputOption = OutputFormat.TABLE,
    verbose: VerboseOption = False,
) -> None:
    """Generate optimization recommendations for jobs."""
    from auralake.cli.main import build_context
    ctx = build_context(config, provider, workspace, output, verbose)
    from auralake.analyzers.job_analyzer import JobAnalyzer
    result = JobAnalyzer(ctx).analyze()

    if result.recommendations:
        render_recommendations(
            [{"resource": r.resource_name, "recommendation": r.title,
              "estimated_savings": f"${r.estimated_monthly_savings_usd:.2f}/mo",
              "priority": r.risk_level} for r in result.recommendations],
            OutputFormat(ctx.output_format),
        )
    else:
        print_warning("No recommendations. All jobs look good!")
