"""Cluster policy management commands."""

from __future__ import annotations

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

policies_app = typer.Typer(no_args_is_help=True)


@policies_app.command("audit")
def policies_audit(
    server: ServerOption = None,
    workspace: WorkspaceOption = None,
    output: OutputOption = OutputFormat.TABLE,
) -> None:
    """Audit existing cluster policies for cost governance gaps."""
    from auralake_cli.main import build_client

    client = build_client(server)
    result = client.policies_audit(workspace=workspace)

    render_table(
        "Policy Audit",
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


@policies_app.command("create")
def policies_create(
    server: ServerOption = None,
) -> None:
    """Create a new cluster policy from a template."""
    print_warning("Policy creation not yet implemented.")


@policies_app.command("recommend")
def policies_recommend(
    server: ServerOption = None,
    workspace: WorkspaceOption = None,
    output: OutputOption = OutputFormat.TABLE,
) -> None:
    """Recommend policy improvements based on cluster usage patterns."""
    from auralake_cli.main import build_client

    client = build_client(server)
    result = client.policies_recommend(workspace=workspace)

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
        print_warning("No policy recommendations. All clusters follow best practices.")


@policies_app.command("apply")
def policies_apply(
    server: ServerOption = None,
    workspace: WorkspaceOption = None,
) -> None:
    """Apply a cluster policy to target clusters."""
    from auralake_cli.main import build_client

    client = build_client(server)
    results = client.policies_apply(workspace=workspace)

    if not results:
        print_warning("No policy violations found to apply fixes.")
        return

    render_table(
        "Policy Apply Results",
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
