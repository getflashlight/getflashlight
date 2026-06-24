"""``auralake init`` — scaffold the lake home with config + bundled sample data.

Silent and idempotent: creates the directory skeleton, drops the bundled FOCUS
sample CSV into ``<home>/data/``, writes a ready-to-run ``connections.yml`` (the
sample enabled, real sources stubbed), and prints next steps. Re-running leaves
existing files alone unless ``--force``.
"""

from __future__ import annotations

import shutil
from importlib import resources

import typer

from auralake.core.logging import get_logger
from auralake.lake import paths

logger = get_logger(__name__)

_CONNECTIONS_TEMPLATE = """\
# Auralake connectors. The bundled FOCUS sample is enabled so `auralake ingest`
# shows real numbers immediately. Enable your own sources by filling these in.
#
# Credentials are read from environment variables named by the *_env fields,
# never stored here.
connectors:
  - type: focus_file
    enabled: true
    path: {sample}
    respect_window: false

  # - type: aws_focus
  #   enabled: true
  #   s3_bucket: my-focus-export-bucket
  #   s3_prefix: focus_data
  #   region: us-east-1
  #   include_services: []        # empty = whole account

  # - type: databricks
  #   enabled: true
  #   host: https://my-workspace.cloud.databricks.com
  #   token_env: DATABRICKS_TOKEN
  #   sql_warehouse_id: abc123
"""


def scaffold(force: bool = False) -> None:
    """Create ``<home>/{config,bronze,gold,meta,data}`` and a starter config."""
    paths.ensure_layout()
    data_dir = paths.home() / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    sample_dst = data_dir / "focus_sample.csv"
    if force or not sample_dst.exists():
        source = resources.files("auralake._samples") / "focus_sample.csv"
        with resources.as_file(source) as src:
            shutil.copyfile(src, sample_dst)
        logger.info("sample_copied", path=str(sample_dst))

    conn = paths.connections_path()
    if force or not conn.exists():
        conn.write_text(_CONNECTIONS_TEMPLATE.format(sample=sample_dst))
        logger.info("connections_written", path=str(conn))

    typer.echo(f"\nAuralake initialized at {paths.home()}")
    typer.echo("\nNext steps:")
    typer.echo("  auralake ingest            # load billing (bundled sample is enabled)")
    typer.echo("  auralake dashboard serve   # dashboard → http://127.0.0.1:8501")
    typer.echo("  auralake mcp serve         # MCP for agents → :8002")
    typer.echo(f"\nEdit connectors in {conn}")
