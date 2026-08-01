from unittest.mock import MagicMock

import pytest

from flashlight.core.exceptions import ConfigError
from flashlight.ingest.config import (
    AwsFocusConfig,
    AwsInfraConfig,
    DatabricksConfig,
    RedshiftConfig,
    aws_client,
    env,
    load_all_connections,
    load_connections,
    save_connections,
)

_YAML = """
connectors:
  - type: aws_focus
    enabled: true
    s3_bucket: my-bucket
    s3_prefix: focus/data
  - type: databricks
    enabled: true
    host: https://example.cloud.databricks.com
  - type: aws_infra
    enabled: false
    region: us-east-1
"""


def test_load_connections_filters_disabled(tmp_path) -> None:  # type: ignore[no-untyped-def]
    path = tmp_path / "connections.yml"
    path.write_text(_YAML)
    configs = load_connections(str(path))
    # aws_infra is disabled → excluded.
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
        "  - type: redshift\n"
        "    enabled: true\n"
        "    cluster_identifier: prod-cluster\n"
        "    database: analytics\n"
    )
    configs = load_connections(str(path))
    assert len(configs) == 1
    assert isinstance(configs[0], RedshiftConfig)
    assert configs[0].cluster_identifier == "prod-cluster"


def test_env_treats_empty_as_unset(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # A present-but-empty var (AWS_ACCESS_KEY_ID= in a .env) must read as None so
    # connectors fall back to the default credential chain, not send an empty AKID.
    monkeypatch.setenv("FLASHLIGHT_TEST_CRED", "")
    assert env("FLASHLIGHT_TEST_CRED") is None
    monkeypatch.setenv("FLASHLIGHT_TEST_CRED", "real-key")
    assert env("FLASHLIGHT_TEST_CRED") == "real-key"
    monkeypatch.delenv("FLASHLIGHT_TEST_CRED", raising=False)
    assert env("FLASHLIGHT_TEST_CRED") is None


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
    # Same 3 entries as _YAML, including the disabled aws_infra one.
    assert len(configs) == 3
    assert isinstance(configs[2], AwsInfraConfig)
    assert configs[2].enabled is False


def test_save_connections_round_trips_through_load_all_connections(tmp_path) -> None:  # type: ignore[no-untyped-def]
    path = tmp_path / "connections.yml"
    entries = [
        AwsFocusConfig(s3_bucket="my-bucket", s3_prefix="focus/data"),
        DatabricksConfig(host="https://example.cloud.databricks.com", enabled=False),
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
