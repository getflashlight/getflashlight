import pytest

from auralake.core.exceptions import ConfigError
from auralake.ingest.config import (
    AwsFocusConfig,
    DatabricksConfig,
    env,
    load_connections,
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


def test_env_treats_empty_as_unset(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # A present-but-empty var (AWS_ACCESS_KEY_ID= in a .env) must read as None so
    # connectors fall back to the default credential chain, not send an empty AKID.
    monkeypatch.setenv("AURALAKE_TEST_CRED", "")
    assert env("AURALAKE_TEST_CRED") is None
    monkeypatch.setenv("AURALAKE_TEST_CRED", "real-key")
    assert env("AURALAKE_TEST_CRED") == "real-key"
    monkeypatch.delenv("AURALAKE_TEST_CRED", raising=False)
    assert env("AURALAKE_TEST_CRED") is None
