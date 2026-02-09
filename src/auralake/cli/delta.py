"""Delta Lake table maintenance commands."""
from __future__ import annotations

from typing import Annotated, Optional

import typer

from auralake.cli._options import (
    ApplyOption,
    AutoOption,
    ConfigOption,
    DryRunOption,
    OutputOption,
    PROption,
    ProviderOption,
    VerboseOption,
    WorkspaceOption,
)
from auralake.core.output import OutputFormat, print_warning, render_recommendations, render_table

delta_app = typer.Typer(no_args_is_help=True)


@delta_app.command("scan")
def delta_scan(
    provider: ProviderOption = None,
    config: ConfigOption = None,
    workspace: WorkspaceOption = None,
    output: OutputOption = OutputFormat.TABLE,
    verbose: VerboseOption = False,
    catalog: Annotated[
        Optional[str],
        typer.Option("--catalog", help="Unity Catalog name to scan."),
    ] = None,
    schema: Annotated[
        Optional[str],
        typer.Option("--schema", help="Schema name to scan."),
    ] = None,
) -> None:
    """Scan Delta tables for maintenance opportunities (small files, stale stats)."""
    from auralake.cli.main import build_context

    ctx = build_context(config, provider, workspace, output, verbose)

    from auralake.analyzers.delta_analyzer import DeltaAnalyzer

    result = DeltaAnalyzer(ctx).analyze()

    render_table(
        "Delta Table Scan",
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


@delta_app.command("optimize")
def delta_optimize(
    provider: ProviderOption = None,
    config: ConfigOption = None,
    workspace: WorkspaceOption = None,
    verbose: VerboseOption = False,
    dry_run: DryRunOption = False,
    apply: ApplyOption = False,
    auto: AutoOption = False,
    pr: PROption = False,
    table: Annotated[
        Optional[str],
        typer.Option("--table", "-t", help="Fully qualified table name to optimize."),
    ] = None,
) -> None:
    """Run OPTIMIZE on Delta tables that need compaction."""
    from auralake.cli.main import build_context

    ctx = build_context(config, provider, workspace, OutputFormat.TABLE, verbose, dry_run, apply, auto, pr)

    if not table:
        print_warning("Please specify a table with --table.")
        return

    if ctx.dry_run:
        print_warning(f"[DRY RUN] Would run OPTIMIZE on table '{table}'.")
        return

    from auralake.actions.delta_actions import OptimizeTableAction
    from auralake.models.recommendations import Recommendation, RiskLevel

    rec = Recommendation(
        type="delta_optimize",
        risk_level=RiskLevel.LOW,
        resource_id=table,
        resource_name=table,
        title=f"OPTIMIZE table '{table}'",
        description=f"Running OPTIMIZE on {table}.",
        recommended_state={"action": "OPTIMIZE"},
    )
    action = OptimizeTableAction(ctx)
    action.execute(rec)


@delta_app.command("vacuum")
def delta_vacuum(
    provider: ProviderOption = None,
    config: ConfigOption = None,
    workspace: WorkspaceOption = None,
    verbose: VerboseOption = False,
    dry_run: DryRunOption = False,
    apply: ApplyOption = False,
    auto: AutoOption = False,
    pr: PROption = False,
    table: Annotated[
        Optional[str],
        typer.Option("--table", "-t", help="Fully qualified table name to vacuum."),
    ] = None,
    retention_hours: Annotated[
        int,
        typer.Option("--retention-hours", help="Retention period in hours for VACUUM."),
    ] = 168,
) -> None:
    """Run VACUUM on Delta tables to reclaim storage."""
    from auralake.cli.main import build_context

    ctx = build_context(config, provider, workspace, OutputFormat.TABLE, verbose, dry_run, apply, auto, pr)

    if not table:
        print_warning("Please specify a table with --table.")
        return

    if ctx.dry_run:
        print_warning(f"[DRY RUN] Would run VACUUM on table '{table}' with retention {retention_hours}h.")
        return

    from auralake.actions.delta_actions import VacuumTableAction
    from auralake.models.recommendations import Recommendation, RiskLevel

    rec = Recommendation(
        type="delta_vacuum",
        risk_level=RiskLevel.MEDIUM,
        resource_id=table,
        resource_name=table,
        title=f"VACUUM table '{table}'",
        description=f"Running VACUUM on {table} with {retention_hours}h retention.",
        recommended_state={"action": "VACUUM", "retention_hours": retention_hours},
    )
    action = VacuumTableAction(ctx)
    action.execute(rec)


@delta_app.command("zorder")
def delta_zorder(
    provider: ProviderOption = None,
    config: ConfigOption = None,
    workspace: WorkspaceOption = None,
    verbose: VerboseOption = False,
    dry_run: DryRunOption = False,
    apply: ApplyOption = False,
    auto: AutoOption = False,
    pr: PROption = False,
    table: Annotated[
        Optional[str],
        typer.Option("--table", "-t", help="Fully qualified table name to Z-ORDER."),
    ] = None,
    columns: Annotated[
        Optional[str],
        typer.Option("--columns", help="Comma-separated columns for Z-ORDER."),
    ] = None,
) -> None:
    """Apply Z-ORDER optimization to Delta tables."""
    from auralake.cli.main import build_context

    ctx = build_context(config, provider, workspace, OutputFormat.TABLE, verbose, dry_run, apply, auto, pr)
    # TODO: implement delta zorder
    print_warning("Delta Z-ORDER not yet implemented.")
