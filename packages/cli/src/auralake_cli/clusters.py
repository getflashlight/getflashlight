"""Cluster analysis and optimization CLI."""

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

clusters_app = typer.Typer(no_args_is_help=True)


@clusters_app.command("analyze")
def clusters_analyze(
    server: ServerOption = None,
    workspace: WorkspaceOption = None,
    output: OutputOption = OutputFormat.TABLE,
) -> None:
    """Analyze cluster utilization and generate right-sizing recommendations."""
    from auralake_cli.main import build_client

    client = build_client(server)
    result = client.clusters_analyze(workspace=workspace)

    summary = result.summary
    render_table(
        "Cluster Analysis",
        ["Metric", "Value"],
        [[k, v] for k, v in summary.items()],
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


@clusters_app.command("list")
def clusters_list(
    server: ServerOption = None,
    workspace: WorkspaceOption = None,
    output: OutputOption = OutputFormat.TABLE,
) -> None:
    """List all clusters with utilization summary."""
    from auralake_cli.main import build_client

    client = build_client(server)
    clusters = client.clusters_list(workspace=workspace)
    rows = []
    for c in clusters:
        rows.append(
            [
                c.get("cluster_id", ""),
                c.get("cluster_name", ""),
                c.get("state", ""),
                c.get("num_workers", ""),
                c.get("worker_node_type", ""),
                "Yes" if c.get("spot_enabled") else "No",
            ]
        )
    render_table(
        "Clusters",
        ["ID", "Name", "State", "Workers", "Instance Type", "Spot"],
        rows,
        output,
    )


@clusters_app.command("resize")
def clusters_resize(
    cluster_id: Annotated[str, typer.Argument(help="Cluster ID to resize.")],
    workers: Annotated[int | None, typer.Option("--workers", help="New number of workers.")] = None,
    instance_type: Annotated[
        str | None, typer.Option("--instance-type", help="New instance type.")
    ] = None,
    server: ServerOption = None,
    workspace: WorkspaceOption = None,
    output: OutputOption = OutputFormat.TABLE,
) -> None:
    """Resize a specific cluster."""
    from auralake_cli.main import build_client

    if workers is None and instance_type is None:
        print_warning("No changes specified. Use --workers or --instance-type.")
        return

    client = build_client(server)
    result = client.clusters_resize(
        cluster_id,
        workers=workers,
        instance_type=instance_type,
        workspace=workspace,
    )
    render_table(
        "Resize Result",
        ["Property", "Value"],
        [
            ["Action", result.action_type],
            ["Resource", result.resource_name],
            ["Status", result.status],
            ["Detail", result.detail or ""],
            ["Error", result.error or ""],
            ["PR URL", result.pr_url or ""],
        ],
        output,
    )


@clusters_app.command("show")
def clusters_show(
    cluster_id: Annotated[str, typer.Argument(help="Cluster ID to show.")],
    server: ServerOption = None,
    workspace: WorkspaceOption = None,
    output: OutputOption = OutputFormat.TABLE,
) -> None:
    """Show detailed information about a specific cluster."""
    from auralake_cli.main import build_client

    client = build_client(server)
    cluster = client.clusters_get(cluster_id, workspace=workspace)
    rows = [
        ["Cluster ID", cluster.get("cluster_id", "N/A")],
        ["Name", cluster.get("cluster_name", "N/A")],
        ["State", cluster.get("state", "N/A")],
        ["Driver", cluster.get("driver_node_type", "N/A")],
        ["Worker Type", cluster.get("worker_node_type", "N/A")],
        ["Workers", str(cluster.get("num_workers", "N/A"))],
        ["Autoscale", "Yes" if cluster.get("autoscale") else "No"],
        ["Spot", "Yes" if cluster.get("spot_enabled") else "No"],
        [
            "Auto-terminate",
            f"{cluster['autotermination_minutes']} min"
            if cluster.get("autotermination_minutes")
            else "Disabled",
        ],
        ["Creator", cluster.get("creator", "N/A")],
    ]
    render_table(
        f"Cluster: {cluster.get('cluster_name', cluster_id)}",
        ["Property", "Value"],
        rows,
        output,
    )
