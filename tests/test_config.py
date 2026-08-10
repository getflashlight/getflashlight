from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from flashlight.core.exceptions import ConfigError
from flashlight.ingest.config import (
    AwsFocusConfig,
    DatabricksConfig,
    RedshiftConfig,
    aws_client,
    effective_connector_name,
    env,
    load_all_connections,
    load_connections,
    save_connections,
    scoped_env_name,
)

_YAML = """
connectors:
  - type: aws_focus
    enabled: true
    name: prod-cost
    s3_bucket: my-bucket
    s3_prefix: focus/data
  - type: databricks
    enabled: true
    host: https://example.cloud.databricks.com
  - type: redshift
    enabled: false
    cluster_identifier: prod-cluster
"""


def test_load_connections_filters_disabled(tmp_path) -> None:  # type: ignore[no-untyped-def]
    path = tmp_path / "connections.yml"
    path.write_text(_YAML)
    configs = load_connections(str(path))
    # the redshift entry is disabled → excluded.
    assert len(configs) == 2
    assert isinstance(configs[0], AwsFocusConfig)
    assert isinstance(configs[1], DatabricksConfig)
    assert configs[0].s3_bucket == "my-bucket"


def test_unknown_connector_type(tmp_path) -> None:  # type: ignore[no-untyped-def]
    path = tmp_path / "connections.yml"
    path.write_text("connectors:\n  - type: mystery\n")
    with pytest.raises(ConfigError):
        load_connections(str(path))


def test_missing_file() -> None:
    with pytest.raises(ConfigError):
        load_connections("/nonexistent/connections.yml")


def test_load_connections_registers_redshift(tmp_path) -> None:  # type: ignore[no-untyped-def]
    path = tmp_path / "connections.yml"
    path.write_text(
        "connectors:\n"
        "  - type: aws_focus\n"
        "    enabled: true\n"
        "    s3_bucket: my-bucket\n"
        "  - type: redshift\n"
        "    enabled: true\n"
        "    cluster_identifier: prod-cluster\n"
        "    database: analytics\n"
    )
    configs = load_connections(str(path))
    assert len(configs) == 2
    assert isinstance(configs[1], RedshiftConfig)
    assert configs[1].cluster_identifier == "prod-cluster"


def test_legacy_redshift_workgroup_is_a_clear_migration_error(tmp_path) -> None:  # type: ignore[no-untyped-def]
    path = tmp_path / "connections.yml"
    path.write_text(
        "connectors:\n"
        "  - type: redshift\n"
        "    enabled: false\n"
        "    workgroup_name: legacy-serverless\n"
    )
    with pytest.raises(ConfigError, match="Redshift Serverless is no longer supported"):
        load_all_connections(str(path))


def test_enabled_redshift_runs_without_aws_focus_for_telemetry(tmp_path) -> None:  # type: ignore[no-untyped-def]
    path = tmp_path / "connections.yml"
    path.write_text(
        "connectors:\n"
        "  - type: redshift\n"
        "    enabled: true\n"
        "    cluster_identifier: prod-cluster\n"
    )
    configs = load_connections(str(path))
    assert len(configs) == 1
    assert isinstance(configs[0], RedshiftConfig)

    # A disabled AWS FOCUS source still permits Redshift telemetry ingestion.
    path.write_text(
        "connectors:\n"
        "  - type: aws_focus\n"
        "    enabled: false\n"
        "    s3_bucket: my-bucket\n"
        "  - type: redshift\n"
        "    enabled: true\n"
        "    cluster_identifier: prod-cluster\n"
    )
    configs = load_connections(str(path))
    assert len(configs) == 1
    assert isinstance(configs[0], RedshiftConfig)

    # A disabled redshift entry is fine on its own — nothing to actually run.
    path.write_text(
        "connectors:\n"
        "  - type: redshift\n"
        "    enabled: false\n"
        "    cluster_identifier: prod-cluster\n"
    )
    assert load_all_connections(str(path))


def test_save_connections_allows_redshift_without_aws_focus(tmp_path) -> None:  # type: ignore[no-untyped-def]
    path = tmp_path / "connections.yml"
    entries = [RedshiftConfig(enabled=True, cluster_identifier="prod-cluster")]
    save_connections(entries, str(path))
    assert load_connections(str(path)) == entries


def test_env_treats_empty_as_unset(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # A present-but-empty var (AWS_ACCESS_KEY_ID= in a .env) must read as None so
    # connectors fall back to the default credential chain, not send an empty AKID.
    monkeypatch.setenv("FLASHLIGHT_TEST_CRED", "")
    assert env("FLASHLIGHT_TEST_CRED") is None
    monkeypatch.setenv("FLASHLIGHT_TEST_CRED", "real-key")
    assert env("FLASHLIGHT_TEST_CRED") == "real-key"
    monkeypatch.delenv("FLASHLIGHT_TEST_CRED", raising=False)
    assert env("FLASHLIGHT_TEST_CRED") is None


def test_env_falls_back_to_keychain_when_unset(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """One ingest approach: a bare ``flashlight ingest`` run in a terminal must
    resolve the exact same secret a dashboard-triggered sync would, with no
    separate "populate the subprocess env from the keychain" step required
    (see ``connection_credentials.py`` and ``dashboard/ingest_runner.py``).
    """
    from flashlight.ingest import connection_credentials

    monkeypatch.delenv("FLASHLIGHT_TEST_CRED", raising=False)
    monkeypatch.setattr(connection_credentials, "_keyring_get", lambda name: "from-keychain")
    assert env("FLASHLIGHT_TEST_CRED") == "from-keychain"

    # A real process env var still wins over the keychain.
    monkeypatch.setenv("FLASHLIGHT_TEST_CRED", "from-env")
    assert env("FLASHLIGHT_TEST_CRED") == "from-env"


def test_aws_client_uses_named_profile_when_set(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    fake_session = MagicMock()
    fake_session_fn = MagicMock(return_value=fake_session)
    monkeypatch.setattr("flashlight.ingest.config.boto3.Session", fake_session_fn)
    aws_client("redshift-data", region="us-east-1", profile="my-sso-profile")
    fake_session_fn.assert_called_once_with(profile_name="my-sso-profile")
    fake_session.client.assert_called_once_with("redshift-data", region_name="us-east-1")


def test_load_all_connections_includes_disabled(tmp_path) -> None:  # type: ignore[no-untyped-def]
    path = tmp_path / "connections.yml"
    path.write_text(_YAML)
    configs = load_all_connections(str(path))
    # Same 3 entries as _YAML, including the disabled redshift one.
    assert len(configs) == 3
    assert isinstance(configs[2], RedshiftConfig)
    assert configs[2].enabled is False


def test_save_connections_round_trips_through_load_all_connections(tmp_path) -> None:  # type: ignore[no-untyped-def]
    path = tmp_path / "connections.yml"
    entries = [
        AwsFocusConfig(name="prod-cost", s3_bucket="my-bucket", s3_prefix="focus/data"),
        DatabricksConfig(
            name="prod-workspace",
            host="https://example.cloud.databricks.com",
            enabled=False,
        ),
    ]
    save_connections(entries, str(path))

    loaded = load_all_connections(str(path))
    assert len(loaded) == 2
    assert loaded[0] == entries[0]
    assert loaded[1] == entries[1]
    # load_connections still filters the disabled one.
    assert len(load_connections(str(path))) == 1


def test_aws_client_falls_back_to_env_keys_without_profile(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("FLASHLIGHT_TEST_AKID", "AKIA_TEST")
    monkeypatch.setenv("FLASHLIGHT_TEST_SECRET", "shh")
    fake_client_fn = MagicMock()
    monkeypatch.setattr("flashlight.ingest.config.boto3.client", fake_client_fn)
    aws_client(
        "redshift-data",
        region="us-east-1",
        access_key_env="FLASHLIGHT_TEST_AKID",
        secret_key_env="FLASHLIGHT_TEST_SECRET",
    )
    fake_client_fn.assert_called_once_with(
        "redshift-data",
        region_name="us-east-1",
        aws_access_key_id="AKIA_TEST",
        aws_secret_access_key="shh",
    )


def test_cost_source_defaults_to_focus_export() -> None:
    cfg = AwsFocusConfig(s3_bucket="my-bucket")
    assert cfg.cost_source == "focus_export"


def test_cost_source_cost_explorer_does_not_require_s3_bucket() -> None:
    cfg = AwsFocusConfig(cost_source="cost_explorer")
    assert cfg.s3_bucket is None


def test_focus_export_requires_s3_bucket() -> None:
    with pytest.raises(ValidationError):
        AwsFocusConfig(cost_source="focus_export")


def test_effective_connector_name_falls_back_to_type() -> None:
    assert effective_connector_name(AwsFocusConfig(s3_bucket="b")) == "aws_focus"
    assert effective_connector_name(AwsFocusConfig(s3_bucket="b", name="prod")) == "prod"


def test_duplicate_connector_names_rejected(tmp_path) -> None:  # type: ignore[no-untyped-def]
    path = tmp_path / "connections.yml"
    path.write_text(
        "connectors:\n"
        "  - type: redshift\n"
        "    cluster_identifier: prod\n"
        "  - type: redshift\n"
        "    cluster_identifier: dev\n"
    )
    # Neither entry names itself, so both fall back to the same effective name
    # ("redshift") — exactly the collision the uniqueness check exists to catch.
    with pytest.raises(ConfigError):
        load_all_connections(str(path))


def test_named_redshift_connections_do_not_collide(tmp_path) -> None:  # type: ignore[no-untyped-def]
    path = tmp_path / "connections.yml"
    path.write_text(
        "connectors:\n"
        "  - type: redshift\n"
        "    name: prod\n"
        "    cluster_identifier: prod\n"
        "  - type: redshift\n"
        "    name: dev\n"
        "    cluster_identifier: dev\n"
    )
    configs = load_all_connections(str(path))
    assert len(configs) == 2


def test_connector_instances_pick_up_effective_name_for_bronze_partitioning() -> None:
    """Regression test: BRONZE partition-replace keys on Connector.name (see
    lake/bronze.py). Two connections of the same type must resolve to distinct
    instance-level names, or the second's ingest() would purge the first's
    partition — this is exactly what effective_connector_name/Connector.name
    exist to prevent.
    """
    from flashlight.ingest.connectors.aws_focus import AwsFocusConnector
    from flashlight.ingest.connectors.redshift import RedshiftConnector

    prod = RedshiftConnector(RedshiftConfig(name="prod", cluster_identifier="prod-cluster"))
    dev = RedshiftConnector(RedshiftConfig(name="dev", cluster_identifier="dev-cluster"))
    assert prod.name == "prod"
    assert dev.name == "dev"
    assert prod.name != dev.name

    unnamed = RedshiftConnector(RedshiftConfig(cluster_identifier="prod-cluster"))
    assert unnamed.name == "redshift"  # falls back to type, matching effective_connector_name

    aws = AwsFocusConnector(AwsFocusConfig(s3_bucket="b"))
    assert aws.name == "aws_focus"


def test_scoped_env_name_slugifies_the_suffix() -> None:
    assert scoped_env_name("AWS_ACCESS_KEY_ID", name="Prod (main)", ctype="redshift") == (
        "AWS_ACCESS_KEY_ID__PROD_MAIN"
    )
    assert scoped_env_name("AWS_ACCESS_KEY_ID", name=None, ctype="redshift") == (
        "AWS_ACCESS_KEY_ID__REDSHIFT"
    )


def test_redshift_connections_default_to_independent_secret_env_names() -> None:
    """Two Redshift connections must not silently share one keychain entry —
    regression test for the bug where all connections defaulted to the exact
    same access_key_env/secret_key_env (e.g. "AWS_ACCESS_KEY_ID"), so saving a
    new key under one connection's dialog overwrote every other's.
    """
    prod = RedshiftConfig(name="Prod (main)", cluster_identifier="prod-cluster")
    dev = RedshiftConfig(name="Dev", cluster_identifier="dev-cluster")
    assert prod.access_key_env == "AWS_ACCESS_KEY_ID__PROD_MAIN"
    assert dev.access_key_env == "AWS_ACCESS_KEY_ID__DEV"
    assert prod.access_key_env != dev.access_key_env
    assert prod.secret_key_env != dev.secret_key_env


def test_explicit_secret_env_name_is_not_rescoped() -> None:
    """An explicitly-set access_key_env (e.g. hand-edited in connections.yml to
    deliberately share one AWS key across connections) is left alone."""
    cfg = RedshiftConfig(
        name="Prod (main)",
        cluster_identifier="prod-cluster",
        access_key_env="SHARED_AWS_ACCESS_KEY_ID",
    )
    assert cfg.access_key_env == "SHARED_AWS_ACCESS_KEY_ID"


def test_databricks_and_aws_focus_also_scope_their_default_secret_env_names() -> None:
    db = DatabricksConfig(name="Prod workspace", host="https://example.databricks.com")
    assert db.token_env == "DATABRICKS_TOKEN__PROD_WORKSPACE"

    aws = AwsFocusConfig(name="Prod cost", s3_bucket="b")
    assert aws.access_key_env == "AWS_ACCESS_KEY_ID__PROD_COST"
    assert aws.secret_key_env == "AWS_SECRET_ACCESS_KEY__PROD_COST"
