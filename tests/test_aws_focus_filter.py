"""The aws_focus connector scopes an account-wide FOCUS export to a service
allow-list, selects current-version files via the per-partition manifest, and
only reads billing periods that overlap the ingest window."""

from datetime import date
from typing import Any

from flashlight.ingest._redshift_service_names import REDSHIFT_SERVICE_NAMES
from flashlight.ingest.base import IngestWindow
from flashlight.ingest.config import AwsFocusConfig
from flashlight.ingest.connectors.aws_focus import (
    _extract_data_file_keys,
    _period_in_window,
    _scan_source,
    _scan_where_literal,
)


# ── billing-period ↔ window overlap ───────────────────────────────────────────
def test_period_in_window_overlap() -> None:
    win = IngestWindow(start=date(2026, 6, 10), end=date(2026, 6, 20))
    assert _period_in_window("2026-06", win)  # month contains the window
    assert not _period_in_window("2026-05", win)  # ends before window starts
    assert not _period_in_window("2026-07", win)  # starts after window ends


def test_period_in_window_partial_overlap_at_month_edge() -> None:
    # A window straddling a month boundary must pull both months' manifests.
    win = IngestWindow(start=date(2026, 1, 28), end=date(2026, 2, 3))
    assert _period_in_window("2026-01", win)
    assert _period_in_window("2026-02", win)
    assert not _period_in_window("2025-12", win)


# ── manifest → current data-file keys ─────────────────────────────────────────
_MKEY = "focus/export/metadata/BILLING_PERIOD=2026-06/Manifest.json"


def test_extract_data_files_from_keyed_entries() -> None:
    manifest = {
        "dataFiles": [
            {"key": "focus/export/data/BILLING_PERIOD=2026-06/export-00001.snappy.parquet"},
            {"key": "focus/export/data/BILLING_PERIOD=2026-06/export-00002.snappy.parquet"},
        ]
    }
    urls = _extract_data_file_keys(manifest, "my-bucket", _MKEY)
    assert urls == [
        "s3://my-bucket/focus/export/data/BILLING_PERIOD=2026-06/export-00001.snappy.parquet",
        "s3://my-bucket/focus/export/data/BILLING_PERIOD=2026-06/export-00002.snappy.parquet",
    ]


def test_extract_data_files_relative_path_resolves_against_data_dir() -> None:
    manifest = {"dataFiles": [{"relativePath": "export-00001.snappy.parquet"}]}
    urls = _extract_data_file_keys(manifest, "my-bucket", _MKEY)
    assert urls == [
        "s3://my-bucket/focus/export/data/BILLING_PERIOD=2026-06/export-00001.snappy.parquet"
    ]


def test_extract_data_files_tolerant_fallback_scans_for_parquet() -> None:
    # Unknown field names: still find the .parquet keys anywhere in the document.
    manifest = {"some_new_field": {"nested": ["a/b/export-00001.snappy.parquet", "notes.txt"]}}
    urls = _extract_data_file_keys(manifest, "my-bucket", _MKEY)
    assert urls == ["s3://my-bucket/a/b/export-00001.snappy.parquet"]


# ── S3 auth (DuckDB httpfs secret) ─────────────────────────────────────────────
def _connector(monkeypatch: Any, **config_kw: Any) -> Any:
    from unittest.mock import MagicMock

    from flashlight.ingest.config import AwsFocusConfig
    from flashlight.ingest.connectors.aws_focus import AwsFocusConnector

    # aws_client() building the boto3 S3 client is a side effect of __init__ we don't
    # need for these tests — stub it out so a real/missing AWS profile on the test
    # machine can't make construction itself fail.
    monkeypatch.setattr(
        "flashlight.ingest.connectors.aws_focus.aws_client", MagicMock(return_value=MagicMock())
    )
    config = AwsFocusConfig.model_validate({"s3_bucket": "b", "region": "us-west-2", **config_kw})
    return AwsFocusConnector(config)


def test_s3_secret_sql_uses_named_profile_over_static_keys(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from unittest.mock import MagicMock

    frozen = MagicMock(access_key="AKIA_PROFILE", secret_key="secret_profile", token=None)
    creds = MagicMock()
    creds.get_frozen_credentials.return_value = frozen
    session = MagicMock()
    session.get_credentials.return_value = creds
    monkeypatch.setattr(
        "flashlight.ingest.connectors.aws_focus.boto3.Session", MagicMock(return_value=session)
    )

    connector = _connector(monkeypatch, aws_profile="acme-corp/data-engineer")
    sql = connector._s3_secret_sql()

    assert "KEY_ID 'AKIA_PROFILE'" in sql
    assert "SECRET 'secret_profile'" in sql
    assert "REGION 'us-west-2'" in sql
    assert "SESSION_TOKEN" not in sql  # no token on this (non-SSO) credential set
    assert "URL_STYLE 'path'" in sql


def test_s3_secret_sql_profile_includes_session_token_when_temporary(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from unittest.mock import MagicMock

    # SSO/assumed-role credentials are temporary and carry a session token — DuckDB
    # needs it too, or the secret is rejected as invalid once presented.
    frozen = MagicMock(access_key="ASIA_SSO", secret_key="secret_sso", token="sso-token-abc")
    creds = MagicMock()
    creds.get_frozen_credentials.return_value = frozen
    session = MagicMock()
    session.get_credentials.return_value = creds
    monkeypatch.setattr(
        "flashlight.ingest.connectors.aws_focus.boto3.Session", MagicMock(return_value=session)
    )

    connector = _connector(monkeypatch, aws_profile="acme-corp/data-engineer")
    sql = connector._s3_secret_sql()

    assert "SESSION_TOKEN 'sso-token-abc'" in sql


def test_s3_secret_sql_falls_back_to_static_keys_without_profile(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "static-key")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "static-secret")

    connector = _connector(monkeypatch)
    sql = connector._s3_secret_sql()

    assert "KEY_ID 'static-key'" in sql
    assert "SECRET 'static-secret'" in sql
    assert "URL_STYLE 'path'" in sql


def test_s3_secret_sql_falls_back_to_credential_chain_without_any_creds(  # type: ignore[no-untyped-def]
    monkeypatch,
) -> None:
    monkeypatch.delenv("AWS_ACCESS_KEY_ID", raising=False)
    monkeypatch.delenv("AWS_SECRET_ACCESS_KEY", raising=False)

    connector = _connector(monkeypatch)
    sql = connector._s3_secret_sql()

    assert "PROVIDER credential_chain" in sql
    assert "URL_STYLE 'path'" in sql


# ── literal-inlined scan SQL (the vectorized ingest() path) ───────────────────
# CREATE TABLE AS can't carry $-params through nested CTEs, so the bulk path
# inlines the window/service predicate as literals instead.
def test_scan_source_builds_read_parquet_array() -> None:
    assert _scan_source(["s3://b/f1.parquet", "s3://b/f2.parquet"]) == (
        "read_parquet(['s3://b/f1.parquet', 's3://b/f2.parquet'], union_by_name=true)"
    )


def test_scan_where_literal_inlines_window_and_services() -> None:
    win = IngestWindow(start=date(2026, 6, 1), end=date(2026, 6, 30))
    where = _scan_where_literal({"Amazon Redshift"}, win)
    assert "\"ChargePeriodStart\" >= DATE '2026-06-01'" in where
    assert "\"ChargePeriodStart\" < (DATE '2026-06-30' + INTERVAL 1 DAY)" in where
    assert '"ServiceName" IN (\'Amazon Redshift\')' in where


def test_scan_where_literal_omits_service_filter_when_allow_list_empty() -> None:
    win = IngestWindow(start=date(2026, 6, 1), end=date(2026, 6, 30))
    where = _scan_where_literal(set(), win)
    assert "ServiceName" not in where


def test_aws_focus_config_defaults_to_redshift_only() -> None:
    config = AwsFocusConfig.model_validate({"s3_bucket": "b"})
    assert set(config.include_services) == REDSHIFT_SERVICE_NAMES
