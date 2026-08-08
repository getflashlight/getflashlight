"""End-to-end: EfficiencyRecord → metrics Parquet → GOLD waste classification.

Exercises the riskiest part of the waste plane — the classification SQL
(050_gold_waste.sql) and the metrics register/COPY path — against a real in-memory
DuckDB over real Parquet. No warehouse needed: the connector's warehouse pull is the
only un-unit-testable piece; everything downstream of EfficiencyRecord is covered here.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from decimal import Decimal

import pytest

from flashlight.core.settings import get_settings
from flashlight.efficiency.model import EfficiencyRecord, EntityType
from flashlight.ingest.base import IngestWindow

_WINDOW = IngestWindow(date(2026, 5, 1), date(2026, 5, 31))
_MONTH = date(2026, 5, 1)


@pytest.fixture
def lake_home(tmp_path, monkeypatch) -> Iterator[object]:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("FLASHLIGHT_HOME", str(tmp_path))
    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


def _rec(entity_id: str, entity_type: EntityType, cost: str, **kw: object) -> EfficiencyRecord:
    return EfficiencyRecord(
        provider_name="Databricks",
        charge_month=_MONTH,
        entity_type=entity_type,
        entity_id=entity_id,
        billed_cost=Decimal(cost),
        x_source_connector="databricks",
        **kw,
    )


def _by_entity_category(rows: list[dict[str, object]]) -> dict[tuple[str, str], dict[str, object]]:
    return {(str(r["entity_id"]), str(r["waste_category"])): r for r in rows}


def test_waste_classification(lake_home) -> None:  # type: ignore[no-untyped-def]
    from flashlight.gold.reader import query_view
    from flashlight.lake import metrics
    from flashlight.transform.runner import build_gold

    records = [
        # underutilized job: 10% util, consistently → high confidence, recoverable 90% of cost
        _rec("job-under", EntityType.JOB, "100", utilization_pct=10.0, activity_count=5,
             cause_detail={"pct_runs_underutilized": 0.9}),
        # idle: billed but zero activity → full cost recoverable
        _rec("job-idle", EntityType.JOB, "50", activity_count=0),
        # placement: only the job-shaped slice of the cluster's cost is recoverable —
        # the real jobs-compute-priced delta (150 - 45 = 105), not the whole cluster's
        # billed_cost
        _rec("cl-interactive", EntityType.INTERACTIVE, "200", activity_count=10,
             cause_detail={"job_shaped_cost": 150.0, "jobs_priced_cost": 45.0,
                           "top_job_name": "nightly_ingest", "top_job_owner": "alice"}),
        # underutilized interactive CLUSTER: cluster-level util is honest at cluster grain
        _rec("cl-under", EntityType.INTERACTIVE, "120", utilization_pct=15.0,
             cause_detail={"job_shaped_cost": 50.0, "jobs_priced_cost": 15.0,
                           "top_job_name": "hourly_job", "top_job_owner": "bob"}),
        # SQL warehouse: shared, no per-entity util → no underutilized AND no placement
        _rec("wh-1", EntityType.SQL_WAREHOUSE, "300"),
        # SQL warehouse hammered by redundant automated queries → low cache reuse fires
        _rec("wh-noisy", EntityType.SQL_WAREHOUSE, "400",
             cause_detail={"cache_hit_pct": 2.0, "query_count": 5000}),
        # SQL warehouse with healthy cache reuse → does not fire despite high volume
        _rec("wh-healthy", EntityType.SQL_WAREHOUSE, "400",
             cause_detail={"cache_hit_pct": 60.0, "query_count": 5000}),
        # SQL warehouse with low cache reuse but low volume → not enough traffic to be
        # a real finding (volume gate), does not fire
        _rec("wh-lowvolume", EntityType.SQL_WAREHOUSE, "50",
             cause_detail={"cache_hit_pct": 0.0, "query_count": 10}),
        # SQL warehouse: 3% of queries spill to disk (above the 2% gate) → fires, unpriced
        _rec("wh-spill", EntityType.SQL_WAREHOUSE, "400",
             cause_detail={"spill_query_count": 30, "query_count": 1000,
                           "spilled_bytes": 5_000_000_000}),
        # SQL warehouse: spill rate below the gate → does not fire
        _rec("wh-nospill", EntityType.SQL_WAREHOUSE, "400",
             cause_detail={"spill_query_count": 5, "query_count": 1000}),
        # job, long run (>=5min) + elevated CPU wait → possible_memory_pressure fires
        _rec("job-wait", EntityType.JOB, "100", cause_detail={
            "pct_time_high_cpu_wait": 0.85, "avg_run_seconds": 600.0}),
        # job, SAME elevated CPU wait but short run (<5min) → gated out, no row
        _rec("job-short", EntityType.JOB, "100", cause_detail={
            "pct_time_high_cpu_wait": 0.85, "avg_run_seconds": 120.0}),
        # job, long run but healthy metrics → no row
        _rec("job-calm", EntityType.JOB, "100", cause_detail={
            "pct_time_high_cpu_wait": 0.02, "avg_run_seconds": 600.0}),
        # job, elevated CPU wait but avg_run_seconds is NULL (untracked runs, e.g.
        # DLT-triggered compute) → fails closed, no row (can't confirm materiality)
        _rec("job-untracked", EntityType.JOB, "100", cause_detail={
            "pct_time_high_cpu_wait": 0.85}),
        # interactive cluster, low local disk free → fires with no duration gate needed
        _rec("cl-diskfree", EntityType.INTERACTIVE, "200", cause_detail={
            "min_local_disk_free_bytes": 5_000_000_000, "worker_node_type": "r5.xlarge"}),
        # job, long run + heavy network I/O → possible_heavy_shuffle fires
        _rec("job-shuffle", EntityType.JOB, "150", cause_detail={
            "network_bytes": 600_000_000_000, "avg_run_seconds": 900.0}),
        # job, network below the gate → no row
        _rec("job-lightnet", EntityType.JOB, "150", cause_detail={
            "network_bytes": 1_000_000, "avg_run_seconds": 900.0}),
        # failed runs: util healthy, but failed_cost present
        _rec("job-failed", EntityType.JOB, "80", utilization_pct=50.0, activity_count=3,
             cause_detail={"failed_cost": 20.0}),
        # photon-no-gain: photon on a low-util job → candidate, flat 2.9x DBU-premium share.
        _rec("job-photon", EntityType.JOB, "60", utilization_pct=10.0, activity_count=2,
             cause_detail={"photon": True, "pct_runs_underutilized": 0.5}),
        # S3 bucket still on Standard → OPPORTUNITY, 35% of cost
        _rec("bucket-standard", EntityType.STORAGE, "1000",
             cause_detail={"storage_class": "standard"}),
        # S3 bucket already on Intelligent-Tiering → not flagged
        _rec("bucket-it", EntityType.STORAGE, "500",
             cause_detail={"storage_class": "intelligent_tiering"}),
    ]
    assert metrics.write_efficiency(_WINDOW, records) == len(records)

    build_gold()
    rows = query_view("efficiency.waste_record")
    idx = _by_entity_category(rows)

    # underutilized: recoverable = cost × (1 − util) = 100 × 0.9, high confidence
    under = idx[("job-under", "underutilized")]
    assert under["recoverable_cost"] == pytest.approx(90.0)
    assert under["confidence"] == "high"
    assert under["lens"] == "WASTE"

    # idle: full billed cost
    assert idx[("job-idle", "idle")]["recoverable_cost"] == pytest.approx(50.0)

    # placement: the job-shaped slice re-priced at jobs-compute rates, opportunity lens,
    # candidate, detail names the specific job and its owner
    placement = idx[("cl-interactive", "placement")]
    assert placement["recoverable_cost"] == pytest.approx(105.0)
    assert placement["lens"] == "OPPORTUNITY"
    assert placement["confidence"] == "candidate"
    assert "nightly_ingest" in str(placement["detail"])
    assert "alice" in str(placement["detail"])

    # interactive CLUSTER with cluster-level util → underutilized is honest at cluster grain
    under_cl = idx[("cl-under", "underutilized")]
    assert under_cl["recoverable_cost"] == pytest.approx(120 * 0.85)
    # it is ALSO a placement candidate (different lens, different remedy) since it has
    # its own identifiable job-shaped cost
    assert idx[("cl-under", "placement")]["recoverable_cost"] == pytest.approx(35.0)

    # failed: the failed_cost from cause_detail
    assert idx[("job-failed", "failed")]["recoverable_cost"] == pytest.approx(20.0)

    # photon-no-gain: flat premium share (1 − 1/2.9) of cost; candidate (pct < 0.8)
    photon = idx[("job-photon", "photon_no_gain")]
    assert photon["recoverable_cost"] == pytest.approx(60 * (1 - 1 / 2.9), rel=1e-3)
    assert photon["confidence"] == "candidate"
    # the same low-util photon job is ALSO underutilized (additive rows)
    assert ("job-photon", "underutilized") in idx

    # S3 storage-tiering opportunity: 35% of cost, candidate confidence
    tiering = idx[("bucket-standard", "s3_intelligent_tiering")]
    assert tiering["recoverable_cost"] == pytest.approx(350.0)
    assert tiering["lens"] == "OPPORTUNITY"
    assert tiering["confidence"] == "candidate"
    # already on Intelligent-Tiering → no row
    assert ("bucket-it", "s3_intelligent_tiering") not in idx

    # SQL warehouse low-cache-reuse: 25% of cost, candidate, detail shows the numbers
    noisy = idx[("wh-noisy", "sql_warehouse_low_cache_reuse")]
    assert noisy["recoverable_cost"] == pytest.approx(100.0)
    assert noisy["lens"] == "WASTE"
    assert noisy["confidence"] == "candidate"
    assert "5000 queries" in str(noisy["detail"])
    # healthy cache reuse → no row despite the same query volume
    assert ("wh-healthy", "sql_warehouse_low_cache_reuse") not in idx
    # low query volume → no row despite a 0% cache-hit rate (volume gate)
    assert ("wh-lowvolume", "sql_warehouse_low_cache_reuse") not in idx

    # SQL warehouse disk spill: unpriced (no $/byte mechanism), candidate, detail shows
    # count + GB
    spill = idx[("wh-spill", "sql_warehouse_disk_spill")]
    assert spill["recoverable_cost"] == pytest.approx(0.0)
    assert spill["lens"] == "WASTE"
    assert spill["confidence"] == "candidate"
    assert "30 of 1000" in str(spill["detail"])
    assert "5.0 GB" in str(spill["detail"])
    # spill rate below the gate → no row
    assert ("wh-nospill", "sql_warehouse_disk_spill") not in idx

    # possible_memory_pressure: fires on elevated CPU wait, unpriced, reports the %
    pressure = idx[("job-wait", "possible_memory_pressure")]
    assert pressure["recoverable_cost"] == pytest.approx(0.0)
    assert pressure["lens"] == "WASTE"
    assert pressure["confidence"] == "candidate"
    assert "85%" in str(pressure["detail"])
    assert "10.0 min" in str(pressure["detail"])
    # same elevated wait, but run too short to matter → gated out
    assert ("job-short", "possible_memory_pressure") not in idx
    # long run, healthy metrics → no row
    assert ("job-calm", "possible_memory_pressure") not in idx
    # elevated wait but untracked run duration (NULL) → fails closed, no row
    assert ("job-untracked", "possible_memory_pressure") not in idx
    # interactive cluster: low local disk free fires with no duration gate
    disk = idx[("cl-diskfree", "possible_memory_pressure")]
    assert disk["recoverable_cost"] == pytest.approx(0.0)
    assert "5.0 GB local disk free" in str(disk["detail"])
    assert "r5.xlarge" in str(disk["detail"])

    # possible_heavy_shuffle: fires on heavy network I/O, unpriced
    shuffle = idx[("job-shuffle", "possible_heavy_shuffle")]
    assert shuffle["recoverable_cost"] == pytest.approx(0.0)
    assert shuffle["confidence"] == "candidate"
    assert "600" in str(shuffle["detail"])
    # network below the gate → no row
    assert ("job-lightnet", "possible_heavy_shuffle") not in idx

    # honesty: a healthy job emits no underutilized row
    assert ("job-failed", "underutilized") not in idx
    # no utilization data (NULL) → no underutilized row (cl-interactive has util=None)
    assert ("cl-interactive", "underutilized") not in idx
    # SQL warehouse: no per-entity util → never underutilized, and never a placement
    # candidate (you can't move a warehouse to jobs compute)
    assert not any(k[0] == "wh-1" for k in idx)


def test_cluster_config_rules(lake_home) -> None:  # type: ignore[no-untyped-def]
    """The five newly-activated cluster-config rules (missing_autotermination,
    autoscale_misconfigured, oversized_nodes, graviton_price_opportunity, on_demand_only)
    — interactive-only, driven by cause_detail keys sourced from system.compute.clusters/
    node_types. Can't validate the live Databricks SQL without a warehouse, but the
    classification logic (what waste_rules.py does with the values once ingested) is
    fully covered here."""
    from flashlight.gold.reader import query_view
    from flashlight.lake import metrics
    from flashlight.transform.runner import build_gold

    records = [
        # no auto-termination policy → fires regardless of utilization
        _rec("cl-noautoterm", EntityType.INTERACTIVE, "100", utilization_pct=90.0,
             cause_detail={}),
        # has a policy → does not fire
        _rec("cl-autoterm", EntityType.INTERACTIVE, "100", utilization_pct=90.0,
             cause_detail={"auto_termination_minutes": 30}),
        # wide autoscale range (5x) + underutilized → fires
        _rec("cl-wide-autoscale", EntityType.INTERACTIVE, "100", utilization_pct=10.0,
             cause_detail={"min_autoscale_workers": 2, "max_autoscale_workers": 10}),
        # narrow range (2x) + underutilized → does not fire
        _rec("cl-narrow-autoscale", EntityType.INTERACTIVE, "100", utilization_pct=10.0,
             cause_detail={"min_autoscale_workers": 2, "max_autoscale_workers": 4}),
        # large node (16 cores) + underutilized → fires
        _rec("cl-oversized", EntityType.INTERACTIVE, "100", utilization_pct=10.0,
             cause_detail={"worker_node_type": "i3.4xlarge", "core_count": 16.0}),
        # small node (4 cores) + underutilized → does not fire (still underutilized, though)
        _rec("cl-small", EntityType.INTERACTIVE, "100", utilization_pct=10.0,
             cause_detail={"worker_node_type": "i3.xlarge", "core_count": 4.0}),
        # non-Graviton instance family → fires regardless of utilization
        _rec("cl-stale-gen", EntityType.INTERACTIVE, "100", utilization_pct=90.0,
             cause_detail={"worker_node_type": "i3.4xlarge"}),
        # Graviton (g) instance family → does not fire
        _rec("cl-graviton", EntityType.INTERACTIVE, "100", utilization_pct=90.0,
             cause_detail={"worker_node_type": "m6g.xlarge"}),
        # 100% on-demand → fires
        _rec("cl-ondemand", EntityType.INTERACTIVE, "100", utilization_pct=90.0,
             cause_detail={"availability": "ON_DEMAND"}),
        # spot → does not fire
        _rec("cl-spot", EntityType.INTERACTIVE, "100", utilization_pct=90.0,
             cause_detail={"availability": "SPOT_WITH_FALLBACK"}),
    ]
    assert metrics.write_efficiency(_WINDOW, records) == len(records)

    build_gold()
    rows = query_view("efficiency.waste_record")
    idx = _by_entity_category(rows)

    assert ("cl-noautoterm", "missing_autotermination") in idx
    assert ("cl-autoterm", "missing_autotermination") not in idx

    assert ("cl-wide-autoscale", "autoscale_misconfigured") in idx
    assert ("cl-narrow-autoscale", "autoscale_misconfigured") not in idx

    assert ("cl-oversized", "oversized_nodes") in idx
    assert ("cl-small", "oversized_nodes") not in idx

    assert ("cl-stale-gen", "graviton_price_opportunity") in idx
    assert ("cl-graviton", "graviton_price_opportunity") not in idx

    assert ("cl-ondemand", "on_demand_only") in idx
    assert ("cl-spot", "on_demand_only") not in idx


def test_waste_resolution_tracking(lake_home) -> None:  # type: ignore[no-untyped-def]
    """Pure re-detection over two months — no applied-action log, no user input."""
    from flashlight.gold.reader import query_view
    from flashlight.lake import metrics
    from flashlight.transform.runner import build_gold

    may = date(2026, 5, 1)
    june = date(2026, 6, 1)
    window = IngestWindow(may, date(2026, 6, 30))

    def rec(month: date, entity_id: str, cost: str, **kw: object) -> EfficiencyRecord:
        return EfficiencyRecord(
            provider_name="Databricks", charge_month=month, entity_type=EntityType.JOB,
            entity_id=entity_id, billed_cost=Decimal(cost), x_source_connector="databricks",
            **kw,
        )

    records = [
        # terminated: idle in May, gone entirely in June → full recovery (100)
        rec(may, "job-terminated", "100", activity_count=0),
        # fixed: idle in May (cost 100), fixed + cheaper in June (cost 80, has activity)
        rec(may, "job-fixed", "100", activity_count=0),
        rec(june, "job-fixed", "80", activity_count=5),
        # still broken: idle in both months → not resolved
        rec(may, "job-stillidle", "60", activity_count=0),
        rec(june, "job-stillidle", "60", activity_count=0),
    ]
    assert metrics.write_efficiency(window, records) == len(records)

    build_gold()
    rows = query_view("efficiency.waste_resolution_month")
    idx = {(str(r["entity_id"]), str(r["waste_category"])): r for r in rows}

    terminated = idx[("job-terminated", "idle")]
    assert terminated["is_resolved"] is True
    assert terminated["resolved_month"] == june.isoformat()
    assert terminated["realized_savings"] == pytest.approx(100.0)

    fixed = idx[("job-fixed", "idle")]
    assert fixed["is_resolved"] is True
    assert fixed["resolved_month"] == june.isoformat()
    assert fixed["realized_savings"] == pytest.approx(20.0)

    still_idle = idx[("job-stillidle", "idle")]
    assert still_idle["is_resolved"] is False
    assert still_idle["resolved_month"] is None


def test_photon_and_job_utilization_rework(lake_home) -> None:  # type: ignore[no-untyped-def]
    """photon_on_interactive_cluster fires categorically (any utilization); photon_no_gain
    is jobs-only now with the raised <80% threshold; job_low_utilization covers the 20-60%
    band for jobs (including DLT-billed compute, which only ever gets utilization_pct, not
    activity_count — see waste_rules.py's comment on why this doesn't need activity_count)."""
    from flashlight.gold.reader import query_view
    from flashlight.lake import metrics
    from flashlight.transform.runner import build_gold

    records = [
        # interactive + photon, even at HIGH utilization → still fires (categorical)
        _rec("cl-photon-busy", EntityType.INTERACTIVE, "100", utilization_pct=90.0,
             cause_detail={"photon": True}),
        # interactive + no photon → does not fire either photon category
        _rec("cl-no-photon", EntityType.INTERACTIVE, "100", utilization_pct=90.0,
             cause_detail={"photon": False}),
        # job + photon at 70% util → fires photon_no_gain under the new <80% threshold
        # (would NOT have fired under the old <=20% threshold)
        _rec("job-photon-70", EntityType.JOB, "100", utilization_pct=70.0,
             cause_detail={"photon": True}),
        # job + photon at 85% util → healthy enough, does not fire
        _rec("job-photon-85", EntityType.JOB, "100", utilization_pct=85.0,
             cause_detail={"photon": True}),
        # job at 35% util, no photon → job_low_utilization (20-60% band)
        _rec("job-moderate", EntityType.JOB, "100", utilization_pct=35.0,
             cause_detail={"max_cpu_pct": 40.0, "max_mem_pct": 92.0}),
        # job at 15% util → the more severe `underutilized` band, NOT job_low_utilization
        _rec("job-severe", EntityType.JOB, "100", utilization_pct=15.0),
        # job at 65% util → healthy, neither band fires
        _rec("job-healthy", EntityType.JOB, "100", utilization_pct=65.0),
    ]
    assert metrics.write_efficiency(_WINDOW, records) == len(records)

    build_gold()
    rows = query_view("efficiency.waste_record")
    idx = _by_entity_category(rows)

    busy = idx[("cl-photon-busy", "photon_on_interactive_cluster")]
    assert busy["confidence"] == "candidate"  # heuristic multiplier, not a measured saving
    assert busy["recoverable_cost"] == pytest.approx(100 * (1 - 1 / 2))
    assert ("cl-photon-busy", "photon_no_gain") not in idx
    assert not any(k[0] == "cl-no-photon" and "photon" in k[1] for k in idx)

    assert ("job-photon-70", "photon_no_gain") in idx
    assert ("job-photon-85", "photon_no_gain") not in idx
    assert not any(k[0] == "job-photon-70" and k[1] == "photon_on_interactive_cluster"
                   for k in idx)

    moderate = idx[("job-moderate", "job_low_utilization")]
    assert moderate["recoverable_cost"] == pytest.approx(100 * 0.65 * 0.5)
    assert moderate["confidence"] == "candidate"
    assert "peak cpu 40%, mem 92%" in str(moderate["detail"])

    assert ("job-severe", "job_low_utilization") not in idx
    assert ("job-severe", "underutilized") in idx
    assert not any(k[0] == "job-healthy" for k in idx)


def test_table_compression_rule(lake_home) -> None:  # type: ignore[no-untyped-def]
    """`table` inventory rows (size/compression snapshot, no $ figure) flow through
    metrics → GOLD without error. snappy_to_zstd_compression fires ONLY on a confirmed
    `compression_codec=snappy` — an absent/zstd codec must NOT fire (waste-honesty: an
    unset property tells us nothing about the actual on-disk codec)."""
    from flashlight.gold.reader import query_view
    from flashlight.lake import metrics
    from flashlight.transform.runner import build_gold

    def table_rec(entity_id: str, **cause: object) -> EfficiencyRecord:
        return EfficiencyRecord(
            provider_name="Databricks",
            charge_month=_MONTH,
            entity_type=EntityType.TABLE,
            entity_id=entity_id,
            entity_name=entity_id,
            native_quantity=123456.0,
            native_unit="bytes",
            cause_detail=dict(cause),
            x_source_connector="databricks",
        )

    records = [
        table_rec("main.sales.orders_snappy", num_files=42, compression_codec="snappy"),
        table_rec("main.sales.orders_zstd", num_files=10, compression_codec="zstd"),
        table_rec("main.sales.orders_unset", num_files=5),
    ]
    assert metrics.write_efficiency(_WINDOW, records) == len(records)

    build_gold()
    rows = query_view("efficiency.waste_record")
    idx = _by_entity_category(rows)

    snappy = idx[("main.sales.orders_snappy", "snappy_to_zstd_compression")]
    assert snappy["recoverable_cost"] == pytest.approx(0.0)
    assert snappy["lens"] == "WASTE"
    assert snappy["confidence"] == "high"

    assert ("main.sales.orders_zstd", "snappy_to_zstd_compression") not in idx
    assert ("main.sales.orders_unset", "snappy_to_zstd_compression") not in idx


def test_sql_warehouse_user_and_notebook_rules(lake_home) -> None:  # type: ignore[no-untyped-def]
    """The three new SQL-warehouse-per-user signals (concentration, high-frequency
    serverless cadence, serverless-vs-classic pricing gap) and the serverless notebook
    placement rule — all OPPORTUNITY/candidate, none claim a per-workload $ figure we
    can't back with real telemetry (query.history duration share, warehouse_type)."""
    from flashlight.gold.reader import query_view
    from flashlight.lake import metrics
    from flashlight.transform.runner import build_gold

    records = [
        # one user drives 60% of this warehouse's cost → concentration fires
        _rec("wh1:alice", EntityType.SQL_WAREHOUSE_USER, "100",
             cause_detail={"duration_share_pct": 60.0, "query_count": 50}),
        # a minority share → does not fire
        _rec("wh1:bob", EntityType.SQL_WAREHOUSE_USER, "50",
             cause_detail={"duration_share_pct": 10.0, "query_count": 5}),
        # serverless + tight cadence + enough volume → high-frequency fires
        _rec("wh2:carol", EntityType.SQL_WAREHOUSE_USER, "100",
             cause_detail={"warehouse_type": "SERVERLESS", "avg_interval_minutes": 5.0,
                           "query_count": 200}),
        # same cadence, but CLASSIC warehouse → does not fire (serverless-only)
        _rec("wh2:dave", EntityType.SQL_WAREHOUSE_USER, "100",
             cause_detail={"warehouse_type": "CLASSIC", "avg_interval_minutes": 5.0,
                           "query_count": 200}),
        # serverless + tight cadence, but too few queries → volume gate, does not fire
        _rec("wh2:erin", EntityType.SQL_WAREHOUSE_USER, "100",
             cause_detail={"warehouse_type": "SERVERLESS", "avg_interval_minutes": 5.0,
                           "query_count": 5}),
        # serverless, high volume, but a relaxed (2h) cadence → does not fire
        _rec("wh2:frank", EntityType.SQL_WAREHOUSE_USER, "100",
             cause_detail={"warehouse_type": "SERVERLESS", "avg_interval_minutes": 120.0,
                           "query_count": 200}),
        # whole-warehouse serverless with sustained volume → pricing-gap candidate,
        # visibility-only (no $ figure — SKU price match not yet validated)
        _rec("wh3", EntityType.SQL_WAREHOUSE, "500",
             cause_detail={"warehouse_type": "SERVERLESS", "query_count": 2000}),
        # same volume, but CLASSIC → does not fire
        _rec("wh4", EntityType.SQL_WAREHOUSE, "500",
             cause_detail={"warehouse_type": "CLASSIC", "query_count": 2000}),
        # serverless notebook — real per-notebook cost, unconditional placement candidate;
        # jobs_priced_cost=12 means the real jobs-compute-priced delta is 40-12=28
        _rec("nb-1", EntityType.NOTEBOOK, "40", owner_user="dana",
             cause_detail={"jobs_priced_cost": 12.0}),
        # same rule, but the counterpart SKU couldn't be resolved (no jobs_priced_cost) —
        # still surfaces the finding, honestly with $0 rather than fabricating a cut
        _rec("nb-2", EntityType.NOTEBOOK, "40", owner_user="erin"),
    ]
    assert metrics.write_efficiency(_WINDOW, records) == len(records)

    build_gold()
    rows = query_view("efficiency.waste_record")
    idx = _by_entity_category(rows)

    concentrated = idx[("wh1:alice", "sql_warehouse_user_concentration")]
    assert concentrated["recoverable_cost"] == pytest.approx(20.0)
    assert concentrated["lens"] == "OPPORTUNITY"
    assert concentrated["confidence"] == "candidate"
    assert ("wh1:bob", "sql_warehouse_user_concentration") not in idx

    assert ("wh2:carol", "sql_warehouse_high_frequency_workload") in idx
    assert ("wh2:dave", "sql_warehouse_high_frequency_workload") not in idx
    assert ("wh2:erin", "sql_warehouse_high_frequency_workload") not in idx
    assert ("wh2:frank", "sql_warehouse_high_frequency_workload") not in idx

    gap = idx[("wh3", "sql_warehouse_serverless_pricing_gap")]
    assert gap["recoverable_cost"] == pytest.approx(0.0)
    assert gap["confidence"] == "candidate"
    assert ("wh4", "sql_warehouse_serverless_pricing_gap") not in idx

    notebook = idx[("nb-1", "notebook_could_move_to_jobs")]
    assert notebook["recoverable_cost"] == pytest.approx(28.0)
    assert notebook["lens"] == "OPPORTUNITY"
    assert "dana" in str(notebook["detail"])

    # unresolved counterpart SKU → finding still surfaces, honestly priced at $0
    unresolved = idx[("nb-2", "notebook_could_move_to_jobs")]
    assert unresolved["recoverable_cost"] == pytest.approx(0.0)
    assert "erin" in str(unresolved["detail"])


def test_redshift_waste_rules(lake_home) -> None:  # type: ignore[no-untyped-def]
    """The Redshift-sourced rules (redshift.py's fetch_efficiency), reusing entity_type
    sql_warehouse/table — gated purely on cause_detail keys only that connector
    populates, so they can never collide with a Databricks row of the same entity_type.
    """
    from flashlight.gold.reader import query_view
    from flashlight.lake import metrics
    from flashlight.transform.runner import build_gold

    def _aws_rec(
        entity_id: str, entity_type: EntityType, cost: str, **kw: object
    ) -> EfficiencyRecord:
        return EfficiencyRecord(
            provider_name="AWS",
            charge_month=_MONTH,
            entity_type=entity_type,
            entity_id=entity_id,
            billed_cost=Decimal(cost),
            x_source_connector="redshift",
            **kw,
        )

    records = [
        # concurrency scaling is 30% of compute+scaling spend → fires (deck's own finding)
        _aws_rec("cl-scaling", EntityType.SQL_WAREHOUSE, "700", activity_count=100,
                 cause_detail={"compute_cost": 490.0, "concurrency_scaling_cost": 210.0}),
        # concurrency scaling is a negligible share → does not fire
        _aws_rec("cl-healthy-scaling", EntityType.SQL_WAREHOUSE, "500", activity_count=100,
                 cause_detail={"compute_cost": 490.0, "concurrency_scaling_cost": 10.0}),
        # on-demand node-hours present alongside reserved coverage → fires
        _aws_rec("cl-ondemand", EntityType.SQL_WAREHOUSE, "300", activity_count=50,
                 cause_detail={"compute_cost": 300.0, "on_demand_node_hours": 720.0,
                               "reserved_node_hours": 1440.0}),
        # fully reserved, no on-demand hours → does not fire
        _aws_rec("cl-fully-reserved", EntityType.SQL_WAREHOUSE, "300", activity_count=50,
                 cause_detail={"compute_cost": 300.0, "on_demand_node_hours": 0.0,
                               "reserved_node_hours": 2160.0}),
        # real Spectrum scan spend → fires
        _aws_rec("cl-spectrum", EntityType.SQL_WAREHOUSE, "120", activity_count=20,
                 cause_detail={"spectrum_scan_cost": 120.0}),
        # 3% of queries spill to disk (above the 2% gate) → fires, unpriced
        _aws_rec("cl-spill", EntityType.SQL_WAREHOUSE, "400", activity_count=1000,
                 cause_detail={"disk_spill_query_count": 30, "query_count": 1000}),
        # spill rate below the gate → does not fire
        _aws_rec("cl-nospill", EntityType.SQL_WAREHOUSE, "400", activity_count=1000,
                 cause_detail={"disk_spill_query_count": 5, "query_count": 1000}),
        # sustained WLM queue wait (p95 6s) → fires, unpriced
        _aws_rec("cl-queued", EntityType.SQL_WAREHOUSE, "250", activity_count=200,
                 cause_detail={"wlm_queue_wait_ms_p95": 6000.0}),
        # low queue wait → does not fire
        _aws_rec("cl-noqueue", EntityType.SQL_WAREHOUSE, "250", activity_count=200,
                 cause_detail={"wlm_queue_wait_ms_p95": 200.0}),
        # zero activity → the generic `idle` rule fires for free (no new category)
        _aws_rec("cl-idle", EntityType.SQL_WAREHOUSE, "150", activity_count=0),
        # Idle, but only measured from partway through the window (STL_QUERY's
        # retention didn't reach back to the window's start) — the caveat belongs in
        # the detail text so "idle" doesn't imply full-billing-period coverage.
        _aws_rec("cl-idle-partial", EntityType.SQL_WAREHOUSE, "150", activity_count=0,
                 cause_detail={"activity_measured_since": "2026-01-20"}),
        # table with encoding disabled → confirmed fact, unpriced
        _aws_rec("tbl-unencoded", EntityType.TABLE, "0",
                 cause_detail={"encoded": "N", "tbl_rows": 1_000_000}),
        # table with encoding enabled → does not fire
        _aws_rec("tbl-encoded", EntityType.TABLE, "0",
                 cause_detail={"encoded": "Y", "tbl_rows": 1_000_000}),
        # table badly unsorted → VACUUM/ANALYZE due
        _aws_rec("tbl-unsorted", EntityType.TABLE, "0",
                 cause_detail={"unsorted_pct": 40.0, "stats_off_pct": 5.0}),
        # table well-maintained → does not fire
        _aws_rec("tbl-maintained", EntityType.TABLE, "0",
                 cause_detail={"unsorted_pct": 2.0, "stats_off_pct": 1.0}),
    ]
    assert metrics.write_efficiency(_WINDOW, records) == len(records)

    build_gold()
    rows = query_view("efficiency.waste_record")
    idx = _by_entity_category(rows)

    scaling = idx[("cl-scaling", "redshift_concurrency_scaling_overage")]
    assert scaling["recoverable_cost"] == pytest.approx(210.0)
    assert scaling["lens"] == "OPPORTUNITY"
    assert ("cl-healthy-scaling", "redshift_concurrency_scaling_overage") not in idx

    ri_gap = idx[("cl-ondemand", "redshift_ri_coverage_gap")]
    # 300 * (720 / (720+1440)) * 0.5 = 50.0
    assert ri_gap["recoverable_cost"] == pytest.approx(50.0)
    assert ("cl-fully-reserved", "redshift_ri_coverage_gap") not in idx

    spectrum = idx[("cl-spectrum", "redshift_spectrum_scan_cost")]
    assert spectrum["recoverable_cost"] == pytest.approx(36.0)  # 120 * 0.3

    spill = idx[("cl-spill", "redshift_disk_spill_queries")]
    assert spill["recoverable_cost"] == pytest.approx(0.0)
    assert "30 of 1000" in str(spill["detail"])
    assert ("cl-nospill", "redshift_disk_spill_queries") not in idx

    assert ("cl-queued", "redshift_wlm_queue_wait") in idx
    assert ("cl-noqueue", "redshift_wlm_queue_wait") not in idx

    # idle Redshift cluster reuses the generic idle category, not a new one
    assert idx[("cl-idle", "idle")]["recoverable_cost"] == pytest.approx(150.0)
    assert "measured since" not in str(idx[("cl-idle", "idle")]["detail"])

    partial = idx[("cl-idle-partial", "idle")]
    assert partial["recoverable_cost"] == pytest.approx(150.0)  # still real, just partial
    assert "measured since 2026-01-20, not the full billing period" in str(partial["detail"])

    assert idx[("tbl-unencoded", "redshift_stale_compression_encoding")]["confidence"] == "high"
    assert ("tbl-encoded", "redshift_stale_compression_encoding") not in idx

    assert ("tbl-unsorted", "redshift_table_maintenance_stale") in idx
    assert ("tbl-maintained", "redshift_table_maintenance_stale") not in idx


def test_redshift_query_pattern_and_table_usage_rules(lake_home) -> None:  # type: ignore[no-untyped-def]
    """New Redshift signals: query_pattern (repeated query shape) spill/skew, and
    table usage staleness (days_since_last_access) — distinct from the maintenance
    staleness (unsorted_pct/stats_off_pct) covered above.
    """
    from flashlight.gold.reader import query_view
    from flashlight.lake import metrics
    from flashlight.transform.runner import build_gold

    def _aws_rec(
        entity_id: str, entity_type: EntityType, cost: str, **kw: object
    ) -> EfficiencyRecord:
        return EfficiencyRecord(
            provider_name="AWS",
            charge_month=_MONTH,
            entity_type=entity_type,
            entity_id=entity_id,
            billed_cost=Decimal(cost),
            x_source_connector="redshift",
            **kw,
        )

    records = [
        # spills on 60% of 5 runs (above the 50% gate) → fires
        _aws_rec("cl:abc123", EntityType.QUERY_PATTERN, "0", activity_count=5,
                 cause_detail={"run_count": 5, "pct_runs_spilling": 0.6,
                               "avg_disk_spill_gb": 2.0}),
        # ran once — gated out even at 100% spill rate (a single run isn't a "pattern")
        _aws_rec("cl:onceonly", EntityType.QUERY_PATTERN, "0", activity_count=1,
                 cause_detail={"run_count": 1, "pct_runs_spilling": 1.0,
                               "avg_disk_spill_gb": 5.0}),
        # spills rarely → does not fire
        _aws_rec("cl:healthy", EntityType.QUERY_PATTERN, "0", activity_count=10,
                 cause_detail={"run_count": 10, "pct_runs_spilling": 0.1,
                               "avg_disk_spill_gb": 0.1}),
        # high skew across slices → fires
        _aws_rec("cl:skewed", EntityType.QUERY_PATTERN, "0", activity_count=4,
                 cause_detail={"run_count": 4, "avg_skew_ratio": 3.5, "max_skew_ratio": 5.0}),
        # low skew → does not fire
        _aws_rec("cl:balanced", EntityType.QUERY_PATTERN, "0", activity_count=4,
                 cause_detail={"run_count": 4, "avg_skew_ratio": 0.5, "max_skew_ratio": 1.0}),
        # not queried in 120 days → fires, priced off size (candidate estimate)
        _aws_rec("tbl-unused", EntityType.TABLE, "0", native_quantity=10240.0,
                 native_unit="MB", cause_detail={"days_since_last_access": 120}),
        # queried 10 days ago → does not fire
        _aws_rec("tbl-active", EntityType.TABLE, "0", native_quantity=10240.0,
                 native_unit="MB", cause_detail={"days_since_last_access": 10}),
    ]
    assert metrics.write_efficiency(_WINDOW, records) == len(records)

    build_gold()
    rows = query_view("efficiency.waste_record")
    idx = _by_entity_category(rows)

    assert ("cl:abc123", "redshift_query_pattern_high_spill") in idx
    high_spill = idx[("cl:abc123", "redshift_query_pattern_high_spill")]
    assert high_spill["recoverable_cost"] == pytest.approx(0.0)
    assert ("cl:onceonly", "redshift_query_pattern_high_spill") not in idx
    assert ("cl:healthy", "redshift_query_pattern_high_spill") not in idx

    assert ("cl:skewed", "redshift_query_pattern_skew") in idx
    assert ("cl:balanced", "redshift_query_pattern_skew") not in idx

    unused = idx[("tbl-unused", "redshift_table_unused")]
    # 10240 MB = 10 GB * 0.024 $/GB-month = 0.24
    assert unused["recoverable_cost"] == pytest.approx(0.24)
    assert unused["confidence"] == "candidate"
    assert ("tbl-active", "redshift_table_unused") not in idx


def test_redshift_spectrum_table_scan_rule(lake_home) -> None:  # type: ignore[no-untyped-def]
    """Which external table is driving Spectrum scan cost — a drill-down under the
    cluster-level redshift_spectrum_scan_cost $ figure, deliberately unpriced (that
    figure already carries the real $; pricing this too would double-count it).
    """
    from flashlight.gold.reader import query_view
    from flashlight.lake import metrics
    from flashlight.transform.runner import build_gold

    records = [
        # 42 GB scanned, only ~24% returned — an un-pruned scan → fires
        EfficiencyRecord(
            provider_name="AWS", charge_month=_MONTH, entity_type=EntityType.TABLE,
            entity_id="cl:spectrum:spectrumdb.events", x_source_connector="redshift",
            cause_detail={
                "spectrum_scan_count": 12, "spectrum_scanned_gb": 42.0,
                "spectrum_returned_gb": 10.0,
            },
        ),
        # under the 1 GB materiality floor → does not fire
        EfficiencyRecord(
            provider_name="AWS", charge_month=_MONTH, entity_type=EntityType.TABLE,
            entity_id="cl:spectrum:spectrumdb.tiny_lookup", x_source_connector="redshift",
            cause_detail={
                "spectrum_scan_count": 3, "spectrum_scanned_gb": 0.2,
                "spectrum_returned_gb": 0.2,
            },
        ),
    ]
    assert metrics.write_efficiency(_WINDOW, records) == len(records)

    build_gold()
    rows = query_view("efficiency.waste_record")
    idx = _by_entity_category(rows)

    fired = idx[("cl:spectrum:spectrumdb.events", "redshift_spectrum_table_scan")]
    assert fired["recoverable_cost"] == pytest.approx(0.0)  # unpriced by design
    assert fired["lens"] == "OPPORTUNITY"
    assert "42.0 GB scanned" in str(fired["detail"])
    assert "24% returned" in str(fired["detail"])
    assert "12 queries" in str(fired["detail"])
    assert ("cl:spectrum:spectrumdb.tiny_lookup", "redshift_spectrum_table_scan") not in idx


def test_summary_rolls_up(lake_home) -> None:  # type: ignore[no-untyped-def]
    from flashlight.gold.reader import query_view
    from flashlight.lake import metrics
    from flashlight.transform.runner import build_gold

    metrics.write_efficiency(
        _WINDOW,
        [
            _rec("a", EntityType.JOB, "100", activity_count=0),  # idle 100
            # auto_termination_minutes set so this fixture stays scoped to placement
            # only — missing_autotermination has its own dedicated test above.
            _rec("b", EntityType.INTERACTIVE, "200", activity_count=5,  # placement 140
                 cause_detail={"auto_termination_minutes": 30, "job_shaped_cost": 200.0,
                               "jobs_priced_cost": 60.0, "top_job_name": "batch_job",
                               "top_job_owner": "carol"}),
        ],
    )
    build_gold()
    summary = {str(r["lens"]): r for r in query_view("efficiency.waste_summary_month")}
    assert summary["WASTE"]["recoverable_cost"] == pytest.approx(100.0)
    assert summary["OPPORTUNITY"]["recoverable_cost"] == pytest.approx(140.0)


def test_endpoint_waste_classification(lake_home) -> None:  # type: ignore[no-untyped-def]
    """Model Serving endpoints join the existing waste plane as ROWS, not a new view.

    The point of that choice is that `idle` and `failed` — both entity-type-agnostic — fire on
    an endpoint with no rule of their own. Every branch gets a fires case AND a does-not-fire
    case, because the does-not-fire ones are where a fabricated finding would appear.
    """
    from flashlight.gold.reader import query_view
    from flashlight.lake import metrics
    from flashlight.transform.runner import build_gold

    def _ep(entity_id: str, cost: str, **cause: object) -> EfficiencyRecord:
        activity = cause.pop("activity_count", None)
        return _rec(
            entity_id,
            EntityType.ENDPOINT,
            cost,
            activity_count=activity,
            native_unit="DBU",
            cause_detail=cause,
        )

    records = [
        # idle: a MEASURED zero request count on a billed endpoint → the existing `idle` rule,
        # full cost, high confidence. No endpoint-specific rule involved.
        _ep("ep-idle", "400", activity_count=0, serving_mode="provisioned_throughput"),
        # ...but an UNMEASURED endpoint (activity_count None) must never be called idle.
        _ep("ep-unmeasured", "400", serving_mode="provisioned_throughput"),
        # failed: token-metered, so failed_cost is a real allocation → the existing `failed`.
        _ep(
            "ep-failed",
            "300",
            activity_count=100,
            serving_mode="pay_per_token",
            failed_cost=30.0,
        ),
        # scale-to-zero off AND low traffic → candidate, unpriced.
        _ep(
            "ep-always-on",
            "200",
            activity_count=5,
            serving_mode="provisioned_compute",
            scale_to_zero_enabled=False,
            request_count=5,
        ),
        # scale-to-zero off but BUSY → must not fire (the traffic justifies always-on).
        _ep(
            "ep-busy",
            "200",
            activity_count=9_000,
            serving_mode="provisioned_compute",
            scale_to_zero_enabled=False,
            request_count=9_000,
        ),
        # scale_to_zero_enabled unmeasured (NULL) on low traffic → must not fire; firing here
        # would invent a config finding from config we never read.
        _ep("ep-config-unknown", "200", activity_count=5, request_count=5),
        # GPU class on low traffic → candidate opportunity, unpriced.
        _ep(
            "ep-gpu-quiet",
            "900",
            activity_count=10,
            serving_mode="provisioned_compute",
            workload_type="GPU_LARGE",
            request_count=10,
        ),
        # GPU class but busy → must not fire.
        _ep(
            "ep-gpu-busy",
            "900",
            activity_count=50_000,
            serving_mode="provisioned_compute",
            workload_type="GPU_LARGE",
            request_count=50_000,
        ),
    ]
    assert metrics.write_efficiency(_WINDOW, records) == len(records)
    build_gold()
    rows = _by_entity_category(query_view("efficiency.waste_record"))

    # The reuse payoff: no new rule, yet both fire on an endpoint.
    idle = rows[("ep-idle", "idle")]
    assert idle["recoverable_cost"] == pytest.approx(400.0)
    assert idle["confidence"] == "high"
    assert idle["entity_type"] == "endpoint"
    assert ("ep-unmeasured", "idle") not in rows, "NULL activity is silence, not idleness"

    assert rows[("ep-failed", "failed")]["recoverable_cost"] == pytest.approx(30.0)

    # Endpoint-specific rules: real but unpriced, always candidate.
    always_on = rows[("ep-always-on", "endpoint_scale_to_zero_disabled")]
    assert always_on["confidence"] == "candidate"
    assert always_on["recoverable_cost"] == pytest.approx(0.0)
    assert always_on["lens"] == "WASTE"
    assert ("ep-busy", "endpoint_scale_to_zero_disabled") not in rows
    assert ("ep-config-unknown", "endpoint_scale_to_zero_disabled") not in rows

    gpu = rows[("ep-gpu-quiet", "endpoint_oversized_workload")]
    assert gpu["confidence"] == "candidate"
    assert gpu["recoverable_cost"] == pytest.approx(0.0)
    assert gpu["lens"] == "OPPORTUNITY"
    assert ("ep-gpu-busy", "endpoint_oversized_workload") not in rows

    # An endpoint has no CPU%, so it is not_applicable — not "we failed to measure it".
    util = {
        r["entity_id"]: r
        for r in query_view("efficiency.utilization_entity_month")
        if r["entity_type"] == "endpoint"
    }
    assert util["ep-idle"]["measurement_status"] == "not_applicable"
    assert util["ep-idle"]["utilization_pct"] is None
    # underutilized gates on utilization_pct IS NOT NULL, so it can never fire here.
    assert not [k for k in rows if k[1] == "underutilized" and k[0].startswith("ep-")]


def test_endpoint_tagging_policy(lake_home) -> None:  # type: ignore[no-untyped-def]
    """The tagging check that makes per-project AI attribution possible.

    Unmeasured (NULL) must read as not_applicable and a measured-empty tag map as
    non_compliant — collapsing the two would either invent a violation or hide one.
    """
    from flashlight.gold.reader import query_view
    from flashlight.lake import metrics
    from flashlight.transform.runner import build_gold

    records = [
        _rec("ep-tagged", EntityType.ENDPOINT, "100", cause_detail={"tag_count": 2}),
        _rec("ep-untagged", EntityType.ENDPOINT, "100", cause_detail={"tag_count": 0}),
        _rec("ep-unknown", EntityType.ENDPOINT, "100", cause_detail={}),
    ]
    metrics.write_efficiency(_WINDOW, records)
    build_gold()
    rows = {
        str(r["entity_id"]): r
        for r in query_view("policy.policy_record")
        if r["policy_category"] == "endpoint_tagging"
    }
    assert rows["ep-tagged"]["status"] == "compliant"
    assert rows["ep-untagged"]["status"] == "non_compliant"
    assert rows["ep-unknown"]["status"] == "not_applicable"
    assert "2 tag(s) set" in str(rows["ep-tagged"]["detail"])
