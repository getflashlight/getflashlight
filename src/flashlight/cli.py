"""Unified ``flashlight`` command — the one operator interface.

    flashlight init               scaffold the lake home (config skeleton + connections.yml)
    flashlight sample             generate Redshift, Databricks, FOCUS, and Snowflake demos
    flashlight sample --clean     remove generated demo data, then rebuild GOLD
    flashlight ingest             pull billing → BRONZE Parquet, then rebuild GOLD
    flashlight transform          rebuild GOLD Parquet from BRONZE
    flashlight cleanup            remove ALL lake data (BRONZE/GOLD Parquet + run log)
    flashlight mcp serve          MCP server for agents (reads GOLD read-only)
    flashlight dashboard serve    NiceGUI dashboard for humans (reads GOLD read-only)
    flashlight aws create-export  provision the AWS FOCUS Data Export to consume
    flashlight aws update-export  update that export in place (e.g. fix its S3 prefix)
    flashlight aws delete-export  remove that export (S3 data is left untouched)

There is no database and no Docker: persistent state is Parquet under
``FLASHLIGHT_HOME``. ``ingest`` is the sole writer; ``mcp serve`` and ``dashboard
serve`` are independent read-only processes.

Heavy imports (boto3, the MCP/NiceGUI stacks) are deferred into each command so
``flashlight --help`` stays fast and doesn't import the world.
"""

import threading
from collections.abc import Callable
from datetime import date

import typer
from dotenv import load_dotenv

from flashlight.core.logging import setup_logging

app = typer.Typer(
    help="Flashlight — FOCUS-based multi-cloud spend visualization",
    no_args_is_help=True,
)
aws_app = typer.Typer(help="AWS Data Exports setup", no_args_is_help=True)
mcp_app = typer.Typer(help="MCP server for agents", no_args_is_help=True)
dashboard_app = typer.Typer(help="NiceGUI dashboard for humans", no_args_is_help=True)
app.add_typer(aws_app, name="aws")
app.add_typer(mcp_app, name="mcp")
app.add_typer(dashboard_app, name="dashboard")


@app.callback()
def _main() -> None:
    # Load .env from the CWD into os.environ before anything reads settings, so both
    # FLASHLIGHT_* platform settings and connector *_env credentials (DATABRICKS_TOKEN,
    # AWS_*) resolve from it. override=False → real shell env always wins over the
    # file. The dashboard subprocess inherits this populated environment.
    load_dotenv()
    setup_logging()


@app.command()
def init(
    force: bool = typer.Option(False, "--force", help="Overwrite existing config/sample"),
) -> None:
    """Scaffold the lake home (FLASHLIGHT_HOME, else the platform user-data dir) with a
    starter connections.yml. Run once. (Use ``sample`` to seed demo data.)"""
    from flashlight.scaffold import scaffold

    scaffold(force=force)


@app.command()
def sample(
    clean: bool = typer.Option(
        False, "--clean", help="Remove generated demo data instead of generating it"
    ),
) -> None:
    """Generate the reconciled cross-cloud and Snowflake dashboard demos.

    The scenario is deterministic and schema-validated: cluster names, owners,
    emails, tags, cost records, and telemetry all refer to the same entities.
    """
    from flashlight.sample import (
        _SNOWFLAKE_SYNTHETIC_DIR,
        cleanup,
        cleanup_snowflake_dashboard_demo,
        generate_snowflake_dashboard_demo,
        load_sample,
    )

    if clean:
        cleanup()
        cleanup_snowflake_dashboard_demo()
        return
    load_sample()
    generate_snowflake_dashboard_demo()
    snowflake_files = len(list(_SNOWFLAKE_SYNTHETIC_DIR.glob("*.parquet")))
    typer.echo(
        f"Generated Snowflake ACCOUNT_USAGE synthetic datasets → {snowflake_files} files."
    )
    typer.echo("Next: flashlight dashboard serve   # http://127.0.0.1:8501")


@mcp_app.command("serve")
def mcp_serve() -> None:
    """Run the MCP server for agents (reads the GOLD Parquet read-only)."""
    from flashlight.mcp.server import serve_mcp

    serve_mcp()


@dashboard_app.command("serve")
def dashboard_serve(
    dev: bool = typer.Option(
        False,
        "--dev",
        help="Reload the dashboard when Python source files change (development only)",
    ),
) -> None:
    """Run the dashboard for humans (reads the GOLD Parquet read-only)."""
    from flashlight.dashboard.launch import serve_dashboard

    serve_dashboard(dev=dev)


def _progress_printer() -> Callable[[str, str, int], None]:
    """A one-line-per-event progress printer (stdout).

    Connectors run concurrently now (a bounded thread pool, see ``run_ingest``), so
    a single ``\\r``-rewritten line no longer works — several connectors would
    interleave garbage on it. Each event gets its own printed line instead; a lock
    keeps one connector's line from interleaving with another's mid-write. The
    per-chunk "rows" tick (event="rows") is intentionally not printed — a live
    running count needs its own line per connector to stay legible, not worth it
    for a CLI progress hint. ``efficiency_done``/``efficiency_failed`` (from
    ``_run_efficiency``, after every connector's cost pull has already finished)
    get their own line too — otherwise a connector whose cost pull is a no-op
    (Redshift) prints "done" while its real payload, the efficiency pull, is
    still running or has silently failed with nothing printed at all.
    """
    lock = threading.Lock()

    def _on_progress(event: str, name: str, rows: int) -> None:
        with lock:
            if event == "ingest_started":
                typer.echo(f"  Ingest started: {name} connector(s)")
            elif event == "start":
                typer.echo(f"  {name} ...")
            elif event == "done":
                typer.echo(f"  {name} ... cost pull complete: {rows:,} rows")
            elif event == "failed":
                typer.secho(f"  {name} ... failed", fg=typer.colors.RED)
            elif event == "efficiency_done":
                typer.echo(f"  {name} ... efficiency: {rows:,} records")
            elif event == "efficiency_failed":
                typer.secho(f"  {name} ... efficiency failed", fg=typer.colors.RED)
            elif event == "cost_phase_complete":
                typer.echo(f"  Cost phase complete: {name} connector(s) finished")
            elif event == "telemetry_phase_complete":
                typer.echo(f"  Telemetry phase complete: {name} connector(s) finished")
            elif event == "runner_complete":
                typer.echo(f"  Runner complete: {name} connector(s)")
            elif event == "transform_start":
                typer.echo(f"  Rebuilding SILVER/GOLD from {name} ...")
            elif event == "transform_done":
                typer.echo(f"  SILVER/GOLD published from {name}: {rows:,} views")

    return _on_progress


@app.command()
def ingest(
    start: str | None = typer.Option(None, help="ISO start date (default: 35d lookback)"),
    end: str | None = typer.Option(None, help="ISO end date (default: today)"),
    connections: str | None = typer.Option(None, help="Path to connections.yml"),
    connector: str | None = typer.Option(
        None,
        "--connector",
        help="Only run this connector (its configured `name`, or `type` if unnamed)",
    ),
    no_transform: bool = typer.Option(False, "--no-transform", help="Skip SILVER/GOLD refresh"),
    run_id: str | None = typer.Option(
        None,
        "--run-id",
        help=(
            "Identify this sync in the run log with a caller-supplied id instead of "
            "generating one (the dashboard passes its own so it can name a saved-log "
            "file before the subprocess starts — see dashboard/ingest_runner.py)."
        ),
    ),
    full_refresh: bool = typer.Option(
        False,
        "--full-refresh",
        help=(
            "Wipe each connector's ENTIRE bronze history before pulling --start/--end "
            "(not just that window's partitions) — use after a config change (e.g. a "
            "narrower include_services) so stale out-of-window data doesn't linger. "
            "Composes with --start/--end: they still control what gets pulled back in. "
            "Without --start/--end, that's just the default 35-day lookback, so "
            "anything older than that is gone unless the source can still provide it "
            "— pass a wide --start explicitly to rebuild full history. Does not affect "
            "efficiency/driver-health metrics (keyed by provider, shared across "
            "connectors, and already window-scoped)."
        ),
    ),
) -> None:
    """Pull billing from all enabled connectors into BRONZE, then refresh views.

    Every connector runs even if an earlier one fails — a broken source doesn't
    block the others. GOLD is rebuilt from whatever succeeded; the command exits
    non-zero afterward if anything failed.
    """
    from flashlight.core.exceptions import IngestError
    from flashlight.ingest.runner import run_ingest

    try:
        run_ingest(
            start=date.fromisoformat(start) if start else None,
            end=date.fromisoformat(end) if end else None,
            connections=connections,
            connector=connector,
            no_transform=no_transform,
            full_refresh=full_refresh,
            on_progress=_progress_printer(),
            run_id=run_id,
        )
    except IngestError as exc:
        typer.secho(
            f"{len(exc.failed)} connector(s) failed: {', '.join(exc.failed)} "
            f"(see the logs above; successful connectors were ingested and GOLD "
            f"was rebuilt from them).",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1) from exc


@app.command()
def transform() -> None:
    """Rebuild GOLD Parquet from the current BRONZE (no re-pull needed)."""
    from flashlight.transform.runner import build_gold

    build_gold()


@app.command()
def cleanup(
    connector: str | None = typer.Option(
        None,
        help="Only remove this connector's data (e.g. aws_focus, databricks, redshift) "
        "— every other connector's BRONZE/GOLD is left untouched.",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Show what would be removed without removing it"
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt"),
) -> None:
    """Remove ALL lake data — every BRONZE/GOLD Parquet and the run log.

    Wipes everything the writers produce under ``FLASHLIGHT_HOME``, leaving only
    ``config/``. Destructive and irreversible; re-seed with ``sample`` or ``ingest``.
    To remove only the seeded sample, use ``sample --clean`` instead. To remove only
    one connector's BRONZE data (e.g. before re-pointing it at a different account),
    pass ``--connector``.
    """
    from flashlight.lake import cleanup as lake_cleanup

    if connector is not None:
        _cleanup_connector(connector, dry_run=dry_run, yes=yes)
        return

    targets = lake_cleanup.cleanup_targets()
    if not targets:
        typer.echo("Nothing to clean — no lake data found.")
        return

    typer.echo("The following lake data will be removed:")
    for path in targets:
        typer.echo(f"  {path}")

    if dry_run:
        typer.echo("\nDry run — nothing removed.")
        return

    # Destructive and applies by default — require explicit confirmation (default No).
    if not yes and not typer.confirm("\nRemove all lake data?"):
        typer.echo("Aborted.")
        raise typer.Exit(code=1)

    removed = lake_cleanup.purge_all()
    typer.echo(f"Removed all lake data ({', '.join(removed)}).")


def _cleanup_connector(connector: str, *, dry_run: bool, yes: bool) -> None:
    from flashlight.lake import cleanup as lake_cleanup
    from flashlight.transform.runner import build_gold

    targets = lake_cleanup.connector_targets(connector)
    if not targets:
        typer.echo(f"Nothing to clean for connector {connector!r} — no BRONZE data found.")
        return

    typer.echo(f"The following {connector!r} data will be removed:")
    for path in targets:
        typer.echo(f"  {path}")

    if dry_run:
        typer.echo("\nDry run — nothing removed.")
        return

    if not yes and not typer.confirm(f"\nRemove all {connector!r} data?"):
        typer.echo("Aborted.")
        raise typer.Exit(code=1)

    removed = lake_cleanup.purge_connector(connector)
    published = build_gold()
    typer.echo(
        f"Removed {removed} path(s) for connector {connector!r} → rebuilt {published} "
        "GOLD views."
    )


@aws_app.command("create-export")
def aws_create_export(
    bucket: str | None = typer.Option(None, help="Destination S3 bucket"),
    prefix: str | None = typer.Option(None, help="Destination S3 prefix"),
    s3_region: str | None = typer.Option(
        None, "--s3-region", help="Bucket region"
    ),
    name: str = typer.Option("flashlight-focus", help="Export name"),
    description: str = typer.Option("FOCUS 1.2 export consumed by Flashlight"),
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

    from flashlight.ingest.aws_export_setup import (
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
            from flashlight.ingest.aws_export_setup import bucket_policy_hint

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

    from flashlight.ingest.aws_export_setup import (
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

    from flashlight.ingest.aws_export_setup import describe_export, resolved_targets, save_state

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
    name: str = typer.Option("flashlight-focus", help="Name of the existing export to update"),
    description: str = typer.Option("FOCUS 1.2 export consumed by Flashlight"),
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

    from flashlight.ingest.aws_export_setup import (
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
    name: str = typer.Option("flashlight-focus", help="Name of the export to delete"),
    connections: str | None = typer.Option(None, help="connections.yml for AWS credentials"),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Show what would be deleted without deleting it"
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt"),
) -> None:
    """Delete the AWS FOCUS Data Export. Parquet already in S3 is left untouched."""
    from botocore.exceptions import BotoCoreError, ClientError

    from flashlight.ingest.aws_export_setup import perform_delete_export

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
