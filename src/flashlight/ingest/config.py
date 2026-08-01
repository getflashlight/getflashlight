"""Connector configuration loaded from a YAML connections file.

Credentials are read from environment variables referenced by ``*_env`` fields
rather than stored in the file. Single-tenant, self-hosted: one connections file
per deployment.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import boto3
import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

from flashlight.core.exceptions import ConfigError
from flashlight.ingest._redshift_service_names import REDSHIFT_SERVICE_NAMES
from flashlight.lake import paths


class AwsFocusConfig(BaseModel):
    type: str = "aws_focus"
    enabled: bool = True
    s3_bucket: str
    s3_prefix: str = ""
    region: str = "us-east-1"
    # Named AWS profile (~/.aws/credentials or ~/.aws/config, incl. SSO profiles) —
    # takes priority over access_key_env/secret_key_env below when set, same as
    # RedshiftConfig.aws_profile.
    aws_profile: str | None = None
    access_key_env: str = "AWS_ACCESS_KEY_ID"
    secret_key_env: str = "AWS_SECRET_ACCESS_KEY"
    # Allow-list of FOCUS ServiceName values to ingest. AWS Data Exports is
    # account-wide and cannot be scoped per service at the source, so Flashlight
    # narrows here. Defaults to Redshift only — set explicitly (e.g. `[]` for the
    # whole account) to widen.
    include_services: list[str] = Field(
        default_factory=lambda: sorted(REDSHIFT_SERVICE_NAMES)
    )

    @field_validator("s3_prefix")
    @classmethod
    def _strip_trailing_slash(cls, v: str) -> str:
        # AWS Data Exports inserts its own ``/`` separator before the export name,
        # so a trailing slash here yields a ``focus_data//export-name`` double
        # slash in the delivered keys. Normalize any accidental trailing slash(es).
        return v.rstrip("/")


class FocusFileConfig(BaseModel):
    type: str = "focus_file"
    enabled: bool = True
    path: str  # local path to a FOCUS CSV or Parquet file
    # Sample/backfill files often predate the ingest window — ingest all rows by
    # default rather than filtering by the run's date range.
    respect_window: bool = False


class DatabricksConfig(BaseModel):
    type: str = "databricks"
    enabled: bool = True
    host: str
    token_env: str = "DATABRICKS_TOKEN"
    sql_warehouse_id: str | None = None


class AwsInfraConfig(BaseModel):
    type: str = "aws_infra"
    enabled: bool = False
    region: str = "us-east-1"
    access_key_env: str = "AWS_ACCESS_KEY_ID"
    secret_key_env: str = "AWS_SECRET_ACCESS_KEY"
    cluster_tag_key: str = "ClusterId"
    tag_filters: dict[str, str] = Field(default_factory=lambda: {"Vendor": "Databricks"})


class RedshiftConfig(BaseModel):
    """Redshift-native efficiency telemetry, via the Data API (no persistent DB creds)
    by default — or a direct SQL connection if ``db_password_env`` or ``bastion_host``
    is set.

    Cost is NOT pulled here — it already flows through ``aws_focus`` (AWS Data
    Exports FOCUS carries Redshift's own SKUs). This connector only supplies
    ``fetch_efficiency()``: WLM queue wait, concurrency-scaling usage, disk spill,
    table inventory, and reserved-node coverage.

    Two ways to reach the cluster over SQL, and — independently — two ways to
    authenticate that connection:

    - ``bastion_host`` set: SSH tunnel + direct SQL connection, for a cluster an
      SSH hop is needed to reach. Unset: a direct SQL connection straight to the
      cluster's endpoint (it's reachable directly, no tunnel needed). Neither set:
      the Data API — IAM (``db_user``) or Secrets Manager (``secret_arn``), no
      persistent connection, no static password anywhere.
    - ``db_password_env`` set: authenticate the SQL connection (tunneled or
      direct, whichever the above selected) with a local/native Redshift DB
      account password read from this env var. Unset: the default
      short-lived IAM-authenticated credential (``redshift:GetClusterCredentials``
      for ``db_user``) instead — only applies when a SQL connection is being made
      at all, i.e. has no effect on the Data API path.

    All ``bastion_*`` fields describe the SSH jump host itself (its address, its SSH
    login user, its key) — not the Redshift cluster. The Redshift cluster's own
    endpoint (for either the tunnel's remote target or a direct connection) is
    auto-discovered via ``describe_clusters`` unless ``db_host``/``db_port`` are set,
    which skip that AWS API call entirely.
    """

    type: str = "redshift"
    enabled: bool = False
    region: str = "us-east-1"
    # Exactly one of these identifies the compute to query — a provisioned cluster
    # or a Serverless workgroup.
    cluster_identifier: str | None = None
    workgroup_name: str | None = None
    database: str = "dev"
    # Data API auth: a temporary-credentials DB user (provisioned clusters, IAM-based)
    # or a Secrets Manager secret ARN — never a static password in config. Also the
    # DB user name used for a SQL connection's IAM credential fetch, if
    # db_password_env is unset.
    db_user: str | None = None
    secret_arn: str | None = None
    # Local/native Redshift DB account password, read from this env var — used to
    # authenticate a SQL connection (tunneled via bastion_host, or direct if unset)
    # instead of the default IAM-temp-credential flow. No effect on the Data API
    # path (neither bastion_host nor this set). Provisioned clusters only, same
    # scope limit as the bastion fields below.
    db_password_env: str | None = None
    # Explicit Redshift cluster endpoint override — skips the describe_clusters AWS
    # API call entirely (used by both the bastion tunnel's remote target and a direct
    # connection). Leave unset to auto-discover via describe_clusters as before.
    db_host: str | None = None
    db_port: int | None = None
    # Named AWS profile (~/.aws/credentials or ~/.aws/config, incl. SSO profiles) to
    # authenticate as — takes priority over access_key_env/secret_key_env below when
    # set. Most convenient for local/dev use against a real account; leave unset for
    # a deployed instance relying on an IAM role or static keys instead.
    aws_profile: str | None = None
    access_key_env: str = "AWS_ACCESS_KEY_ID"
    secret_key_env: str = "AWS_SECRET_ACCESS_KEY"
    # ── SSH bastion (jump host) — flattened, not nested, so these can never be
    # confused with the Redshift-side db_* fields above or with each other by name.
    # bastion_host is what makes a bastion "configured" (analogous to the old
    # `bastion is not None`).
    bastion_host: str | None = None
    bastion_port: int = 22
    bastion_user: str | None = None
    bastion_private_key_path: str | None = None
    bastion_private_key_passphrase_env: str | None = None

    @model_validator(mode="after")
    def _one_target(self) -> RedshiftConfig:
        if bool(self.cluster_identifier) == bool(self.workgroup_name):
            raise ValueError(
                "exactly one of cluster_identifier or workgroup_name must be set"
            )
        if self.bastion_host is not None and not self.cluster_identifier:
            raise ValueError(
                "bastion_host requires cluster_identifier (provisioned clusters only)"
            )
        if self.bastion_host is not None and not self.bastion_user:
            raise ValueError("bastion_host requires bastion_user")
        if self.bastion_host is not None and not self.bastion_private_key_path:
            raise ValueError("bastion_host requires bastion_private_key_path")
        if self.bastion_host is not None and not self.db_user:
            raise ValueError("bastion_host requires db_user (for the IAM credential fetch)")
        if self.db_password_env and not self.db_user:
            raise ValueError("db_password_env requires db_user")
        if self.db_password_env and not self.cluster_identifier:
            raise ValueError(
                "db_password_env requires cluster_identifier (provisioned clusters only)"
            )
        return self


ConnectorConfig = (
    AwsFocusConfig | FocusFileConfig | DatabricksConfig | AwsInfraConfig | RedshiftConfig
)

_CONFIG_TYPES: dict[str, type[BaseModel]] = {
    "aws_focus": AwsFocusConfig,
    "focus_file": FocusFileConfig,
    "databricks": DatabricksConfig,
    "aws_infra": AwsInfraConfig,
    "redshift": RedshiftConfig,
}


class ConnectionsFile(BaseModel):
    connectors: list[ConnectorConfig] = Field(default_factory=list)


def env(name: str) -> str | None:
    """Read an environment variable (helper for connectors).

    A present-but-empty value — ``AWS_ACCESS_KEY_ID=`` in a ``.env`` reads back as
    ``""`` — is treated as *unset* (returns ``None``), so connectors fall back to
    their default credential chain (instance role, ``~/.aws/credentials``, …)
    instead of sending an explicit empty credential that AWS rejects as a malformed
    authorization header.
    """
    return os.environ.get(name) or None


def aws_client(
    service: str,
    *,
    region: str,
    profile: str | None = None,
    access_key_env: str = "AWS_ACCESS_KEY_ID",
    secret_key_env: str = "AWS_SECRET_ACCESS_KEY",
) -> Any:
    """A boto3 client resolving credentials the same way across every AWS connector.

    A named ``profile`` (if set) takes priority — built via ``boto3.Session``, no
    explicit keys. Otherwise falls back to ``access_key_env``/``secret_key_env``;
    if those are also unset, ``env()`` returns ``None`` for both and boto3 falls
    through to its own default chain (env vars, ``~/.aws/credentials``, IAM role).
    """
    if profile:
        return boto3.Session(profile_name=profile).client(service, region_name=region)
    return boto3.client(
        service,
        region_name=region,
        aws_access_key_id=env(access_key_env),
        aws_secret_access_key=env(secret_key_env),
    )


def _parse_entries(raw: dict[str, Any]) -> list[BaseModel]:
    entries = raw.get("connectors", [])
    if not isinstance(entries, list):
        raise ConfigError("`connectors` must be a list")

    configs: list[BaseModel] = []
    for entry in entries:
        ctype = entry.get("type")
        model = _CONFIG_TYPES.get(ctype)
        if model is None:
            raise ConfigError(f"Unknown connector type: {ctype!r}")
        configs.append(model.model_validate(entry))
    return configs


def load_all_connections(path: str | None = None) -> list[BaseModel]:
    """Parse the connections YAML into typed connector configs, disabled included.

    Defaults to ``<home>/config/connections.yml`` (what ``flashlight init`` writes).
    Use :func:`load_connections` instead when you only want what ingest will run —
    this is for callers (e.g. the dashboard's Connections page) that need to show
    and edit disabled entries too.
    """
    cfg_path = Path(path) if path else paths.connections_path()
    if not cfg_path.exists():
        raise ConfigError(
            f"Connections file not found: {cfg_path}. Run `flashlight init` first."
        )
    raw = yaml.safe_load(cfg_path.read_text()) or {}
    return _parse_entries(raw)


def load_connections(path: str | None = None) -> list[BaseModel]:
    """Parse the connections YAML into typed, enabled connector configs."""
    return [cfg for cfg in load_all_connections(path) if getattr(cfg, "enabled", True)]


def save_connections(entries: list[BaseModel], path: str | None = None) -> None:
    """Write connector configs back to the connections YAML (full overwrite).

    Round-trips through the same Pydantic models :func:`load_all_connections` reads
    back, so a save immediately followed by a load returns equivalent configs.
    """
    cfg_path = Path(path) if path else paths.connections_path()
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"connectors": [e.model_dump(exclude_none=True) for e in entries]}
    cfg_path.write_text(yaml.safe_dump(payload, sort_keys=False))
