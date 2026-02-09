"""Query analysis and optimization CLI."""
from __future__ import annotations

from typing import Annotated, Optional

import typer

from auralake.cli._options import (
    ConfigOption, DaysOption, OutputOption, ProviderOption, VerboseOption, WorkspaceOption,
)
from auralake.core.output import OutputFormat, print_warning, render_recommendations, render_table

query_app = typer.Typer(no_args_is_help=True)


@query_app.command("analyze")
def query_analyze(
    provider: ProviderOption = None,
    config: ConfigOption = None,
    workspace: WorkspaceOption = None,
    output: OutputOption = OutputFormat.TABLE,
    verbose: VerboseOption = False,
    days: DaysOption = 7,
) -> None:
    """Analyze query patterns for optimization opportunities."""
    from auralake.cli.main import build_context
    ctx = build_context(config, provider, workspace, output, verbose)
    from auralake.analyzers.query_analyzer import QueryAnalyzer
    result = QueryAnalyzer(ctx).analyze()

    render_table(
        "Query Analysis",
        ["Metric", "Value"],
        [[k, v] for k, v in result.summary.items()],
        OutputFormat(ctx.output_format),
    )
    if result.recommendations:
        render_recommendations(
            [{"resource": r.resource_name, "recommendation": r.title,
              "estimated_savings": f"${r.estimated_monthly_savings_usd:.2f}/mo",
              "priority": r.risk_level} for r in result.recommendations],
            OutputFormat(ctx.output_format),
        )


@query_app.command("expensive")
def query_expensive(
    provider: ProviderOption = None,
    config: ConfigOption = None,
    workspace: WorkspaceOption = None,
    output: OutputOption = OutputFormat.TABLE,
    verbose: VerboseOption = False,
    days: DaysOption = 7,
    top_n: Annotated[int, typer.Option("--top", "-n", help="Number of top queries to show.")] = 10,
) -> None:
    """Show the most expensive queries by duration."""
    from auralake.cli.main import build_context
    ctx = build_context(config, provider, workspace, output, verbose)
    query_client = ctx.provider.get_query_client()
    queries = query_client.get_query_history(hours=days * 24, limit=500)

    sorted_q = sorted(queries, key=lambda q: q.get("duration_ms", 0) or 0, reverse=True)[:top_n]

    rows = []
    for q in sorted_q:
        duration = (q.get("duration_ms", 0) or 0) / 1000
        text = (q.get("query_text", "") or "")[:80]
        rows.append([q.get("query_id", ""), q.get("user_name", ""), f"{duration:.1f}s", text])

    render_table(
        f"Top {top_n} Expensive Queries",
        ["Query ID", "User", "Duration", "Query (truncated)"],
        rows,
        OutputFormat(ctx.output_format),
    )


@query_app.command("plans")
def query_plans(
    provider: ProviderOption = None,
    config: ConfigOption = None,
    workspace: WorkspaceOption = None,
    output: OutputOption = OutputFormat.TABLE,
    verbose: VerboseOption = False,
) -> None:
    """Show query plans with anti-pattern detection."""
    from auralake.cli.main import build_context
    ctx = build_context(config, provider, workspace, output, verbose)

    # Try to read from database first (if agent has been collecting)
    try:
        from auralake.db.engine import init_engine, get_session
        from auralake.db.repositories import QueryPlanRepository

        init_engine(ctx.config.database.url)
        with get_session() as session:
            repo = QueryPlanRepository(session)
            plans = repo.list_by_workspace(ctx.workspace or "default", limit=20)
            if plans:
                rows = []
                for p in plans:
                    anti_count = len(p.anti_patterns) if p.anti_patterns else 0
                    rows.append([p.query_id, str(p.duration_ms or 0) + "ms", str(anti_count), str(p.captured_at)])
                render_table(
                    "Captured Query Plans",
                    ["Query ID", "Duration", "Anti-patterns", "Captured At"],
                    rows,
                    OutputFormat(ctx.output_format),
                )
                return
    except Exception:
        pass

    print_warning("No query plans found. Start the agent with `auralake agent start` to collect plans.")
