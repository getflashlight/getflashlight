"""Unified ``auralake`` command — the one operator interface.

    auralake serve              run the MCP server (the consumer surface for agents)
    auralake ingest             pull billing → BRONZE, then refresh SILVER/GOLD
    auralake transform          rebuild SILVER views + GOLD matviews
    auralake aws create-export  provision the AWS FOCUS Data Export to consume

End users don't use this CLI — they read Grafana dashboards (which query Postgres
directly) and talk to the MCP server. This is the deployment/ops surface.

Heavy imports (boto3, psycopg, the MCP stack) are deferred into each command so
``auralake --help`` stays fast and doesn't import the world.
"""

from datetime import date

import typer

from auralake.core.logging import setup_logging

app = typer.Typer(
    help="Auralake — FOCUS-based multi-cloud TCO spend visualization",
    no_args_is_help=True,
)
aws_app = typer.Typer(help="AWS Data Exports setup", no_args_is_help=True)
app.add_typer(aws_app, name="aws")


@app.callback()
def _main() -> None:
    setup_logging()


@app.command()
def serve() -> None:
    """Run the Auralake MCP server (migrations auto-apply on startup)."""
    from auralake.mcp.server import serve_mcp

    serve_mcp()


@app.command()
def ingest(
    start: str | None = typer.Option(None, help="ISO start date (default: 35d lookback)"),
    end: str | None = typer.Option(None, help="ISO end date (default: today)"),
    connections: str | None = typer.Option(None, help="Path to connections.yml"),
    no_transform: bool = typer.Option(False, "--no-transform", help="Skip SILVER/GOLD refresh"),
) -> None:
    """Pull billing from all enabled connectors into BRONZE, then refresh views."""
    from auralake.ingest.runner import run_ingest

    run_ingest(
        start=date.fromisoformat(start) if start else None,
        end=date.fromisoformat(end) if end else None,
        connections=connections,
        no_transform=no_transform,
    )


@app.command()
def transform(
    rebuild: bool = typer.Option(False, help="Drop + recreate GOLD matviews to apply changes"),
) -> None:
    """(Re)build SILVER views and GOLD materialized views."""
    from auralake.transform.runner import apply_views

    apply_views(rebuild=rebuild)


@aws_app.command("create-export")
def aws_create_export(
    bucket: str | None = typer.Option(None, help="Destination S3 bucket (default: from config)"),
    prefix: str | None = typer.Option(None, help="Destination S3 prefix (default: from config)"),
    s3_region: str | None = typer.Option(None, "--s3-region", help="Bucket region"),
    name: str = typer.Option("auralake-focus", help="Export name"),
    description: str = typer.Option("FOCUS 1.2 export consumed by Auralake"),
    time_granularity: str = typer.Option("DAILY", help="HOURLY | DAILY | MONTHLY"),
    overwrite: str = typer.Option("OVERWRITE_REPORT", help="OVERWRITE_REPORT | CREATE_NEW_REPORT"),
    query_statement: str | None = typer.Option(None, help="Override the FOCUS column projection"),
    connections: str | None = typer.Option(None, help="connections.yml for bucket/prefix defaults"),
    apply: bool = typer.Option(False, help="Actually create the export (default: dry-run)"),
) -> None:
    """Create the AWS FOCUS 1.2 Data Export this platform consumes."""
    from auralake.ingest.aws_export_setup import perform_create_export

    try:
        perform_create_export(
            apply=apply,
            name=name,
            description=description,
            bucket=bucket,
            prefix=prefix,
            s3_region=s3_region,
            time_granularity=time_granularity,
            overwrite=overwrite,
            query_statement=query_statement,
            connections=connections,
        )
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc


if __name__ == "__main__":
    app()
