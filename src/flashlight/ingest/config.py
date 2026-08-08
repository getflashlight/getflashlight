"""Connector configuration loaded from a YAML connections file.

Credentials are read from environment variables referenced by ``*_env`` fields
rather than stored in the file. Single-tenant, self-hosted: one connections file
per deployment.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Literal

import boto3
import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

from flashlight.core.exceptions import ConfigError
from flashlight.ingest._ec2_service_names import EC2_SERVICE_NAMES
from flashlight.ingest._redshift_service_names import REDSHIFT_SERVICE_NAMES
from flashlight.ingest._s3_service_names import S3_SERVICE_NAMES
from flashlight.ingest.connection_credentials import load_secret
from flashlight.lake import paths

# The default AWS FOCUS pull: Redshift's own services (the cost the /aws page reports)
# plus S3 (the storage behind Unity Catalog, which Databricks' own DBU-only bill can't
# show — see docs/design/backing-storage.md) plus EC2 (the cloud VMs behind a classic
# Databricks cluster, same reasoning — see docs/design/backing-compute.md). One constant
# so the model default, the scaffolded connections.yml template and the tests can't
# disagree about it.
DEFAULT_INCLUDE_SERVICES: tuple[str, ...] = tuple(
    sorted(REDSHIFT_SERVICE_NAMES | S3_SERVICE_NAMES | EC2_SERVICE_NAMES)
)


def scoped_env_name(base: str, *, name: str | None, ctype: str) -> str:
    """A per-connection env var name derived from a connection's own identity —
    e.g. base ``"AWS_ACCESS_KEY_ID"`` + name ``"Prod (main)"`` ->
    ``"AWS_ACCESS_KEY_ID__PROD_MAIN"``.

    Two connections of the same type otherwise default to the exact same env
    var name (and so the exact same OS-keychain entry, see
    ``connection_credentials.py``) — scoping by the connection's own
    (enforced-unique) name/type keeps their secrets independent by default,
    without the user having to pick distinct names themselves.
    """
    suffix = re.sub(r"[^A-Za-z0-9]+", "_", name or ctype).strip("_").upper()
    return f"{base}__{suffix}"


class AwsFocusConfig(BaseModel):
    type: str = "aws_focus"
    enabled: bool = True
    # Human label. Optional for the common single-connection-of-this-type case
    # (falls back to ``type`` — see Connector.name in ingest/base.py); needed
    # once there's more than one AWS cost source (e.g. separate accounts), since
    # it's what BRONZE partitioning and the dashboard use to tell them apart —
    # enforced unique (by its effective value) across connections.yml.
    name: str | None = None
    # "focus_export": read the S3 FOCUS Data Export (s3_bucket/s3_prefix below).
    # "cost_explorer": query Cost Explorer instead — no S3 export needed, but
    # coarser (account-level SERVICE totals, no per-charge detail) and needs
    # ce:GetCostAndUsage. Pick one explicitly; there's no automatic fallback.
    cost_source: Literal["focus_export", "cost_explorer"] = "focus_export"
    s3_bucket: str | None = None
    s3_prefix: str = ""
    region: str = "us-east-1"
    # Named AWS profile (~/.aws/credentials or ~/.aws/config, incl. SSO profiles) —
    # takes priority over access_key_env/secret_key_env below when set, same as
    # RedshiftConfig.aws_profile.
    aws_profile: str | None = None
    access_key_env: str = "AWS_ACCESS_KEY_ID"
    secret_key_env: str = "AWS_SECRET_ACCESS_KEY"
    # Allow-list of FOCUS ServiceName values to ingest (also used as the Cost
    # Explorer SERVICE-dimension filter when cost_source="cost_explorer"). AWS
    # Data Exports is account-wide and cannot be scoped per service at the
    # source, so Flashlight narrows here. Defaults to Redshift + S3 + EC2 (see
    # DEFAULT_INCLUDE_SERVICES) — set explicitly (e.g. `[]` for the whole
    # account) to widen, or to just Redshift's names to opt out of S3/EC2.
    include_services: list[str] = Field(
        default_factory=lambda: list(DEFAULT_INCLUDE_SERVICES)
    )

    @field_validator("s3_prefix")
    @classmethod
    def _strip_trailing_slash(cls, v: str) -> str:
        # AWS Data Exports inserts its own ``/`` separator before the export name,
        # so a trailing slash here yields a ``focus_data//export-name`` double
        # slash in the delivered keys. Normalize any accidental trailing slash(es).
        return v.rstrip("/")

    @model_validator(mode="after")
    def _s3_bucket_required_for_focus_export(self) -> AwsFocusConfig:
        if self.cost_source == "focus_export" and not self.s3_bucket:
            raise ValueError("s3_bucket is required when cost_source='focus_export'")
        return self

    @model_validator(mode="after")
    def _scope_default_secret_env_names(self) -> AwsFocusConfig:
        # Only when left at the class default (i.e. absent from the input) — an
        # explicit value is a deliberate choice (e.g. to genuinely share one
        # AWS key across connections) and is left alone.
        if "access_key_env" not in self.model_fields_set:
            self.access_key_env = scoped_env_name(
                self.access_key_env, name=self.name, ctype=self.type
            )
        if "secret_key_env" not in self.model_fields_set:
            self.secret_key_env = scoped_env_name(
                self.secret_key_env, name=self.name, ctype=self.type
            )
        return self


class DatabricksConfig(BaseModel):
    type: str = "databricks"
    enabled: bool = True
    # Optional — see AwsFocusConfig.name for the fallback/uniqueness contract.
    name: str | None = None
    host: str
    token_env: str = "DATABRICKS_TOKEN"
    sql_warehouse_id: str | None = None
    # Which custom-tag key on system.billing.usage the efficiency/waste plane reads as
    # EfficiencyRecord.owner_project (databricks_efficiency.sql). Literal key match, not
    # a fold across spellings like the FOCUS Tags views — an org whose project-equivalent
    # tag is named e.g. "team" or "cost_center" instead of "project" would otherwise see
    # the Attribution tab's Projects panel read as ~100% Unattributed despite tagging
    # consistently. Case-sensitive: Databricks custom tag keys are, too.
    project_tag_key: str = "project"

    @model_validator(mode="after")
    def _scope_default_secret_env_name(self) -> DatabricksConfig:
        if "token_env" not in self.model_fields_set:
            self.token_env = scoped_env_name(self.token_env, name=self.name, ctype=self.type)
        return self


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
    # Optional — see AwsFocusConfig.name for the fallback/uniqueness contract.
    # Especially useful here: several Redshift connections (one per cluster) are
    # the common case, unlike the other connector types.
    name: str | None = None
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
    # Optional SQL run once right after a SQL connection opens (bastion tunnel or
    # direct — no effect on the Data API path, which has no persistent session to
    # set this on), before any of the real efficiency queries. The intended use is
    # WLM routing — e.g. ``SET query_group TO 'my_wlm_queue';`` to give this
    # connector's queries priority on a busy production cluster — but this is
    # deployment-specific WLM tuning, not something the shared codebase should
    # hard-code a queue/group name for. Put the real value in your own
    # connections.yml (already gitignored, see config/connections.yml in
    # .gitignore) rather than committing it.
    session_init_sql: str | None = None
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

    @model_validator(mode="after")
    def _scope_default_secret_env_names(self) -> RedshiftConfig:
        # db_password_env/bastion_private_key_passphrase_env have no fixed default
        # to scope here (they default to None — only ever set to a real value by
        # the dashboard form, which scopes them itself at that point).
        if "access_key_env" not in self.model_fields_set:
            self.access_key_env = scoped_env_name(
                self.access_key_env, name=self.name, ctype=self.type
            )
        if "secret_key_env" not in self.model_fields_set:
            self.secret_key_env = scoped_env_name(
                self.secret_key_env, name=self.name, ctype=self.type
            )
        return self


ConnectorConfig = AwsFocusConfig | DatabricksConfig | RedshiftConfig

_CONFIG_TYPES: dict[str, type[BaseModel]] = {
    "aws_focus": AwsFocusConfig,
    "databricks": DatabricksConfig,
    "redshift": RedshiftConfig,
}


class ConnectionsFile(BaseModel):
    connectors: list[ConnectorConfig] = Field(default_factory=list)


def env(name: str) -> str | None:
    """Resolve a connector secret by its ``*_env`` name (helper for connectors).

    Checks the real process environment first (a real shell export, or ``.env``
    via ``load_dotenv()`` in ``cli.py`` — either way this is what a bare
    ``flashlight ingest`` run in a terminal has), then falls back to the
    OS-keychain entry the dashboard's Connections page saves to
    (:func:`flashlight.ingest.connection_credentials.load_secret`) — the same
    one lookup a connector uses regardless of whether it's built by the
    dashboard's sync subprocess or invoked directly, so there's exactly one
    place secret resolution can go wrong instead of two divergent ones.

    A present-but-empty value — ``AWS_ACCESS_KEY_ID=`` in a ``.env`` reads back as
    ``""`` — is treated as *unset* (falls through to the keychain, then
    ``None``), so connectors fall back to their default credential chain
    (instance role, ``~/.aws/credentials``, …) instead of sending an explicit
    empty credential that AWS rejects as a malformed authorization header.
    """
    return os.environ.get(name) or load_secret(name) or None


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


def effective_connector_name(cfg: BaseModel) -> str:
    """The identifier BRONZE partitioning, the runlog, and the dashboard use for
    ``cfg`` — its explicit ``name`` if set, else its ``type`` (the common
    single-connection-of-this-type case). ``Connector.__init__`` sets its
    instance ``self.name`` from this same function, so a config's uniqueness
    here is exactly what keeps two connections from colliding on one BRONZE
    partition (see ``lake/bronze.py``).
    """
    name: str | None = getattr(cfg, "name", None)
    ctype: str = getattr(cfg, "type")  # noqa: B009 - every ConnectorConfig has one
    return name or ctype


def _validate_connectors(configs: list[BaseModel]) -> None:
    """Cross-connector checks that a single config can't make on its own — run on
    both load (:func:`_parse_entries`) and save (:func:`save_connections`), so a
    dashboard edit is rejected at write time, not just on the next ingest run.
    """
    seen: dict[str, int] = {}
    for cfg in configs:
        key = effective_connector_name(cfg)
        seen[key] = seen.get(key, 0) + 1
    duplicates = sorted(name for name, count in seen.items() if count > 1)
    if duplicates:
        raise ConfigError(f"Connection names must be unique; duplicated: {duplicates}")

    # Redshift never pulls its own cost — it flows through aws_focus (AWS Data
    # Exports FOCUS carries Redshift's SKUs). Without an enabled aws_focus
    # connector, an enabled redshift one would ingest efficiency telemetry with
    # no cost ever attributed to it. See RedshiftConfig's docstring.
    has_enabled_redshift = any(isinstance(c, RedshiftConfig) and c.enabled for c in configs)
    has_enabled_aws_focus = any(isinstance(c, AwsFocusConfig) and c.enabled for c in configs)
    if has_enabled_redshift and not has_enabled_aws_focus:
        raise ConfigError(
            "an enabled redshift connector requires an enabled aws_focus connector "
            "— Redshift cost flows through aws_focus, redshift only supplies "
            "efficiency telemetry"
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

    _validate_connectors(configs)
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
    back, so a save immediately followed by a load returns equivalent configs. Runs
    the same cross-connector checks :func:`load_all_connections` does (e.g. the
    redshift/aws_focus pairing) before writing, so a bad dashboard edit is rejected
    here rather than only on the next load.
    """
    _validate_connectors(entries)
    cfg_path = Path(path) if path else paths.connections_path()
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"connectors": [e.model_dump(exclude_none=True) for e in entries]}
    cfg_path.write_text(yaml.safe_dump(payload, sort_keys=False))
