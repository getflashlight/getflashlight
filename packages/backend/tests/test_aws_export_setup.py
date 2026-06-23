"""The create-export CLI builds a valid bcm-data-exports:CreateExport payload for
a FOCUS 1.2 export aligned with what the connector ingests."""

from auralake.ingest.aws_export_setup import (
    FOCUS_TABLE,
    bucket_policy_document,
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


def test_state_roundtrip_and_precedence(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("AURALAKE_STATE_DIR", str(tmp_path))
    from auralake.ingest.aws_export_setup import load_state, resolved_targets, save_state

    assert load_state() == {}
    save_state(bucket="b1", region="us-west-2", prefix=None)  # None values are dropped
    assert load_state() == {"bucket": "b1", "region": "us-west-2"}

    none = str(tmp_path / "none.yml")  # empty connections → falls through to remembered
    rb, rp, rr, _ = resolved_targets(None, None, None, none)
    assert (rb, rr) == ("b1", "us-west-2")
    assert rp is None  # prefix was never recorded
    # An explicit flag always wins over the remembered value.
    rb2, *_ = resolved_targets("flagged", None, None, none)
    assert rb2 == "flagged"


def test_print_export_shows_status_and_last_run(capsys) -> None:  # type: ignore[no-untyped-def]
    from auralake.ingest.aws_export_setup import _print_export

    export = {
        "Name": "auralake-focus",
        "DataQuery": {"TableConfigurations": {"FOCUS_1_2_AWS": {"TIME_GRANULARITY": "DAILY"}}},
        "DestinationConfigurations": {"S3Destination": {"S3Bucket": "b", "S3Prefix": "focus/"}},
        "RefreshCadence": {"Frequency": "SYNCHRONOUS"},
    }
    status = {"StatusCode": "HEALTHY", "CreatedAt": "2026-06-23", "LastRefreshedAt": "2026-06-24"}
    _print_export("arn:aws:bcm-data-exports:…:export/auralake-focus", export, status)
    out = capsys.readouterr().out
    assert "FOCUS_1_2_AWS" in out and "granularity DAILY" in out
    # The AWS generation time is distinct from S3 delivery (the S3 section is truth).
    assert "generated:" in out and "2026-06-24" in out


def test_prefix_eq_ignores_slashes() -> None:
    from auralake.ingest.aws_export_setup import _prefix_eq

    assert _prefix_eq("focus_data/", "focus_data")
    assert _prefix_eq("/a/b/", "a/b")
    assert _prefix_eq(None, "")
    assert not _prefix_eq("a", "b")


def test_human_bytes() -> None:
    from auralake.ingest.aws_export_setup import _human_bytes

    assert _human_bytes(0) == "0 B"
    assert _human_bytes(1536) == "1.5 KB"
    assert _human_bytes(5 * 1024**3).endswith("GB")


def test_bucket_policy_grants_data_exports_put_object() -> None:
    doc = bucket_policy_document("my-bucket", "123456789012")
    stmt = doc["Statement"][0]
    assert stmt["Principal"]["Service"] == ["bcm-data-exports.amazonaws.com"]
    assert stmt["Action"] == ["s3:PutObject"]
    assert stmt["Resource"] == "arn:aws:s3:::my-bucket/*"
    # Scoped to the caller's account on both the source ARN and source account.
    assert stmt["Condition"]["StringEquals"]["aws:SourceAccount"] == "123456789012"
    assert "123456789012" in stmt["Condition"]["ArnLike"]["aws:SourceArn"]
