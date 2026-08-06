"""The aws_focus connector scopes an account-wide FOCUS export to a service
allow-list, selects current-version files via the per-partition manifest, and
only reads billing periods that overlap the ingest window."""

from datetime import date
from typing import Any

import duckdb
import pytest

from flashlight.focus import sql_mapping
from flashlight.ingest._redshift_service_names import REDSHIFT_SERVICE_NAMES
from flashlight.ingest._s3_service_names import S3_SERVICE_NAMES
from flashlight.ingest.base import IngestWindow
from flashlight.ingest.config import DEFAULT_INCLUDE_SERVICES, AwsFocusConfig
from flashlight.ingest.connectors.aws_focus import (
    _classify_redshift_cost_category,
    _classify_s3_cost_category,
    _cost_subcategory_sql,
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

    # Explicit access_key_env/secret_key_env, matching the env vars set above —
    # left at the class default, these would instead be scoped per-connection
    # (see config.py's scoped_env_name) and so wouldn't resolve to those names.
    connector = _connector(
        monkeypatch, access_key_env="AWS_ACCESS_KEY_ID", secret_key_env="AWS_SECRET_ACCESS_KEY"
    )
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


def test_aws_focus_config_defaults_to_redshift_and_s3() -> None:
    # S3 is in the default pull because the Databricks backing-storage view needs it:
    # Databricks' own bill is DBU-only, so the storage behind a Unity Catalog external
    # location is billed by AWS under S3's ServiceName and is invisible without it.
    config = AwsFocusConfig.model_validate({"s3_bucket": "b"})
    assert set(config.include_services) == REDSHIFT_SERVICE_NAMES | S3_SERVICE_NAMES


def test_aws_focus_config_include_services_can_be_narrowed_back_to_redshift() -> None:
    # Opting out of the S3 pull stays a one-line edit — the default is a default,
    # not a floor.
    config = AwsFocusConfig.model_validate(
        {"s3_bucket": "b", "include_services": sorted(REDSHIFT_SERVICE_NAMES)}
    )
    assert set(config.include_services) == REDSHIFT_SERVICE_NAMES


def test_scan_where_literal_pushes_down_every_default_service() -> None:
    # The default allow-list reaches the pushed-down predicate as-is: if S3 fell out
    # here, BRONZE would carry no S3 rows and the backing-storage view would be empty
    # while looking correctly configured.
    win = IngestWindow(start=date(2026, 6, 1), end=date(2026, 6, 30))
    where = _scan_where_literal(set(DEFAULT_INCLUDE_SERVICES), win)
    for service in DEFAULT_INCLUDE_SERVICES:
        assert f"'{service}'" in where


# ── S3 below-SKU classification ───────────────────────────────────────────────
# Databricks' storage is billed by AWS under S3, so "how much" isn't enough: a
# storage-growth problem and a request-volume problem have different remedies, and
# Databricks drives heavy LIST/GET metadata traffic.
@pytest.mark.parametrize(
    ("charge_description", "expected"),
    [
        ("TimedStorage-ByteHrs", "storage"),
        ("$0.023 per GB - first 50 TB / month of storage used", "storage"),
        ("TimedStorage-INT-FA-ByteHrs", "storage"),
        ("TimedStorage-SizeOverhead", "storage"),
        ("Requests-Tier1", "requests"),
        ("per 1,000 PUT, COPY, POST, or LIST requests", "requests"),
        ("Retrieval-SIA", "requests"),
        ("Select-Scanned-Bytes", "requests"),
        ("DataTransfer-Out-Bytes", "data_transfer"),
        ("Data Transfer Acceleration - Out", "data_transfer"),
        ("Monitoring-Automation-INT", "monitoring"),
        ("EarlyDelete-ByteHrs", "early_delete"),
        ("something totally unrecognized", "other"),
        (None, "other"),
    ],
)
def test_classify_s3_cost_category(charge_description: str | None, expected: str) -> None:
    assert _classify_s3_cost_category(charge_description, None) == expected


@pytest.mark.parametrize(
    ("charge_description", "expected", "swallowed_by"),
    [
        # Each of these matches a *later*, broader rule too — the ordering is what
        # keeps it out of that bucket, so these three are the real regression guards.
        ("EarlyDelete-ByteHrs", "early_delete", "storage (via 'bytehrs')"),
        ("Requests-Tier4 Lifecycle Transition request", "requests", "storage"),
        ("Monitoring and Automation, per 1,000 objects of storage", "monitoring", "storage"),
    ],
)
def test_classify_s3_cost_category_ordering_traps(
    charge_description: str, expected: str, swallowed_by: str
) -> None:
    assert _classify_s3_cost_category(charge_description, None) == expected, (
        f"{charge_description!r} was swallowed by {swallowed_by}"
    )


# ── the one x_cost_subcategory expression: SQL ↔ Python parity ─────────────────
# mapping_sql takes exactly one cost_subcategory_sql, so both service families share
# one CASE. This is the test that keeps the SQL the ingest actually runs in step with
# the Python twins the unit tests above pin — the drift lake/seed.py once had against
# _focus_map.py.
_PARITY_ROWS: tuple[tuple[str, str | None, str | None, str], ...] = (
    ("Amazon Simple Storage Service", "TimedStorage-ByteHrs", None, "Usage"),
    ("Amazon Simple Storage Service", "Requests-Tier1", None, "Usage"),
    ("Amazon Simple Storage Service", "Requests-Tier4 Lifecycle Transition request", None, "Usage"),
    ("Amazon Simple Storage Service", "DataTransfer-Out-Bytes", None, "Usage"),
    ("Amazon Simple Storage Service", "Monitoring-Automation-INT", None, "Usage"),
    ("Amazon Simple Storage Service", "EarlyDelete-ByteHrs", None, "Usage"),
    ("Amazon Simple Storage Service", "unrecognized", None, "Usage"),
    ("Amazon Simple Storage Service", None, "TimedStorage-ByteHrs", "Usage"),
    ("Amazon Redshift", "Redshift, ra3.4xlarge instance hourly fee", None, "Usage"),
    ("Amazon Redshift", "Concurrency Scaling usage", None, "Usage"),
    ("Amazon Redshift", "$5.00 per Terabyte for Redshift Data Scan", None, "Usage"),
    ("Amazon Redshift Spectrum", "Spectrum scan", None, "Usage"),
    ("Amazon Elastic Compute Cloud", "BoxUsage:m5.large", None, "Usage"),
    ("Amazon Simple Storage Service", "TimedStorage-ByteHrs", None, "Credit"),
    ("Amazon Redshift", "Redshift compute node", None, "Credit"),
)


def _expected_subcategory(service: str, desc: str | None, sku: str | None, cat: str) -> str | None:
    """What the Python twins say ``x_cost_subcategory`` should be, including the two
    exclusions the SQL applies: a credit is never classified, and Redshift's
    ``committed`` is NULLed out of the usage-mix view."""
    if cat == "Credit":
        return None
    if service in REDSHIFT_SERVICE_NAMES:
        category = _classify_redshift_cost_category(desc, sku)
        return None if category == "committed" else category
    if service in S3_SERVICE_NAMES:
        return _classify_s3_cost_category(desc, sku)
    return None


def test_cost_subcategory_sql_matches_the_python_classifiers() -> None:
    con = duckdb.connect()
    try:
        sql_mapping.ensure_helpers(con)
        # The same macro ingest() installs before splicing the expression in.
        con.execute(
            "CREATE OR REPLACE MACRO charge_text(d, s) AS "
            "lower(coalesce(d, '') || ' ' || coalesce(s, ''))"
        )
        con.execute(
            "CREATE TABLE src (ServiceName VARCHAR, ChargeDescription VARCHAR, "
            "SkuId VARCHAR, ChargeCategory VARCHAR)"
        )
        con.executemany("INSERT INTO src VALUES (?, ?, ?, ?)", list(_PARITY_ROWS))
        rows = con.execute(
            "SELECT ServiceName, ChargeDescription, SkuId, ChargeCategory, "
            f"({_cost_subcategory_sql()}) AS sub FROM src"
        ).fetchall()
    finally:
        con.close()

    assert len(rows) == len(_PARITY_ROWS)
    for service, desc, sku, cat, actual in rows:
        assert actual == _expected_subcategory(service, desc, sku, cat), (
            f"SQL and Python disagree on {service!r} / {desc!r} / {sku!r} / {cat!r}"
        )


def test_cost_subcategory_sql_nulls_credits_and_redshift_commitments() -> None:
    """The two exclusions stated directly, so they can't be lost in the parity sweep
    (which would still pass if both sides regressed together)."""
    con = duckdb.connect()
    try:
        sql_mapping.ensure_helpers(con)
        con.execute(
            "CREATE OR REPLACE MACRO charge_text(d, s) AS "
            "lower(coalesce(d, '') || ' ' || coalesce(s, ''))"
        )
        con.execute(
            "CREATE TABLE src (ServiceName VARCHAR, ChargeDescription VARCHAR, "
            "SkuId VARCHAR, ChargeCategory VARCHAR)"
        )
        con.executemany(
            "INSERT INTO src VALUES (?, ?, ?, ?)",
            [
                ("Amazon Redshift", "unused commitment", None, "Usage"),
                ("Amazon Redshift", "Redshift compute node", None, "Credit"),
                ("Amazon Simple Storage Service", "TimedStorage-ByteHrs", None, "Credit"),
                ("Amazon Elastic Compute Cloud", "BoxUsage:m5.large", None, "Usage"),
                # An S3 row must NOT be NULLIF'd against 'committed' — that exclusion
                # is Redshift-only, so a future S3 category named 'committed' survives.
                ("Amazon Simple Storage Service", "unused commitment", None, "Usage"),
            ],
        )
        rows = con.execute(f"SELECT ({_cost_subcategory_sql()}) FROM src").fetchall()
        subs = [r[0] for r in rows]
    finally:
        con.close()

    assert subs[:4] == [None, None, None, None]
    assert subs[4] == "other"  # classified by S3's rules, not swallowed by the NULLIF
