"""Workload portability and routing analysis commands."""
from __future__ import annotations

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
from auralake.core.output import OutputFormat, print_warning, render_table

routing_app = typer.Typer(no_args_is_help=True)


@routing_app.command("analyze")
def routing_analyze(
    provider: ProviderOption = None,
    config: ConfigOption = None,
    workspace: WorkspaceOption = None,
    output: OutputOption = OutputFormat.TABLE,
    verbose: VerboseOption = False,
    days: DaysOption = 30,
) -> None:
    """Analyze workloads for cross-provider routing opportunities."""
    from auralake.cli.main import build_context

    ctx = build_context(config, provider, workspace, output, verbose)

    from auralake.analyzers.workload_analyzer import WorkloadAnalyzer

    result = WorkloadAnalyzer(ctx).analyze()

    render_table(
        "Workload Portability Analysis",
        ["Metric", "Value"],
        [[k, v] for k, v in result.summary.items()],
        OutputFormat(ctx.output_format),
    )


@routing_app.command("compare")
def routing_compare(
    provider: ProviderOption = None,
    config: ConfigOption = None,
    workspace: WorkspaceOption = None,
    output: OutputOption = OutputFormat.TABLE,
    verbose: VerboseOption = False,
    days: DaysOption = 30,
    target_provider: Annotated[
        Optional[str],
        typer.Option("--target", help="Target provider for comparison: databricks, snowflake, lake_formation."),
    ] = None,
) -> None:
    """Compare costs across providers for equivalent workloads."""
    from auralake.cli.main import build_context

    ctx = build_context(config, provider, workspace, output, verbose)
    # TODO: implement routing comparison with multi-provider cost modeling
    print_warning("Routing comparison not yet implemented.")
