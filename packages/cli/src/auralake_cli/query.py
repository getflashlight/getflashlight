"""Query analysis and optimization CLI."""

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
    DaysOption,
    OutputOption,
    ServerOption,
    WorkspaceOption,
)

query_app = typer.Typer(no_args_is_help=True)


@query_app.command("analyze")
def query_analyze(
    server: ServerOption = None,
    workspace: WorkspaceOption = None,
    output: OutputOption = OutputFormat.TABLE,
    days: DaysOption = 7,
) -> None:
    """Analyze query patterns for optimization opportunities."""
    from auralake_cli.main import build_client

    client = build_client(server)
    result = client.query_analyze(workspace=workspace)

    render_table(
        "Query Analysis",
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


@query_app.command("expensive")
def query_expensive(
    server: ServerOption = None,
    workspace: WorkspaceOption = None,
    output: OutputOption = OutputFormat.TABLE,
    days: DaysOption = 7,
    top_n: Annotated[int, typer.Option("--top", "-n", help="Number of top queries to show.")] = 10,
) -> None:
    """Show the most expensive queries by duration."""
    from auralake_cli.main import build_client

    client = build_client(server)
    queries = client.query_expensive(days=days, top_n=top_n, workspace=workspace)

    rows = []
    for q in queries:
        duration = (q.get("duration_ms", 0) or 0) / 1000
        text = (q.get("query_text", "") or "")[:80]
        rows.append([q.get("query_id", ""), q.get("user_name", ""), f"{duration:.1f}s", text])

    render_table(
        f"Top {top_n} Expensive Queries",
        ["Query ID", "User", "Duration", "Query (truncated)"],
        rows,
        output,
    )


@query_app.command("plans")
def query_plans(
    server: ServerOption = None,
    workspace: WorkspaceOption = None,
    output: OutputOption = OutputFormat.TABLE,
) -> None:
    """Show query plans with anti-pattern detection."""
    from auralake_cli.main import build_client

    client = build_client(server)
    plans = client.query_plans(workspace=workspace)

    if not plans:
        print_warning(
            "No query plans found. Start the agent with `auralake agent start` to collect plans."
        )
        return

    rows = []
    for p in plans:
        anti_count = len(p.get("anti_patterns", [])) if p.get("anti_patterns") else 0
        rows.append(
            [
                p.get("query_id", ""),
                str(p.get("duration_ms", 0)) + "ms",
                str(anti_count),
                str(p.get("captured_at", "")),
            ]
        )
    render_table(
        "Captured Query Plans",
        ["Query ID", "Duration", "Anti-patterns", "Captured At"],
        rows,
        output,
    )
