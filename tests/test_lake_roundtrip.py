"""End-to-end: BRONZE Parquet write → partition-replace → GOLD build → read.

Exercises the riskiest part of the rearchitecture — the DuckDB SQL ports
(json_extract_string over the tags JSON string, the per-provider GOLD slicing) —
against a real in-memory DuckDB over real Parquet, with no database.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from flashlight.core.settings import get_settings
from flashlight.focus.enums import (
    ChargeCategory,
    ComputeClass,
    ProviderName,
    ServiceCategory,
)
from flashlight.focus.model import FocusRecord
from flashlight.ingest.base import IngestWindow
from flashlight.lake.storage_location_schema import StorageLocationRecord

_WINDOW = IngestWindow(date(2026, 5, 1), date(2026, 5, 31))


@pytest.fixture
def lake_home(tmp_path, monkeypatch) -> Iterator[object]:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("FLASHLIGHT_HOME", str(tmp_path))
    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


def _rec(
    provider: ProviderName,
    service: str,
    cost: str,
    *,
    tags: dict[str, str] | None = None,
    resource_id: str | None = None,
    compute: ComputeClass = ComputeClass.NOT_APPLICABLE,
    list_cost: str | None = None,
    day: int = 15,
) -> FocusRecord:
    amount = Decimal(cost)
    return FocusRecord(
        provider_name=provider,
        billing_account_id="acct",
        billing_period_start=date(2026, 5, 1),
        billing_period_end=date(2026, 5, 31),
        charge_period_start=datetime(2026, 5, day, tzinfo=UTC),
        charge_period_end=datetime(2026, 5, day, 1, tzinfo=UTC),
        billed_cost=amount,
        effective_cost=amount,
        list_cost=Decimal(list_cost) if list_cost is not None else amount,
        charge_category=ChargeCategory.USAGE,
        service_category=ServiceCategory.COMPUTE,
        service_name=service,
        resource_id=resource_id,
        tags=tags or {},
        x_compute_class=compute,
        x_source_connector="t",
    )


def test_partition_replace_is_idempotent(lake_home) -> None:  # type: ignore[no-untyped-def]
    from flashlight.lake import bronze, duck

    records = [_rec(ProviderName.AWS, "AmazonEC2", "10"), _rec(ProviderName.AWS, "AmazonS3", "5")]
    assert bronze.write_window("t", _WINDOW, records, ingest_run_id="r1") == 2
    # Re-ingesting the same window replaces rather than appends.
    assert bronze.write_window("t", _WINDOW, records, ingest_run_id="r2") == 2

    con = duck.connect()
    duck.register_bronze(con)
    row = con.execute("SELECT count(*) FROM raw.focus_record").fetchone()
    assert row is not None
    assert row[0] == 2


def test_transform_builds_gold_split_per_provider(lake_home) -> None:  # type: ignore[no-untyped-def]
    from flashlight.gold.reader import query_view
    from flashlight.lake import bronze
    from flashlight.transform.runner import build_gold

    records = [
        # Tagged AWS spend → lands in the tag/allocation views.
        _rec(ProviderName.AWS, "AmazonEC2", "100", tags={"ClusterId": "c1"}, resource_id="i-1"),
        # Untagged AWS → the honest untagged remainder.
        _rec(ProviderName.AWS, "AmazonS3", "20", resource_id="bkt"),
        # A second provider, so the per-provider fan-out has something to split.
        _rec(ProviderName.DATABRICKS, "jobs", "40", resource_id="c1", compute=ComputeClass.CLASSIC),
    ]
    bronze.write_window("t", _WINDOW, records, ingest_run_id="r1")

    published = build_gold()
    assert published > 0

    # GOLD is split per provider on disk: gold/<group>/<view>.parquet.
    from flashlight.lake import paths

    gold = paths.gold_dir()
    assert (gold / "aws" / "monthly_bill.parquet").exists()
    assert (gold / "databricks" / "monthly_bill.parquet").exists()

    # Per-provider files are pre-sliced: every row carries that provider.
    aws_bill = query_view("aws.monthly_bill")
    assert aws_bill, "aws.monthly_bill should have rows"
    assert {r["provider_name"] for r in aws_bill} == {"AWS"}
    assert {r["provider_name"] for r in query_view("databricks.monthly_bill")} == {"Databricks"}

    # Tags survive the round-trip as a JSON string and come back out via
    # json_extract_string — the one non-obvious part of the Parquet schema.
    tagged = query_view("aws.spend_by_tag_month")
    assert [(r["tag_key"], r["tag_value"]) for r in tagged] == [("ClusterId", "c1")]
    assert tagged[0]["net_cost"] == pytest.approx(100.0)

    # Attribution honesty: untagged spend is surfaced, not silently dropped.
    coverage = query_view("aws.spend_tag_coverage_month")
    assert coverage, "aws.spend_tag_coverage_month should have rows"
    assert coverage[0]["tagged_cost"] == pytest.approx(100.0)
    assert coverage[0]["untagged_cost"] == pytest.approx(20.0)


def test_write_window_accepts_generator_input(lake_home) -> None:  # type: ignore[no-untyped-def]
    from flashlight.lake import bronze, duck

    def _records() -> Iterator[FocusRecord]:
        yield _rec(ProviderName.AWS, "AmazonEC2", "10")
        yield _rec(ProviderName.AWS, "AmazonS3", "5")

    assert bronze.write_window("t", _WINDOW, _records(), ingest_run_id="r1") == 2
    con = duck.connect()
    duck.register_bronze(con)
    row = con.execute("SELECT count(*) FROM raw.focus_record").fetchone()
    assert row is not None
    assert row[0] == 2


def test_write_window_chunks_and_dedupes_across_chunks(  # type: ignore[no-untyped-def]
    lake_home, monkeypatch
) -> None:
    from flashlight.lake import bronze, duck

    monkeypatch.setattr(bronze, "CHUNK_ROWS", 2)

    def _records() -> Iterator[FocusRecord]:
        # 5 records, one truly repeated (identical in every field) across what will
        # be two different chunks under CHUNK_ROWS=2 — the duplicate must still be
        # dropped. A same-resource-different-cost row is NOT a duplicate — it's a
        # distinct charge that happens to share a dimension — so must NOT collapse.
        yield _rec(ProviderName.AWS, "AmazonEC2", "1", resource_id="i-1")
        yield _rec(ProviderName.AWS, "AmazonEC2", "2", resource_id="i-2")
        yield _rec(ProviderName.AWS, "AmazonEC2", "3", resource_id="i-3")
        yield _rec(ProviderName.AWS, "AmazonEC2", "1", resource_id="i-1")  # true dup of i-1
        yield _rec(ProviderName.AWS, "AmazonEC2", "4", resource_id="i-4")

    written = bronze.write_window("t", _WINDOW, _records(), ingest_run_id="r1")
    assert written == 4

    con = duck.connect()
    duck.register_bronze(con)
    row = con.execute("SELECT count(*) FROM raw.focus_record").fetchone()
    assert row is not None
    assert row[0] == 4


def test_write_window_repurges_on_mid_stream_failure(  # type: ignore[no-untyped-def]
    lake_home,
) -> None:
    from flashlight.lake import bronze, paths

    def _records() -> Iterator[FocusRecord]:
        yield _rec(ProviderName.AWS, "AmazonEC2", "10")
        raise RuntimeError("connector blew up mid-stream")

    with pytest.raises(RuntimeError):
        bronze.write_window("t", _WINDOW, _records(), ingest_run_id="r1")

    connector_dir = paths.bronze_dir() / "x_source_connector=t"
    assert not connector_dir.exists() or not any(connector_dir.iterdir())


def test_tag_explosion(lake_home) -> None:  # type: ignore[no-untyped-def]
    from flashlight.gold.reader import query_view
    from flashlight.lake import bronze
    from flashlight.transform.runner import build_gold

    records = [
        _rec(ProviderName.AWS, "AmazonEC2", "30", tags={"team": "data", "env": "prod"}),
    ]
    bronze.write_window("t", _WINDOW, records, ingest_run_id="r1")
    build_gold()

    rows = query_view("aws.spend_by_tag_month")
    pairs = {(r["tag_key"], r["tag_value"]) for r in rows}
    assert ("team", "data") in pairs
    assert ("env", "prod") in pairs


def test_tag_coverage_keeps_untagged_spend_visible(lake_home) -> None:  # type: ignore[no-untyped-def]
    """The tag views drop untagged rows; coverage is what stops that reading as 100%."""
    from flashlight.gold.reader import query_view
    from flashlight.lake import bronze
    from flashlight.transform.runner import build_gold

    bronze.write_window(
        "t",
        _WINDOW,
        [
            _rec(ProviderName.AWS, "AmazonEC2", "30", tags={"team": "data"}),
            _rec(ProviderName.AWS, "AmazonS3", "10"),  # untagged
        ],
        ingest_run_id="r1",
    )
    build_gold()

    row = query_view("aws.spend_tag_coverage_month")[0]
    assert float(row["net_cost"]) == pytest.approx(40.0)
    assert float(row["tagged_cost"]) == pytest.approx(30.0)
    assert float(row["untagged_cost"]) == pytest.approx(10.0)
    assert float(row["tagged_pct"]) == pytest.approx(75.0)

    # The breakdown view really does omit that $10 — which is why coverage exists.
    tagged_total = sum(float(r["net_cost"]) for r in query_view("aws.spend_by_tag_month"))
    assert tagged_total == pytest.approx(30.0)


def test_tag_coverage_excludes_credits(lake_home) -> None:  # type: ignore[no-untyped-def]
    """Untagged credits must not push coverage above 100% or untagged_cost below zero.

    Regression: measuring coverage over *net* cost on the FOCUS sample (which carries
    untagged credits) reported tagged_pct = 281.8% and untagged_cost = -$100.
    """
    from flashlight.gold.reader import query_view
    from flashlight.lake import bronze
    from flashlight.transform.runner import build_gold

    credit = _rec(ProviderName.AWS, "AmazonEC2", "-100")
    credit.charge_category = ChargeCategory.CREDIT
    bronze.write_window(
        "t",
        _WINDOW,
        [
            _rec(ProviderName.AWS, "AmazonEC2", "120", tags={"team": "data"}),
            _rec(ProviderName.AWS, "AmazonS3", "40"),  # untagged charge
            credit,  # untagged credit — the row that broke the naive version
        ],
        ingest_run_id="r1",
    )
    build_gold()

    row = query_view("aws.spend_tag_coverage_month")[0]
    assert float(row["net_cost"]) == pytest.approx(60.0)  # 120 + 40 - 100
    assert float(row["gross_cost"]) == pytest.approx(160.0)  # charges only
    assert float(row["tagged_cost"]) == pytest.approx(120.0)
    assert float(row["untagged_cost"]) == pytest.approx(40.0)
    assert float(row["tagged_pct"]) == pytest.approx(75.0)
    assert 0 <= float(row["tagged_pct"]) <= 100


def test_service_month_list_and_savings_reconcile_to_the_headline(lake_home) -> None:  # type: ignore[no-untyped-def]
    """`spend_by_service_month` is `monthly_bill` at one finer grain, so summing its
    list_cost/savings over every service must equal the headline exactly.

    This is the property the /aws page's KPI row rests on: scoped to a subset of
    services, it builds list / savings / realized-discount from this view, and those
    figures have to be the same dollars the provider-wide page would report. If the two
    ever diverge, one of the pages is lying about the discount.
    """
    from flashlight.gold.reader import query_view
    from flashlight.lake import bronze
    from flashlight.transform.runner import build_gold

    bronze.write_window(
        "t",
        _WINDOW,
        [
            # Discounted: list 100, paid 80 → 20 of savings.
            _rec(ProviderName.AWS, "AmazonEC2", "80", list_cost="100"),
            # Undiscounted: list == effective → no savings.
            _rec(ProviderName.AWS, "AmazonS3", "20"),
            _rec(ProviderName.DATABRICKS, "jobs", "40", list_cost="50"),
        ],
        ingest_run_id="r1",
    )
    build_gold()

    for group in ("aws", "databricks"):
        bill = query_view(f"{group}.monthly_bill")
        assert len(bill) == 1, f"{group} should have exactly one month"
        services = query_view(f"{group}.spend_by_service_month")

        for measure in ("net_cost", "gross_cost", "list_cost", "savings"):
            assert sum(float(r[measure]) for r in services) == pytest.approx(
                float(bill[0][measure])
            ), f"{group}.spend_by_service_month {measure} must reconcile to monthly_bill"

        # And to the savings view, the other consumer of the same subtraction.
        summary = query_view(f"{group}.savings_summary_month")
        assert sum(float(r["savings"]) for r in services) == pytest.approx(
            sum(float(r["savings"]) for r in summary)
        )

    # The discount is per service, not smeared across the provider.
    by_service = {r["service_name"]: r for r in query_view("aws.spend_by_service_month")}
    assert float(by_service["AmazonEC2"]["savings"]) == pytest.approx(20.0)
    assert float(by_service["AmazonS3"]["savings"]) == pytest.approx(0.0)


def test_spend_trend_daily_is_one_row_per_day_and_service(lake_home) -> None:  # type: ignore[no-untyped-def]
    """`spend_trend_daily` carries service_name, so it is NOT one row per day.

    Every consumer wanting a provider-wide daily series must aggregate over
    service_name. A chart that forgets to would get several points per x and draw a
    zig-zag that reads as real volatility rather than a bug, which is why the grain is
    pinned here rather than left to the panel that happens to consume it.
    """
    from flashlight.gold.reader import query_view
    from flashlight.lake import bronze
    from flashlight.transform.runner import build_gold

    bronze.write_window(
        "t",
        _WINDOW,
        [
            _rec(ProviderName.AWS, "AmazonEC2", "10", day=15),
            _rec(ProviderName.AWS, "AmazonS3", "5", day=15),  # same day, second service
            _rec(ProviderName.AWS, "AmazonEC2", "7", day=16),
        ],
        ingest_run_id="r1",
    )
    build_gold()

    rows = query_view("aws.spend_trend_daily")
    assert {r["service_name"] for r in rows} == {"AmazonEC2", "AmazonS3"}
    # Two services on the 15th → two rows for that day, one for the 16th.
    assert len([r for r in rows if str(r["charge_day"]) == "2026-05-15"]) == 2
    assert len([r for r in rows if str(r["charge_day"]) == "2026-05-16"]) == 1

    # Aggregated, it still ties to the headline — the widening added a dimension, not cost.
    bill = query_view("aws.monthly_bill")
    assert sum(float(r["net_cost"]) for r in rows) == pytest.approx(float(bill[0]["net_cost"]))

    # Scoped to one service, it gives a daily series the monthly views cannot — the
    # whole point of the added dimension.
    ec2 = dict(
        sorted(
            (str(r["charge_day"]), float(r["net_cost"]))
            for r in rows
            if r["service_name"] == "AmazonEC2"
        )
    )
    assert list(ec2) == ["2026-05-15", "2026-05-16"]
    assert ec2["2026-05-15"] == pytest.approx(10.0)
    assert ec2["2026-05-16"] == pytest.approx(7.0)


# ── backing storage: AWS-billed S3 cost labelled by Unity Catalog ─────────────
_S3 = "Amazon Simple Storage Service"


def _s3_rec(cost: str, resource_id: str | None, *, day: int = 15) -> FocusRecord:
    rec = _rec(ProviderName.AWS, _S3, cost, resource_id=resource_id, day=day)
    rec.service_category = ServiceCategory.STORAGE
    return rec


def _loc(
    name: str,
    url: str,
    bucket: str | None,
    prefix: str | None,
    *,
    cloud: str = "AWS",
    scheme: str = "s3",
    kind: str = "external_location",
) -> StorageLocationRecord:
    return StorageLocationRecord(
        provider_name="Databricks",
        snapshot_month=date(2026, 5, 1),
        location_kind=kind,
        location_name=name,
        url=url,
        scheme=scheme,
        cloud_provider_name=cloud,
        bucket_name=bucket,
        key_prefix=prefix,
        x_source_connector="databricks",
    )


def _seed_backing_storage() -> None:
    """One S3 bill covering every mapping/confidence case, plus a UC map over it.

    Only ``metastore_root`` counts as Databricks-managed storage — external locations and
    catalog roots are recorded but deliberately don't cost (they'd double-claim data that
    pre-existed Databricks). The fixture therefore has to distinguish four things a single
    "is it in UC?" test could not.
    """
    from flashlight.lake import bronze
    from flashlight.lake.storage_locations import write_storage_locations
    from flashlight.transform.runner import build_gold

    bronze.write_window(
        "t",
        _WINDOW,
        [
            # Metastore root at the BUCKET ROOT → databricks / whole_bucket.
            # Two rows, so the fan-out guard has real cost to (not) multiply.
            _s3_rec("100", "arn:aws:s3:::acme-uc-root", day=2),
            _s3_rec("10", "arn:aws:s3:::acme-uc-root", day=3),
            # Metastore root under a PREFIX (the realistic shape,
            # s3://bucket/<metastore-id>) → databricks / prefix_scoped, an upper bound.
            _s3_rec("60", "arn:aws:s3:::acme-metastore-prefixed", day=4),
            # EXTERNAL LOCATION ONLY → unmapped. Registered for access, not owned.
            _s3_rec("50", "arn:aws:s3:::acme-external-lake", day=5),
            # BOTH a metastore root and an external location → databricks wins.
            _s3_rec("30", "arn:aws:s3:::acme-both", day=6),
            # No UC location at all → unmapped.
            _s3_rec("25", "arn:aws:s3:::random-other", day=7),
            # No ResourceId at all → attributable to no bucket.
            _s3_rec("7", None, day=8),
            # Databricks' own DBU spend, so the invariant assertion has something to guard.
            _rec(ProviderName.DATABRICKS, "jobs", "40", resource_id="c1"),
        ],
        ingest_run_id="r1",
    )
    write_storage_locations(
        [
            _loc("acme", "s3://acme-uc-root", "acme-uc-root", None, kind="metastore_root"),
            # A catalog root on the SAME bucket — must not add to location_count, and must
            # not multiply that bucket's cost.
            _loc("main", "s3://acme-uc-root/main", "acme-uc-root", "main", kind="catalog"),
            _loc(
                "acme-prefixed",
                "s3://acme-metastore-prefixed/1234-metastore",
                "acme-metastore-prefixed",
                "1234-metastore",
                kind="metastore_root",
            ),
            # External only: in the map, but never costed.
            _loc("lake", "s3://acme-external-lake/dbx", "acme-external-lake", "dbx"),
            # Managed precedence: an external location on a bucket that is ALSO a
            # metastore root must not demote it.
            _loc("both-ext", "s3://acme-both/ext", "acme-both", "ext"),
            _loc("both-ms", "s3://acme-both/ms", "acme-both", "ms", kind="metastore_root"),
            # Another cloud entirely — must never join an AWS cost row.
            _loc("gcs", "gs://gbucket/x", "gbucket", "x", cloud="Google Cloud", scheme="gs"),
        ]
    )
    build_gold()


def test_backing_storage_accounts_for_every_s3_row(lake_home) -> None:  # type: ignore[no-untyped-def]
    """The honest-denominator contract: summing every `mapping` value reproduces the
    account's whole S3 bill.

    This is what makes "X% of your S3 spend is Databricks-managed" a real percentage
    rather than a share of whatever happened to map. If a row could be dropped (or
    double-counted) the managed figure would still look plausible.
    """
    from flashlight.gold.reader import run_select

    _seed_backing_storage()

    total = float(
        run_select("SELECT sum(net_cost) AS t FROM storage.backing_storage_month")[0]["t"]
    )
    s3_line = float(
        run_select(
            "SELECT sum(net_cost) AS t FROM aws.spend_by_service_month "
            f"WHERE service_name = '{_S3}'"
        )[0]["t"]
    )
    assert total == s3_line == 282.0

    by_mapping = {
        r["mapping"]: float(r["c"])
        for r in run_select(
            "SELECT mapping, sum(net_cost) AS c FROM storage.backing_storage_month GROUP BY mapping"
        )
    }
    # managed = acme-uc-root 110 + acme-metastore-prefixed 60 + acme-both 30
    # unmapped = acme-external-lake 50 (external only!) + random-other 25
    assert by_mapping == {"databricks": 200.0, "unmapped": 75.0, "no_resource_id": 7.0}


def test_backing_storage_does_not_multiply_a_bucket_with_two_uc_locations(lake_home) -> None:  # type: ignore[no-untyped-def]
    """Several UC locations on one bucket must not double that bucket's cost.

    The bug this prevents is a plain LEFT JOIN against the location rows, which would
    invent spend that is not on the bill — the aggregation to one row per bucket in
    065_gold_storage.sql exists for exactly this. `acme-uc-root` carries a metastore root
    AND a managed catalog root — both count as managed storage, so location_count is 2, but
    the cost must still be the sum of its own two bill rows, not double that.
    """
    from flashlight.gold.reader import run_select

    _seed_backing_storage()

    rows = run_select(
        "SELECT sum(net_cost) AS c, max(location_count) AS locs "
        "FROM storage.backing_storage_month WHERE bucket_name = 'acme-uc-root'"
    )
    assert float(rows[0]["c"]) == 110.0  # 100 + 10, NOT 220
    assert int(rows[0]["locs"]) == 2  # both managed objects counted, cost still not doubled


def test_backing_storage_labels_confidence_and_keeps_the_gaps(lake_home) -> None:  # type: ignore[no-untyped-def]
    """A bucket-root location reads whole_bucket; a prefix-only one reads prefix_scoped
    (its cost is an upper bound); an S3 row with no ResourceId is kept as its own bucket
    rather than vanishing; a non-AWS UC location never joins."""
    from flashlight.gold.reader import run_select

    _seed_backing_storage()

    confidence = {
        r["bucket_name"]: r["mapping_confidence"]
        for r in run_select(
            "SELECT bucket_name, any_value(mapping_confidence) AS mapping_confidence "
            "FROM storage.backing_storage_month GROUP BY bucket_name"
        )
    }
    assert confidence["acme-uc-root"] == "whole_bucket"
    assert confidence["acme-metastore-prefixed"] == "prefix_scoped"
    # External-only and unrelated buckets are both simply not managed storage.
    assert confidence["acme-external-lake"] == "n/a"
    assert confidence["random-other"] == "n/a"
    assert confidence["(no resource id)"] == "n/a"

    # The gs:// bucket is in the map but can never be a mapped AWS cost row.
    assert "gbucket" not in confidence
    mapped_buckets = {
        r["bucket_name"]
        for r in run_select(
            "SELECT DISTINCT bucket_name FROM storage.backing_storage_month "
            "WHERE mapping = 'databricks'"
        )
    }
    # acme-external-lake is absent: registered for access, not Databricks-owned.
    assert mapped_buckets == {"acme-uc-root", "acme-metastore-prefixed", "acme-both"}


def test_backing_storage_never_changes_databricks_spend(lake_home) -> None:  # type: ignore[no-untyped-def]
    """The invariant guard: `databricks.monthly_bill` is identical with and without the
    storage plane.

    CLAUDE.md forbids joining Databricks DBU cost to the AWS infra behind it. The
    backing-storage views join AWS cost to Databricks *metadata*, and live in their own
    GOLD group precisely so nothing can leak into gold/databricks/. This asserts that
    structurally rather than trusting the SQL to stay well-behaved.
    """
    from flashlight.gold.reader import run_select
    from flashlight.lake import bronze
    from flashlight.transform.runner import build_gold

    # First: the same cost rows, with NO storage-location map at all.
    bronze.write_window(
        "t",
        _WINDOW,
        [
            _s3_rec("100", "arn:aws:s3:::acme-uc-root", day=2),
            _rec(ProviderName.DATABRICKS, "jobs", "40", resource_id="c1"),
        ],
        ingest_run_id="r1",
    )
    build_gold()
    before = run_select("SELECT * FROM databricks.monthly_bill ORDER BY charge_month")

    # Then add the map and rebuild.
    from flashlight.lake.storage_locations import write_storage_locations

    write_storage_locations(
        [_loc("acme", "s3://acme-uc-root", "acme-uc-root", None, kind="metastore_root")]
    )
    build_gold()
    after = run_select("SELECT * FROM databricks.monthly_bill ORDER BY charge_month")

    assert before == after
    # And the Databricks bill is its DBU spend alone — the S3 dollars are not in it.
    assert float(after[0]["net_cost"]) == 40.0


def test_backing_storage_attributes_cost_per_catalog(lake_home) -> None:  # type: ignore[no-untyped-def]
    """`managed_name` names the Unity Catalog object that owns each bucket, so cost can be
    read per catalog — and a metastore root wins when a bucket carries both.

    Honest only because each managed object sits on its own bucket; where several catalogs
    share one, the view must refuse to name a single owner (asserted below) rather than
    attributing its neighbours' bytes to whichever name sorted first.
    """
    from flashlight.gold.reader import run_select
    from flashlight.lake import bronze
    from flashlight.lake.storage_locations import write_storage_locations
    from flashlight.transform.runner import build_gold

    bronze.write_window(
        "t",
        _WINDOW,
        [
            _s3_rec("100", "arn:aws:s3:::bronze-cat", day=2),
            _s3_rec("40", "arn:aws:s3:::silver-cat", day=3),
            # Two catalogs on ONE bucket: unsplittable from the AWS bill.
            _s3_rec("70", "arn:aws:s3:::shared-cats", day=4),
            # Both a metastore root and a catalog: the metastore is the broader container.
            _s3_rec("25", "arn:aws:s3:::ms-and-cat", day=5),
        ],
        ingest_run_id="r1",
    )
    write_storage_locations(
        [
            _loc("bronze", "s3://bronze-cat", "bronze-cat", None, kind="catalog"),
            _loc("silver", "s3://silver-cat", "silver-cat", None, kind="catalog"),
            _loc("cat_a", "s3://shared-cats/a", "shared-cats", "a", kind="catalog"),
            _loc("cat_b", "s3://shared-cats/b", "shared-cats", "b", kind="catalog"),
            _loc("the-metastore", "s3://ms-and-cat", "ms-and-cat", None, kind="metastore_root"),
            _loc("nested_cat", "s3://ms-and-cat/c", "ms-and-cat", "c", kind="catalog"),
        ]
    )
    build_gold()

    by_object = {
        r["managed_name"]: (r["managed_kind"], float(r["c"]))
        for r in run_select(
            "SELECT managed_name, any_value(managed_kind) AS managed_kind, "
            "sum(net_cost) AS c FROM storage.backing_storage_month "
            "WHERE mapping = 'databricks' GROUP BY managed_name"
        )
    }
    assert by_object["bronze"] == ("catalog", 100.0)
    assert by_object["silver"] == ("catalog", 40.0)
    # A metastore root beats a catalog nested in the same bucket.
    assert by_object["the-metastore"] == ("metastore_root", 25.0)
    # Shared bucket: no single catalog is named, and the cost is NOT multiplied by 2.
    assert by_object["(shared by 2 catalogs)"] == ("catalog", 70.0)
    assert "cat_a" not in by_object and "cat_b" not in by_object
