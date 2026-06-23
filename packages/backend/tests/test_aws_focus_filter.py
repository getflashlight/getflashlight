"""The aws_focus connector scopes an account-wide FOCUS export to a service
allow-list, selects current-version files via the per-partition manifest, and
only reads billing periods that overlap the ingest window."""

from datetime import date

from auralake.focus.model import FocusRecord
from auralake.ingest.base import IngestWindow
from auralake.ingest.connectors._focus_map import map_focus_row
from auralake.ingest.connectors.aws_focus import (
    _extract_data_file_keys,
    _period_in_window,
    _scan_sql,
    _service_allowed,
)


def _record(service_name: str) -> FocusRecord:
    row = {
        "ProviderName": "AWS",
        "BillingAccountId": "acct-1",
        "BillingPeriodStart": "2026-06-01",
        "BillingPeriodEnd": "2026-06-30",
        "ChargePeriodStart": "2026-06-18 22:00:00",
        "ChargePeriodEnd": "2026-06-18 23:00:00",
        "EffectiveCost": "1.0",
        "ServiceName": service_name,
    }
    rec = map_focus_row(row, "aws_focus")
    assert rec is not None
    return rec


# ── service allow-list ────────────────────────────────────────────────────────
def test_empty_allow_list_passes_every_service() -> None:
    assert _service_allowed(_record("Amazon Redshift"), set())
    assert _service_allowed(_record("Amazon Whatever"), set())


def test_allow_list_keeps_only_listed_services() -> None:
    allowed = {"Amazon Redshift", "Amazon Simple Storage Service"}
    assert _service_allowed(_record("Amazon Redshift"), allowed)
    assert not _service_allowed(_record("Amazon CloudFront"), allowed)


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


# ── DuckDB scan SQL ───────────────────────────────────────────────────────────
def test_scan_sql_pushes_down_services_and_window() -> None:
    win = IngestWindow(start=date(2026, 6, 1), end=date(2026, 6, 30))
    sql, params = _scan_sql(["s3://b/f.parquet"], {"Amazon Redshift"}, win)
    assert "read_parquet(['s3://b/f.parquet'], union_by_name=true)" in sql
    assert '"ChargePeriodStart" >= $start' in sql
    assert '"ServiceName" = ANY($services)' in sql
    assert params == {"start": win.start, "end": win.end, "services": ["Amazon Redshift"]}


def test_scan_sql_omits_service_filter_when_allow_list_empty() -> None:
    win = IngestWindow(start=date(2026, 6, 1), end=date(2026, 6, 30))
    sql, params = _scan_sql(["s3://b/f.parquet"], set(), win)
    assert "ServiceName" not in sql
    assert "services" not in params
