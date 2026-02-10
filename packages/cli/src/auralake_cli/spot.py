"""Spot instance optimization commands."""

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

spot_app = typer.Typer(no_args_is_help=True)


@spot_app.command("analyze")
def spot_analyze(
    server: ServerOption = None,
    workspace: WorkspaceOption = None,
    output: OutputOption = OutputFormat.TABLE,
) -> None:
    """Analyze workloads for spot instance eligibility."""
    from auralake_cli.main import build_client

    client = build_client(server)
    result = client.spot_analyze(workspace=workspace)

    render_table(
        "Spot Analysis",
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


@spot_app.command("recommend")
def spot_recommend(
    server: ServerOption = None,
    workspace: WorkspaceOption = None,
    output: OutputOption = OutputFormat.TABLE,
) -> None:
    """Generate spot instance recommendations for eligible workloads."""
    from auralake_cli.main import build_client

    client = build_client(server)
    result = client.spot_recommend(workspace=workspace)

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
    else:
        print_warning("No spot recommendations. All eligible clusters already use spot instances.")


@spot_app.command("apply")
def spot_apply(
    server: ServerOption = None,
    workspace: WorkspaceOption = None,
    cluster_id: Annotated[
        str | None,
        typer.Option("--cluster-id", help="Target specific cluster for spot conversion."),
    ] = None,
) -> None:
    """Apply spot instance configuration to eligible clusters."""
    from auralake_cli.main import build_client

    client = build_client(server)
    results = client.spot_apply(cluster_id=cluster_id, workspace=workspace)

    if not results:
        print_warning("No spot-eligible clusters found to apply.")
        return

    render_table(
        "Spot Apply Results",
        ["Action", "Resource", "Status", "Detail", "Error"],
        [
            [
                r.action_type,
                r.resource_name,
                r.status,
                r.detail or "",
                r.error or "",
            ]
            for r in results
        ],
        OutputFormat.TABLE,
    )
