"""Cluster analysis and optimization CLI."""
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
from auralake.core.output import OutputFormat, print_warning, render_table, render_recommendations

clusters_app = typer.Typer(no_args_is_help=True)


@clusters_app.command("analyze")
def clusters_analyze(
    provider: ProviderOption = None,
    config: ConfigOption = None,
    workspace: WorkspaceOption = None,
    output: OutputOption = OutputFormat.TABLE,
    verbose: VerboseOption = False,
    days: DaysOption = 30,
    dry_run: DryRunOption = False,
    apply: ApplyOption = False,
    auto: AutoOption = False,
) -> None:
    """Analyze cluster utilization and generate right-sizing recommendations."""
    from auralake.cli.main import build_context

    ctx = build_context(config, provider, workspace, output, verbose, dry_run, apply, auto)

    from auralake.analyzers.cluster_analyzer import ClusterAnalyzer

    result = ClusterAnalyzer(ctx).analyze()

    summary = result.summary
    render_table(
        "Cluster Analysis",
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


@clusters_app.command("list")
def clusters_list(
    provider: ProviderOption = None,
    config: ConfigOption = None,
    workspace: WorkspaceOption = None,
    output: OutputOption = OutputFormat.TABLE,
    verbose: VerboseOption = False,
) -> None:
    """List all clusters with utilization summary."""
    from auralake.cli.main import build_context

    ctx = build_context(config, provider, workspace, output, verbose)
    compute = ctx.provider.get_compute_client()
    clusters = compute.list_clusters()
    rows = []
    for c in clusters:
        rows.append([
            c.cluster_id,
            c.cluster_name,
            c.state,
            c.num_workers,
            c.worker_node_type or "",
            "Yes" if c.spot_enabled else "No",
        ])
    render_table(
        "Clusters",
        ["ID", "Name", "State", "Workers", "Instance Type", "Spot"],
        rows,
        OutputFormat(ctx.output_format),
    )


@clusters_app.command("resize")
def clusters_resize(
    cluster_id: Annotated[str, typer.Argument(help="Cluster ID to resize.")],
    workers: Annotated[Optional[int], typer.Option("--workers", help="New number of workers.")] = None,
    instance_type: Annotated[Optional[str], typer.Option("--instance-type", help="New instance type.")] = None,
    provider: ProviderOption = None,
    config: ConfigOption = None,
    workspace: WorkspaceOption = None,
    output: OutputOption = OutputFormat.TABLE,
    verbose: VerboseOption = False,
    dry_run: DryRunOption = False,
    apply: ApplyOption = False,
    pr: PROption = False,
) -> None:
    """Resize a specific cluster."""
    from auralake.cli.main import build_context

    ctx = build_context(config, provider, workspace, output, verbose, dry_run, apply, create_pr=pr)

    if ctx.dry_run:
        print_warning(f"[DRY RUN] Would resize cluster {cluster_id}: workers={workers}, instance_type={instance_type}")
        return

    changes = {}
    if workers is not None:
        changes["num_workers"] = workers
    if instance_type is not None:
        changes["node_type_id"] = instance_type

    if not changes:
        print_warning("No changes specified. Use --workers or --instance-type.")
        return

    compute = ctx.provider.get_compute_client()
    compute.resize(cluster_id, changes)
    from auralake.core.output import print_success

    print_success(f"Cluster {cluster_id} resized: {changes}")


@clusters_app.command("show")
def clusters_show(
    cluster_id: Annotated[str, typer.Argument(help="Cluster ID to show.")],
    provider: ProviderOption = None,
    config: ConfigOption = None,
    workspace: WorkspaceOption = None,
    output: OutputOption = OutputFormat.TABLE,
    verbose: VerboseOption = False,
) -> None:
    """Show detailed information about a specific cluster."""
    from auralake.cli.main import build_context

    ctx = build_context(config, provider, workspace, output, verbose)
    compute = ctx.provider.get_compute_client()
    cluster = compute.get_cluster(cluster_id)
    rows = [
        ["Cluster ID", cluster.cluster_id],
        ["Name", cluster.cluster_name],
        ["State", cluster.state],
        ["Driver", cluster.driver_node_type or "N/A"],
        ["Worker Type", cluster.worker_node_type or "N/A"],
        ["Workers", str(cluster.num_workers)],
        ["Autoscale", "Yes" if cluster.autoscale else "No"],
        ["Spot", "Yes" if cluster.spot_enabled else "No"],
        ["Auto-terminate", f"{cluster.autotermination_minutes} min" if cluster.autotermination_minutes else "Disabled"],
        ["Creator", cluster.creator or "N/A"],
    ]
    render_table(
        f"Cluster: {cluster.cluster_name}",
        ["Property", "Value"],
        rows,
        OutputFormat(ctx.output_format),
    )
