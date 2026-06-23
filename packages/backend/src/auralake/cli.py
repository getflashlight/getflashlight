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
    bucket: str | None = typer.Option(None, help="Destination S3 bucket (flag → config → prompt)"),
    prefix: str | None = typer.Option(None, help="Destination S3 prefix (flag → config → prompt)"),
    s3_region: str | None = typer.Option(
        None, "--s3-region", help="Bucket region (config → prompt)"
    ),
    name: str = typer.Option("auralake-focus", help="Export name"),
    description: str = typer.Option("FOCUS 1.2 export consumed by Auralake"),
    time_granularity: str = typer.Option("DAILY", help="HOURLY | DAILY | MONTHLY"),
    overwrite: str = typer.Option("OVERWRITE_REPORT", help="OVERWRITE_REPORT | CREATE_NEW_REPORT"),
    query_statement: str | None = typer.Option(None, help="Override the FOCUS column projection"),
    connections: str | None = typer.Option(None, help="connections.yml for bucket/prefix defaults"),
    apply: bool = typer.Option(False, help="Actually create the export (default: dry-run)"),
) -> None:
    """Create the AWS FOCUS 1.2 Data Export this platform consumes."""
    from botocore.exceptions import BotoCoreError, ClientError

    from auralake.ingest.aws_export_setup import (
        perform_create_export,
        resolved_targets,
        save_state,
    )

    # Resolve the values lacking a static default: flag → connections.yml →
    # remembered → prompt. (Plain `prompt=True` can't see those fallbacks.)
    bucket, prefix, s3_region, _ = resolved_targets(bucket, prefix, s3_region, connections)
    bucket = bucket or typer.prompt("Destination S3 bucket")
    if prefix is None:
        prefix = typer.prompt(
            "S3 export-root prefix (the folder with data/ and metadata/)", default=""
        )
    s3_region = s3_region or typer.prompt("Bucket region", default="us-east-1")
    save_state(bucket=bucket, prefix=prefix, region=s3_region)

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
    except (ClientError, BotoCoreError) as exc:
        # Expected operational failure — show a clean message, not a traceback.
        typer.secho(f"CreateExport failed: {exc}", fg=typer.colors.RED, err=True)
        if "bucket permission" in str(exc).lower() or "AccessDenied" in str(exc):
            from auralake.ingest.aws_export_setup import bucket_policy_hint

            typer.echo(bucket_policy_hint(bucket, connections), err=True)
        raise typer.Exit(code=1) from exc


@aws_app.command("bucket-policy")
def aws_bucket_policy(
    bucket: str | None = typer.Option(None, help="S3 bucket (flag → config → prompt)"),
    region: str | None = typer.Option(None, help="Bucket region (default: config)"),
    connections: str | None = typer.Option(None, help="connections.yml for defaults"),
    apply: bool = typer.Option(False, help="Merge-apply to the bucket (default: print only)"),
) -> None:
    """Print or --apply the S3 bucket policy AWS Data Exports needs to write the export."""
    from botocore.exceptions import BotoCoreError, ClientError

    from auralake.ingest.aws_export_setup import (
        apply_bucket_policy,
        print_bucket_policy,
        resolved_targets,
        save_state,
    )

    bucket, _, region, _ = resolved_targets(bucket, None, region, connections)
    bucket = bucket or typer.prompt("S3 bucket")
    save_state(bucket=bucket, region=region)
    if not apply:
        print_bucket_policy(bucket=bucket, connections=connections)
        return
    try:
        apply_bucket_policy(bucket=bucket, region=region, connections=connections)
    except (ClientError, BotoCoreError) as exc:
        typer.secho(f"Failed to apply bucket policy: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(f"Applied the Data Exports bucket policy to {bucket}.")


@aws_app.command("describe-export")
def aws_describe_export(
    bucket: str | None = typer.Option(None, help="S3 bucket (flag → config → remembered → prompt)"),
    prefix: str | None = typer.Option(None, help="Export-root prefix (flag → config → remembered)"),
    region: str | None = typer.Option(None, "--s3-region", help="Bucket region"),
    connections: str | None = typer.Option(None, help="connections.yml for defaults"),
) -> None:
    """Report how much FOCUS data has landed in S3 (billing periods, file count, size)."""
    from botocore.exceptions import BotoCoreError, ClientError

    from auralake.ingest.aws_export_setup import describe_export, resolved_targets, save_state

    bucket, prefix, region, _ = resolved_targets(bucket, prefix, region, connections)
    bucket = bucket or typer.prompt("S3 bucket")
    if prefix is None:
        prefix = typer.prompt("Export-root prefix", default="")
    save_state(bucket=bucket, prefix=prefix, region=region)
    try:
        describe_export(bucket=bucket, prefix=prefix, region=region, connections=connections)
    except (ClientError, BotoCoreError) as exc:
        typer.secho(f"describe-export failed: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc


if __name__ == "__main__":
    app()
