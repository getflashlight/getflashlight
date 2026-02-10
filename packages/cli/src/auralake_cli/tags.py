"""Tag governance commands."""

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

tags_app = typer.Typer(no_args_is_help=True)


@tags_app.command("scan")
def tags_scan(
    server: ServerOption = None,
    workspace: WorkspaceOption = None,
    output: OutputOption = OutputFormat.TABLE,
) -> None:
    """Scan resources for missing or non-compliant tags."""
    from auralake_cli.main import build_client

    client = build_client(server)
    result = client.tags_scan(workspace=workspace)

    render_table(
        "Tag Scan",
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


@tags_app.command("report")
def tags_report(
    server: ServerOption = None,
    workspace: WorkspaceOption = None,
    output: OutputOption = OutputFormat.TABLE,
) -> None:
    """Generate a tag compliance report."""
    from auralake_cli.main import build_client

    client = build_client(server)
    result = client.tags_report(workspace=workspace)

    render_table(
        "Tag Compliance Report",
        ["Metric", "Value"],
        [[k, v] for k, v in result.summary.items()],
        output,
    )
    if result.recommendations:
        render_table(
            "Non-Compliant Resources",
            ["Resource", "Missing Tags", "Current Tags"],
            [
                [
                    r.resource_name,
                    ", ".join(r.recommended_state.get("missing_tags", [])),
                    str(r.current_state.get("tags", {})),
                ]
                for r in result.recommendations
            ],
            output,
        )
    else:
        print_warning("All resources are tag-compliant.")


@tags_app.command("enforce")
def tags_enforce(
    server: ServerOption = None,
    workspace: WorkspaceOption = None,
) -> None:
    """Enforce tag policies by applying missing required tags."""
    from auralake_cli.main import build_client

    client = build_client(server)
    results = client.tags_enforce(workspace=workspace)

    if not results:
        print_warning("No tag violations found. All resources are compliant.")
        return

    render_table(
        "Tag Enforcement Results",
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
