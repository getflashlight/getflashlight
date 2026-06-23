"""The create-export CLI builds a valid bcm-data-exports:CreateExport payload for
a FOCUS 1.2 export aligned with what the connector ingests."""

from auralake.ingest.aws_export_setup import (
    FOCUS_TABLE,
    build_export_request,
    default_query_statement,
)
from auralake.ingest.connectors._focus_map import map_focus_row


def test_default_query_targets_focus_1_2_table() -> None:
    sql = default_query_statement()
    assert sql.startswith("SELECT ")
    assert f"FROM {FOCUS_TABLE}" in sql
    assert FOCUS_TABLE == "FOCUS_1_2_AWS"


def test_default_query_selects_columns_the_mapper_reads() -> None:
    # The export projection must include every column map_focus_row pulls, or
    # ingestion would silently lose fields. Tags drives TCO attribution.
    sql = default_query_statement()
    for col in ("ProviderName", "ServiceName", "EffectiveCost", "ResourceId", "Tags"):
        assert col in sql


def test_projection_matches_mapper_contract() -> None:
    # Guard against drift: a row containing exactly the projected columns must map.
    cols = default_query_statement().removeprefix("SELECT ").split(f" FROM {FOCUS_TABLE}")[0]
    row = {c.strip(): "" for c in cols.split(",")}
    row["ChargePeriodStart"] = "2026-06-01 00:00:00"  # the one required field
    rec = map_focus_row(row, "aws_focus")
    assert rec is not None


def test_build_export_request_shape() -> None:
    req = build_export_request(
        name="auralake-focus",
        description="d",
        s3_bucket="my-bucket",
        s3_prefix="cid-focus/export",
        s3_region="us-west-1",
        query_statement="SELECT ServiceName FROM FOCUS_1_2_AWS",
        time_granularity="DAILY",
        overwrite="OVERWRITE_REPORT",
    )
    assert req["Name"] == "auralake-focus"
    assert req["DataQuery"]["TableConfigurations"] == {FOCUS_TABLE: {"TIME_GRANULARITY": "DAILY"}}
    s3 = req["DestinationConfigurations"]["S3Destination"]
    assert s3["S3Bucket"] == "my-bucket"
    assert s3["S3Region"] == "us-west-1"
    assert s3["S3OutputConfigurations"] == {
        "OutputType": "CUSTOM",
        "Format": "PARQUET",
        "Compression": "PARQUET",
        "Overwrite": "OVERWRITE_REPORT",
    }
    assert req["RefreshCadence"] == {"Frequency": "SYNCHRONOUS"}
