"""Job/workflow optimization CLI."""

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

jobs_app = typer.Typer(no_args_is_help=True)


@jobs_app.command("analyze")
def jobs_analyze(
    server: ServerOption = None,
    workspace: WorkspaceOption = None,
    output: OutputOption = OutputFormat.TABLE,
) -> None:
    """Analyze job runs for optimization opportunities."""
    from auralake_cli.main import build_client

    client = build_client(server)
    result = client.jobs_analyze(workspace=workspace)

    render_table(
        "Job Analysis",
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


@jobs_app.command("consolidate")
def jobs_consolidate(
    server: ServerOption = None,
    workspace: WorkspaceOption = None,
) -> None:
    """Identify and consolidate overlapping or redundant jobs."""
    from auralake_cli.main import build_client

    client = build_client(server)
    results = client.jobs_consolidate(workspace=workspace)

    if not results:
        print_warning("No consolidation opportunities found.")
        return

    render_table(
        "Consolidation Results",
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


@jobs_app.command("stale")
def jobs_stale(
    server: ServerOption = None,
    workspace: WorkspaceOption = None,
    output: OutputOption = OutputFormat.TABLE,
) -> None:
    """Identify stale or unused jobs that can be decommissioned."""
    from auralake_cli.main import build_client

    client = build_client(server)
    result = client.jobs_stale(workspace=workspace)

    stale_recs = [r for r in result.recommendations if r.type == "job_stale"]
    if not stale_recs:
        print_warning("No stale jobs found.")
        return

    render_table(
        "Stale Jobs",
        ["Job", "Recommendation", "Savings"],
        [
            [r.resource_name, r.title, f"${r.estimated_monthly_savings_usd:.2f}/mo"]
            for r in stale_recs
        ],
        output,
    )


@jobs_app.command("recommend")
def jobs_recommend(
    server: ServerOption = None,
    workspace: WorkspaceOption = None,
    output: OutputOption = OutputFormat.TABLE,
) -> None:
    """Generate optimization recommendations for jobs."""
    from auralake_cli.main import build_client

    client = build_client(server)
    result = client.jobs_recommend(workspace=workspace)

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
        print_warning("No recommendations. All jobs look good!")
