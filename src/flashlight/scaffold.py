"""``flashlight init`` — scaffold the lake home with a starter config.

Silent and idempotent: creates the directory skeleton and writes a documented
``connections.yml`` (all connectors commented as examples), then prints next
steps. Re-running leaves existing files alone unless ``--force``.

It does **not** bundle sample data — `flashlight sample` generates a deterministic
schema-driven demo on demand, so the wheel stays lean and there's one seeding path.
"""

from __future__ import annotations

import textwrap

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

  # - type: aws_focus
  #   enabled: true
  #   name: Prod
  #   s3_bucket: my-focus-export-bucket
  #   s3_prefix: focus_data
  #   region: us-east-1
  #   # cost_source defaults to focus_export (the S3 export above); set to
  #   # cost_explorer instead to query Cost Explorer directly (coarser, no
  #   # export needed, but needs ce:GetCostAndUsage) — pick one, no fallback.
  #   # cost_source: cost_explorer
  #   # include_services defaults to Redshift's services + Amazon S3 (S3 is what
  #   # backs Databricks storage, which its own DBU-only bill can't show). Set
  #   # explicitly to widen or narrow, e.g.:
  #   # include_services: []   # [] = every service (the whole account)

  # - type: databricks
  #   enabled: true
  #   name: Prod workspace
  #   host: https://my-workspace.cloud.databricks.com
  #   token_env: DATABRICKS_TOKEN
  #   sql_warehouse_id: abc123

  # - type: snowflake
  #   enabled: true
  #   name: Prod org
  #   account: xy12345.us-east-1
  #   user_env: SNOWFLAKE_USER
  #   password_env: SNOWFLAKE_PASSWORD
  #   role: ACCOUNTADMIN
  #   # warehouse: COMPUTE_WH          # optional query warehouse
  #   # authenticator: externalbrowser # optional SSO; leave unset for password
  #   # private_key_path: /path/key.pem  # optional key-pair; takes priority over password
"""


def _policies_template() -> str:
    """The starter policies.yml, documented from the model's own field descriptions
    so the file can't drift from the defaults it's showing."""
    from flashlight.efficiency.policy_config import PolicyThresholds

    lines = [
        "# Cost-policy thresholds. Every value below is already the default — this",
        "# file exists so you can tighten or relax them for your org. Delete it and",
        "# Flashlight falls back to these same defaults.",
        "#",
        "# Changing a value takes effect on the next `flashlight transform`/`ingest`:",
        "# thresholds are baked into the published GOLD, so the dashboard, the MCP",
        "# server, and any agent all classify identically.",
        "#",
        "# The rules themselves (what Flashlight checks) are the shipped catalog —",
        "# see the Policy page or the MCP `list_policy_rules` tool.",
        "",
        "thresholds:",
    ]
    defaults = PolicyThresholds()
    for name, field in PolicyThresholds.model_fields.items():
        if field.description:
            lines.append(f"  # {field.description}")
        lines.append(f"  {name}: {getattr(defaults, name)}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _assistant_template() -> str:
    """The starter assistant.yml, documented from the model's own field descriptions
    so the file can't drift from the shape the loader validates."""
    from flashlight.dashboard.assistant_config import AssistantConfig

    lines = [
        "# BYOK assistant — which model answers questions on the /assistant page.",
        "# Normally written for you by that page's gear dialog; edit it by hand to",
        "# configure a headless install without clicking through the UI.",
        "#",
        "# No API key here, ever. The key lives in your OS keychain, or in",
        "# FLASHLIGHT_ASSISTANT_API_KEY — so this file is safe to commit or mount.",
        "#",
        "# FLASHLIGHT_ASSISTANT_PROVIDER / _MODEL / _BASE_URL override these values.",
        "",
        "assistant:",
    ]
    for name, field in AssistantConfig.model_fields.items():
        if field.description:
            # Wrapped, unlike policies.yml's one-line descriptions: these run long
            # enough that an unwrapped comment would need horizontal scrolling.
            lines.extend(f"  # {ln}" for ln in textwrap.wrap(field.description, width=76))
        lines.append(f"  # {name}:")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def scaffold(force: bool = False) -> None:
    """Create ``<home>/{config,bronze,gold,meta}`` plus starter connections/policies/
    assistant YAML."""
    paths.ensure_layout()

    conn = paths.connections_path()
    if force or not conn.exists():
        conn.write_text(_CONNECTIONS_TEMPLATE)
        logger.info("connections_written", path=str(conn))

    policies = paths.policies_path()
    if force or not policies.exists():
        policies.write_text(_policies_template())
        logger.info("policies_written", path=str(policies))

    assistant = paths.assistant_config_path()
    if force or not assistant.exists():
        assistant.write_text(_assistant_template())
        logger.info("assistant_config_written", path=str(assistant))

    typer.echo(f"\nFlashlight initialized at {paths.home()}")
    typer.echo("\nNext steps:")
    typer.echo("  flashlight sample            # generate the linked demo data (no config)")
    typer.echo(f"  # or edit {conn} to add your sources, then: flashlight ingest")
    typer.echo(f"  # cost-policy thresholds (optional): {policies}")
    typer.echo(f"  # assistant model (optional, or use the dashboard's gear icon): {assistant}")
    typer.echo("  flashlight dashboard serve   # dashboard → http://127.0.0.1:8501")
