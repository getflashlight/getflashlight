"""Unified ``auralake`` command — the one operator interface.

    auralake init               scaffold ~/.auralake (config + bundled sample data)
    auralake ingest             pull billing → BRONZE Parquet, then rebuild GOLD
    auralake transform          rebuild GOLD Parquet from BRONZE
    auralake mcp serve          MCP server for agents (reads GOLD read-only)
    auralake dashboard serve    Streamlit dashboard for humans (reads GOLD read-only)
    auralake aws create-export  provision the AWS FOCUS Data Export to consume
    auralake aws update-export  update that export in place (e.g. fix its S3 prefix)
    auralake aws delete-export  remove that export (S3 data is left untouched)

There is no database and no Docker: persistent state is Parquet under
``AURALAKE_HOME``. ``ingest`` is the sole writer; ``mcp serve`` and ``dashboard
serve`` are independent read-only processes.

Heavy imports (boto3, the MCP/Streamlit stacks) are deferred into each command so
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
mcp_app = typer.Typer(help="MCP server for agents", no_args_is_help=True)
dashboard_app = typer.Typer(help="Streamlit dashboard for humans", no_args_is_help=True)
app.add_typer(aws_app, name="aws")
app.add_typer(mcp_app, name="mcp")
app.add_typer(dashboard_app, name="dashboard")
app.add_typer(dashboard_app, name="grafana", hidden=True)  # familiar alias


@app.callback()
def _main() -> None:
    setup_logging()


@app.command()
def init(
    force: bool = typer.Option(False, "--force", help="Overwrite existing config/sample"),
) -> None:
    """Scaffold ~/.auralake with config and bundled sample data. Run once."""
    from auralake.scaffold import scaffold

    scaffold(force=force)


@mcp_app.command("serve")
def mcp_serve() -> None:
    """Run the MCP server for agents (reads the GOLD Parquet read-only)."""
    from auralake.mcp.server import serve_mcp

    serve_mcp()


@dashboard_app.command("serve")
def dashboard_serve() -> None:
    """Run the Streamlit dashboard for humans (reads the GOLD Parquet read-only)."""
    from auralake.dashboard.launch import serve_dashboard

    serve_dashboard()


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
def transform() -> None:
    """Rebuild GOLD Parquet from the current BRONZE (no re-pull needed)."""
    from auralake.transform.runner import build_gold

    build_gold()


@aws_app.command("create-export")
def aws_create_export(
    bucket: str | None = typer.Option(None, help="Destination S3 bucket"),
    prefix: str | None = typer.Option(None, help="Destination S3 prefix"),
    s3_region: str | None = typer.Option(
        None, "--s3-region", help="Bucket region"
    ),
    name: str = typer.Option("auralake-focus", help="Export name"),
    description: str = typer.Option("FOCUS 1.2 export consumed by Auralake"),
    time_granularity: str = typer.Option("DAILY", help="HOURLY | DAILY | MONTHLY"),
    overwrite: str = typer.Option("OVERWRITE_REPORT", help="OVERWRITE_REPORT | CREATE_NEW_REPORT"),
    query_statement: str | None = typer.Option(None, help="Override the FOCUS column projection"),
    connections: str | None = typer.Option(None, help="connections.yml for bucket/prefix defaults"),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Print the request without creating the export"
    ),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Use resolved values without prompting; skip confirmation"
    ),
) -> None:
    """Create the AWS FOCUS 1.2 Data Export this platform consumes.

    Walks you through each input (bucket, prefix, region) with the resolved value as
    the default — press Enter to keep it, or type a new one — then confirms before
    provisioning. ``--yes`` takes the resolved values as-is.
    """
    from botocore.exceptions import BotoCoreError, ClientError

    from auralake.ingest.aws_export_setup import (
        perform_create_export,
        resolved_targets,
        save_state,
    )

    # Resolve the values lacking a static default: flag → connections.yml → remembered.
    bucket, prefix, s3_region, _ = resolved_targets(bucket, prefix, s3_region, connections)
    if not yes:
        # Interactive flow: prompt for each field with the resolved value as the
        # editable default (no default → the field is required).
        bucket = typer.prompt("Destination S3 bucket", default=bucket or None)
        prefix = typer.prompt(
            "S3 export-root prefix (the folder with data/ and metadata/)",
            default=prefix if prefix is not None else "",
        )
        s3_region = typer.prompt("Bucket region", default=s3_region or "us-east-1")
    save_state(bucket=bucket, prefix=prefix, region=s3_region)

    # Confirm after the summary prints (default Yes); --yes skips, --dry-run never asks.
    confirm = None if yes else (lambda: typer.confirm("\nCreate this export?", default=True))
    try:
        perform_create_export(
            apply=not dry_run,
            name=name,
            description=description,
            bucket=bucket,
            prefix=prefix,
            s3_region=s3_region,
            time_granularity=time_granularity,
            overwrite=overwrite,
            query_statement=query_statement,
            connections=connections,
            confirm=confirm,
        )
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    except (ClientError, BotoCoreError) as exc:
        # Expected operational failure — show a clean message, not a traceback.
        typer.secho(f"CreateExport failed: {exc}", fg=typer.colors.RED, err=True)
        if "bucket permission" in str(exc).lower() or "AccessDenied" in str(exc):
            from auralake.ingest.aws_export_setup import bucket_policy_hint

            typer.echo(bucket_policy_hint(bucket or "", connections), err=True)
        raise typer.Exit(code=1) from exc


@aws_app.command("bucket-policy")
def aws_bucket_policy(
    bucket: str | None = typer.Option(None, help="S3 bucket"),
    region: str | None = typer.Option(None, help="Bucket region (default: config)"),
    connections: str | None = typer.Option(None, help="connections.yml for defaults"),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Print the policy without applying it to the bucket"
    ),
) -> None:
    """Merge-apply the S3 bucket policy AWS Data Exports needs (--dry-run to just print it)."""
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
    if dry_run:
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
    bucket: str | None = typer.Option(None, help="S3 bucket"),
    prefix: str | None = typer.Option(None, help="Export-root prefix"),
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


@aws_app.command("update-export")
def aws_update_export(
    bucket: str | None = typer.Option(None, help="Destination S3 bucket"),
    prefix: str | None = typer.Option(None, help="Destination S3 prefix"),
    s3_region: str | None = typer.Option(
        None, "--s3-region", help="Bucket region"
    ),
    name: str = typer.Option("auralake-focus", help="Name of the existing export to update"),
    description: str = typer.Option("FOCUS 1.2 export consumed by Auralake"),
    time_granularity: str = typer.Option("DAILY", help="HOURLY | DAILY | MONTHLY"),
    overwrite: str = typer.Option("OVERWRITE_REPORT", help="OVERWRITE_REPORT | CREATE_NEW_REPORT"),
    query_statement: str | None = typer.Option(None, help="Override the FOCUS column projection"),
    connections: str | None = typer.Option(None, help="connections.yml for bucket/prefix defaults"),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Print the plan without updating the export"
    ),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Use resolved values without prompting; skip confirmation"
    ),
) -> None:
    """Update the AWS FOCUS Data Export in place (e.g. fix its S3 prefix after editing config).

    Walks you through each input (bucket, prefix, region) with the current value as
    the default — press Enter to keep it, or type a new one. Then prints a before→after
    plan of every field UpdateExport changes, flags the bucket-policy step if the bucket
    moves, and confirms before applying. ``--yes`` takes the resolved values as-is.
    """
    from botocore.exceptions import BotoCoreError, ClientError

    from auralake.ingest.aws_export_setup import (
        current_export_destination,
        perform_update_export,
        resolved_targets,
        save_state,
    )

    # Defaults come from the LIVE export (what AWS actually has), then fall back to
    # flags/config/state. We're editing a deployed resource, so its current values
    # are the truth — not a remembered local guess.
    try:
        live = current_export_destination(name, connections)
    except (ClientError, BotoCoreError):
        live = None  # can't reach AWS — fall back to config/state below
    rb, rp, rr, _ = resolved_targets(bucket, prefix, s3_region, connections)
    bucket = bucket or (live or {}).get("bucket") or rb
    prefix = prefix if prefix is not None else ((live or {}).get("prefix") or rp)
    s3_region = s3_region or (live or {}).get("region") or rr

    if not yes:
        # Interactive flow: prompt for each field with the current value as the
        # editable default (no default → the field is required).
        bucket = typer.prompt("Destination S3 bucket", default=bucket or None)
        prefix = typer.prompt(
            "S3 export-root prefix (the folder with data/ and metadata/)",
            default=prefix if prefix is not None else "",
        )
        s3_region = typer.prompt("Bucket region", default=s3_region or "us-east-1")
    save_state(bucket=bucket, prefix=prefix, region=s3_region)

    # Confirm after the plan prints (default Yes); --yes skips, --dry-run never asks.
    confirm = None if yes else (lambda: typer.confirm("\nApply this update?", default=True))
    try:
        perform_update_export(
            apply=not dry_run,
            name=name,
            description=description,
            bucket=bucket,
            prefix=prefix,
            s3_region=s3_region,
            time_granularity=time_granularity,
            overwrite=overwrite,
            query_statement=query_statement,
            connections=connections,
            confirm=confirm,
        )
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    except (ClientError, BotoCoreError) as exc:
        typer.secho(f"UpdateExport failed: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc


@aws_app.command("delete-export")
def aws_delete_export(
    name: str = typer.Option("auralake-focus", help="Name of the export to delete"),
    connections: str | None = typer.Option(None, help="connections.yml for AWS credentials"),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Show what would be deleted without deleting it"
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt"),
) -> None:
    """Delete the AWS FOCUS Data Export. Parquet already in S3 is left untouched."""
    from botocore.exceptions import BotoCoreError, ClientError

    from auralake.ingest.aws_export_setup import perform_delete_export

    # Destructive and applies by default — require explicit confirmation (default No)
    # unless --dry-run (which deletes nothing) or --yes (an intentional bypass).
    if not dry_run and not yes and not typer.confirm(f"Delete the export {name!r}?"):
        typer.echo("Aborted.")
        raise typer.Exit(code=1)

    try:
        perform_delete_export(apply=not dry_run, name=name, connections=connections)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    except (ClientError, BotoCoreError) as exc:
        typer.secho(f"DeleteExport failed: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc


if __name__ == "__main__":
    app()
