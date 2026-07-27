"""``flashlight init`` — scaffold the lake home with a starter config.

Silent and idempotent: creates the directory skeleton and writes a documented
``connections.yml`` (all connectors commented as examples), then prints next
steps. Re-running leaves existing files alone unless ``--force``.

It does **not** bundle sample data — `flashlight sample` downloads the FinOps FOCUS
sample on demand, so the wheel stays lean and there's one seeding path.
"""

from __future__ import annotations

import typer

from flashlight.core.logging import get_logger
from flashlight.lake import paths

logger = get_logger(__name__)

_CONNECTIONS_TEMPLATE = """\
# Flashlight connectors. Uncomment and fill in the sources you want to ingest.
# Credentials are read from environment variables named by the *_env fields,
# never stored here.
#
# For a quick demo with real data and no config, skip this and run:
#     flashlight sample
connectors: []

  # - type: focus_file
  #   enabled: true
  #   path: /path/to/your/focus_export.csv   # or .parquet
  #   respect_window: false

  # - type: aws_focus
  #   enabled: true
  #   s3_bucket: my-focus-export-bucket
  #   s3_prefix: focus_data
  #   region: us-east-1
  #   # include_services defaults to Redshift only; set explicitly to widen, e.g.:
  #   # include_services: []   # [] = every service (the whole account)

  # - type: databricks
  #   enabled: true
  #   host: https://my-workspace.cloud.databricks.com
  #   token_env: DATABRICKS_TOKEN
  #   sql_warehouse_id: abc123
"""


def scaffold(force: bool = False) -> None:
    """Create ``<home>/{config,bronze,gold,meta}`` and a starter connections.yml."""
    paths.ensure_layout()

    conn = paths.connections_path()
    if force or not conn.exists():
        conn.write_text(_CONNECTIONS_TEMPLATE)
        logger.info("connections_written", path=str(conn))

    typer.echo(f"\nFlashlight initialized at {paths.home()}")
    typer.echo("\nNext steps:")
    typer.echo("  flashlight sample            # download the FOCUS sample + seed it (no config)")
    typer.echo(f"  # or edit {conn} to add your sources, then: flashlight ingest")
    typer.echo("  flashlight dashboard serve   # dashboard → http://127.0.0.1:8501")
