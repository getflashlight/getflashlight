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


class _FakeBcmClient:
    """Minimal stand-in for the bcm-data-exports client used by update/delete."""

    def __init__(
        self, exports: list[dict[str, str]], current: dict[str, object] | None = None
    ) -> None:
        self._exports = exports
        self._current = current or {}
        self.created: dict[str, object] | None = None
        self.updated: dict[str, object] | None = None
        self.deleted: str | None = None

    def list_exports(self, **_: object) -> dict[str, object]:
        return {"Exports": self._exports}

    def create_export(self, *, Export: dict[str, object]) -> dict[str, str]:  # noqa: N803 - boto3 API kwargs
        self.created = {"export": Export}
        return {"ExportArn": "arn:created"}

    def get_export(self, *, ExportArn: str) -> dict[str, object]:  # noqa: N803 - boto3 API kwargs
        return {"Export": self._current}

    def update_export(self, *, ExportArn: str, Export: dict[str, object]) -> dict[str, str]:  # noqa: N803 - boto3 API kwargs
        self.updated = {"arn": ExportArn, "export": Export}
        return {"ExportArn": ExportArn}

    def delete_export(self, *, ExportArn: str) -> dict[str, str]:  # noqa: N803 - boto3 API kwargs
        self.deleted = ExportArn
        return {"ExportArn": ExportArn}


def test_find_export_by_name() -> None:
    from auralake.ingest.aws_export_setup import _find_export_by_name

    client = _FakeBcmClient(
        [
            {"ExportName": "other", "ExportArn": "arn:other"},
            {"ExportName": "auralake-focus", "ExportArn": "arn:focus"},
        ]
    )
    assert _find_export_by_name(client, "auralake-focus") == "arn:focus"
    assert _find_export_by_name(client, "missing") is None


def test_perform_create_export_warns_on_duplicate_name(monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    import auralake.ingest.aws_export_setup as mod

    # An export with this name already exists → creating another should warn.
    client = _FakeBcmClient([{"ExportName": "auralake-focus", "ExportArn": "arn:existing"}])
    monkeypatch.setattr(mod, "_bcm_client", lambda _defaults: client)

    mod.perform_create_export(
        apply=True,
        name="auralake-focus",
        description="d",
        bucket="b",
        prefix="p",
        s3_region="us-east-1",
        time_granularity="DAILY",
        overwrite="OVERWRITE_REPORT",
        query_statement=None,
        connections="/nonexistent.yml",
        confirm=lambda: False,  # decline at the warning → no create
    )
    out = capsys.readouterr().out
    assert "already exists" in out
    assert "arn:existing" in out
    assert "Aborted" in out
    assert client.created is None  # nothing provisioned


def test_current_export_destination_reads_live_values(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import auralake.ingest.aws_export_setup as mod

    current = {
        "DestinationConfigurations": {
            "S3Destination": {
                "S3Bucket": "sphinx-test-2",
                "S3Prefix": "focus_data",
                "S3Region": "us-west-1",
            }
        }
    }
    client = _FakeBcmClient(
        [{"ExportName": "auralake-focus", "ExportArn": "arn:focus"}], current=current
    )
    monkeypatch.setattr(mod, "_bcm_client", lambda _defaults: client)

    dest = mod.current_export_destination("auralake-focus", "/nonexistent.yml")
    assert dest is not None
    assert dest["bucket"] == "sphinx-test-2"
    assert dest["prefix"] == "focus_data"
    assert dest["region"] == "us-west-1"
    # Unknown export name → None (caller falls back to config/state).
    monkeypatch.setattr(mod, "_bcm_client", lambda _defaults: _FakeBcmClient([]))
    assert mod.current_export_destination("missing", "/nonexistent.yml") is None


def test_perform_update_export_applies_to_named_arn(monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    import auralake.ingest.aws_export_setup as mod

    # The live export currently has the trailing-slash prefix we're fixing.
    current = {
        "DestinationConfigurations": {
            "S3Destination": {"S3Bucket": "my-bucket", "S3Prefix": "focus_data/"}
        }
    }
    client = _FakeBcmClient(
        [{"ExportName": "auralake-focus", "ExportArn": "arn:focus"}], current=current
    )
    monkeypatch.setattr(mod, "_bcm_client", lambda _defaults: client)

    mod.perform_update_export(
        apply=True,
        name="auralake-focus",
        description="d",
        bucket="my-bucket",
        prefix="focus_data/",  # trailing slash must be stripped before the request
        s3_region="us-west-1",
        time_granularity="DAILY",
        overwrite="OVERWRITE_REPORT",
        query_statement="SELECT ServiceName FROM FOCUS_1_2_AWS",
        connections="/nonexistent.yml",
    )
    assert client.updated is not None
    assert client.updated["arn"] == "arn:focus"
    export = client.updated["export"]
    assert isinstance(export, dict)
    s3 = export["DestinationConfigurations"]["S3Destination"]
    assert s3["S3Prefix"] == "focus_data"  # normalized, no double slash downstream
    out = capsys.readouterr().out
    assert "Update plan:" in out  # before→after plan is shown
    assert "focus_data/  ⇒  focus_data" in out  # the prefix change is surfaced
    assert "Updated export" in out


def test_perform_update_export_flags_bucket_policy_when_bucket_changes(monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    import auralake.ingest.aws_export_setup as mod

    current = {
        "DestinationConfigurations": {"S3Destination": {"S3Bucket": "old-bkt", "S3Prefix": "p"}}
    }
    client = _FakeBcmClient(
        [{"ExportName": "auralake-focus", "ExportArn": "arn:focus"}], current=current
    )
    monkeypatch.setattr(mod, "_bcm_client", lambda _defaults: client)

    mod.perform_update_export(
        apply=False,  # dry-run: just show the plan + guidance, don't mutate
        name="auralake-focus",
        description="d",
        bucket="new-bkt",
        prefix="p",
        s3_region="us-east-1",
        time_granularity="DAILY",
        overwrite="OVERWRITE_REPORT",
        query_statement=None,
        connections="/nonexistent.yml",
    )
    out = capsys.readouterr().out
    assert client.updated is None  # dry-run
    assert "old-bkt  ⇒  new-bkt" in out
    assert "bucket-policy --bucket new-bkt" in out  # guides the policy step
    assert "DRY RUN" in out


def test_perform_update_export_confirm_false_aborts(monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    import auralake.ingest.aws_export_setup as mod

    client = _FakeBcmClient([{"ExportName": "auralake-focus", "ExportArn": "arn:focus"}])
    monkeypatch.setattr(mod, "_bcm_client", lambda _defaults: client)

    mod.perform_update_export(
        apply=True,
        name="auralake-focus",
        description="d",
        bucket="b",
        prefix="p",
        s3_region="us-east-1",
        time_granularity="DAILY",
        overwrite="OVERWRITE_REPORT",
        query_statement=None,
        connections="/nonexistent.yml",
        confirm=lambda: False,  # user declines
    )
    assert client.updated is None  # nothing applied
    assert "Aborted" in capsys.readouterr().out


def test_perform_update_export_unknown_name_raises(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import auralake.ingest.aws_export_setup as mod

    monkeypatch.setattr(mod, "_bcm_client", lambda _defaults: _FakeBcmClient([]))
    try:
        mod.perform_update_export(
            apply=True,
            name="auralake-focus",
            description="d",
            bucket="b",
            prefix="p",
            s3_region="us-east-1",
            time_granularity="DAILY",
            overwrite="OVERWRITE_REPORT",
            query_statement=None,
            connections="/nonexistent.yml",
        )
        raise AssertionError("expected ValueError for missing export")
    except ValueError as exc:
        assert "no export named" in str(exc)


def test_perform_delete_export_dry_run_does_not_delete(monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    import auralake.ingest.aws_export_setup as mod

    client = _FakeBcmClient([{"ExportName": "auralake-focus", "ExportArn": "arn:focus"}])
    monkeypatch.setattr(mod, "_bcm_client", lambda _defaults: client)

    mod.perform_delete_export(apply=False, name="auralake-focus", connections="/nonexistent.yml")
    assert client.deleted is None  # dry run
    assert "DRY RUN" in capsys.readouterr().out

    mod.perform_delete_export(apply=True, name="auralake-focus", connections="/nonexistent.yml")
    assert client.deleted == "arn:focus"


def test_bucket_policy_grants_data_exports_put_object() -> None:
    doc = bucket_policy_document("my-bucket", "123456789012")
    stmt = doc["Statement"][0]
    assert stmt["Principal"]["Service"] == ["bcm-data-exports.amazonaws.com"]
    assert stmt["Action"] == ["s3:PutObject"]
    assert stmt["Resource"] == "arn:aws:s3:::my-bucket/*"
    # Scoped to the caller's account on both the source ARN and source account.
    assert stmt["Condition"]["StringEquals"]["aws:SourceAccount"] == "123456789012"
    assert "123456789012" in stmt["Condition"]["ArnLike"]["aws:SourceArn"]
