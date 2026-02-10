"""Idle/unused resource management commands."""

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

resources_app = typer.Typer(no_args_is_help=True)


@resources_app.command("scan")
def resources_scan(
    server: ServerOption = None,
    workspace: WorkspaceOption = None,
    output: OutputOption = OutputFormat.TABLE,
) -> None:
    """Scan for idle or unused resources."""
    from auralake_cli.main import build_client

    client = build_client(server)
    result = client.resources_scan(workspace=workspace)

    render_table(
        "Idle Resource Scan",
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


@resources_app.command("cleanup")
def resources_cleanup(
    server: ServerOption = None,
    workspace: WorkspaceOption = None,
    resource_type: Annotated[
        str | None,
        typer.Option(
            "--type", "-t", help="Resource type to clean up: clusters, endpoints, warehouses, all."
        ),
    ] = None,
) -> None:
    """Terminate or downscale idle resources."""
    from auralake_cli.main import build_client

    client = build_client(server)
    results = client.resources_cleanup(resource_type=resource_type, workspace=workspace)

    if not results:
        print_warning("No idle resources found to clean up.")
        return

    render_table(
        "Cleanup Results",
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


@resources_app.command("report")
def resources_report(
    server: ServerOption = None,
    workspace: WorkspaceOption = None,
    output: OutputOption = OutputFormat.TABLE,
) -> None:
    """Generate a report of idle resources and potential savings."""
    from auralake_cli.main import build_client

    client = build_client(server)
    result = client.resources_report(workspace=workspace)

    render_table(
        "Idle Resource Report",
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
