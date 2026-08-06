"""The deterministic waste/optimization rule pool.

Config-driven: add a ``WasteRule`` with ``where_sql`` set and the next
``flashlight transform`` classifies it into ``gold.waste_record`` — no other code
change. Every rule compiles to plain DuckDB SQL (see :func:`build_waste_record_sql`),
so a rule always classifies the same input the same way regardless of whether the
result is read from the dashboard or MCP — no LLM/skill judgment in the loop.

Rules with ``where_sql=None`` are BLOCKED — real Databricks cost-optimization
patterns (source-attributed below) that need telemetry this connector doesn't pull
yet. They stay in the pool (visible via the MCP ``list_optimization_rules`` tool) so
the roadmap is honest and lives in one place, but they contribute no rows until a
connector supplies the ``requires`` signal — inventing a detection rule against data
we don't have would violate the waste-honesty invariant (never classify off a signal
we don't actually measure).

Some BLOCKED entries are adapted from OptimNow's cloud-finops-skills
(https://github.com/OptimNow/cloud-finops-skills, CC BY-SA 4.0) — attributed via
``source`` on each rule; any derived text here inherits that license.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from flashlight.efficiency.policy_config import threshold_values


@dataclass(frozen=True)
class WasteRule:
    """One row of the waste/optimization pool. ACTIVE iff ``where_sql`` is set.

    ``detail_sql``/``recoverable_cost_sql``/``confidence_sql``/``where_sql`` are
    DuckDB expressions evaluated over the ``e`` CTE in
    :func:`build_waste_record_sql` (columns: the EfficiencyRecord fields plus the
    unpacked ``cause_detail`` keys ``failed_cost``, ``pct_runs_underutilized``,
    ``photon``, ``min_autoscale_workers``, ``max_autoscale_workers``,
    ``auto_termination_minutes``, ``worker_node_type``, ``core_count``,
    ``availability``, ``job_shaped_cost``, ``top_job_name``, ``top_job_owner`` — all
    nine populated for ``interactive`` entities only — plus ``storage_class``
    (``storage`` entities only), ``compression_codec``/``num_files`` (``table``
    entities only), ``max_cpu_pct``/``max_mem_pct`` (``job``/``interactive``
    entities only — peak alongside the avg-based ``utilization_pct``), and
    ``cache_hit_pct``/``query_count`` (``sql_warehouse`` only — query-pattern health
    in place of the per-entity ``utilization_pct`` shared compute doesn't have),
    ``spill_query_count``/``spilled_bytes`` (``sql_warehouse`` only, from
    ``system.query.history.spilled_local_bytes`` — see 'sql_warehouse_disk_spill') and
    ``shuffle_bytes`` (``sql_warehouse`` only, visibility-only — no rule reads it; shuffle
    is a normal consequence of joins/aggregations, not itself a waste signal),
    ``pct_time_high_cpu_wait``/``pct_time_high_mem_swap``/``min_local_disk_free_bytes``/
    ``network_bytes`` (``job``/``interactive`` — proxy signals for spill/shuffle on
    compute classes with no direct measurement, from ``system.compute.node_timeline``;
    see 'possible_memory_pressure'/'possible_heavy_shuffle') and ``avg_run_seconds``
    (``job`` only, from ``system.lakeflow.job_run_timeline`` — a materiality gate on
    those proxies, not a signal itself), and
    ``warehouse_type``/``avg_interval_minutes``/``duration_share_pct`` (``sql_warehouse``/
    ``sql_warehouse_user`` — real CLASSIC/PRO/SERVERLESS fact, per-user query cadence, and
    that user's share of the warehouse's monthly cost), and ``jobs_priced_cost``
    (``interactive``/``notebook`` — the same usage_quantity re-priced at the jobs-compute
    counterpart SKU's real rate, for a real $ delta instead of a fixed percentage; see
    'placement', 'notebook_could_move_to_jobs'). Photon has no SKU-price counterpart to
    re-price against — Databricks confirms the Photon/non-Photon SKU pair for the same
    compute tier charges the identical $/DBU rate, so the premium isn't a price
    difference; 'photon_no_gain'/'photon_on_interactive_cluster' below price it as a flat
    multiplier of billed_cost instead (see their recoverable_cost_sql).
    Also: ``compute_cost``/``concurrency_scaling_cost``/``storage_cost``/
    ``spectrum_scan_cost`` (``sql_warehouse`` entities from the Redshift connector —
    a real $ breakdown by cost subcategory, via Cost Explorer), ``wlm_queue_wait_ms_p95``/
    ``disk_spill_query_count``/``concurrency_scaling_active_seconds``/
    ``on_demand_node_hours``/``reserved_node_hours`` (Redshift ``sql_warehouse`` only —
    see redshift.py), and ``diststyle`` (Redshift ``table`` entities only — unsorted_pct/
    stats_off_pct/tbl_rows are also Redshift-``table``-only, reusing the same column
    names Databricks' ``table`` rows don't populate). Also: ``wlm_queue_wait_ms_p99``
    (Redshift ``sql_warehouse`` — rides along in ``redshift_wlm_queue_wait``'s detail
    text, not a separate category), ``run_count``/``pct_runs_spilling``/
    ``avg_disk_spill_gb``/``avg_skew_ratio``/``max_skew_ratio`` (Redshift
    ``query_pattern`` only — a repeated query shape's run distribution for the month;
    ``query_pattern`` reuses no Databricks fields, see redshift_query_pattern_metrics.sql),
    and ``days_since_last_access`` (Redshift ``table`` only — usage staleness, a
    different signal from the maintenance-staleness ``unsorted_pct``/``stats_off_pct``
    above), and ``spectrum_scan_count``/``spectrum_scanned_gb``/``spectrum_returned_gb``
    (Redshift ``table`` only, from ``redshift._fetch_spectrum_table_usage`` — an
    external table's own Spectrum scan volume, a different row from the internal-storage
    fields above; the same table row never populates both). Redshift ``sql_warehouse_user``
    rows populate the same
    ``duration_share_pct``/``query_count``/``warehouse_type`` keys Databricks does
    (see redshift_user_activity.sql), so ``sql_warehouse_user_concentration`` above
    classifies both providers with no Redshift-specific rule needed.
    """

    category: str
    lens: str  # "WASTE" | "OPPORTUNITY"
    label: str
    remedy: str
    confidence_sql: str = "'candidate'"
    detail_sql: str = "''"
    recoverable_cost_sql: str = "round(billed_cost, 2)"
    where_sql: str | None = None  # None = blocked, not yet evaluable
    requires: tuple[str, ...] = ()  # telemetry needed to activate a blocked rule
    source: str = "flashlight"

    # ── Coverage metadata: which scopes this rule can even be evaluated in ──────────
    # NOT used to build the classification SQL — `where_sql` remains the single source of
    # truth for what fires. These declare, for a *reader*, the scope in which "this rule
    # found nothing" is a meaningful statement. A coverage table that lists a rule outside
    # its scope reports "clean" for a check that never ran, which is a worse lie than
    # omitting it: see `coverage_groups`.
    #
    # `providers` is the FOCUS provider_name values whose connectors populate the fields
    # `where_sql` tests; () means any provider that supplies them. Getting this wrong is
    # the dangerous direction — leaving a Databricks-only rule at () puts a false "clean"
    # on every Redshift cluster.
    providers: tuple[str, ...] = ()
    # `entity_types` is the EntityType values `where_sql` restricts to; () means any (or,
    # for `photon_no_gain`, a *negative* restriction that a tuple can't express).
    entity_types: tuple[str, ...] = ()


_RULES_RAW: tuple[WasteRule, ...] = (
    # ── Active: evaluated today from metrics.efficiency_record ─────────────────────
    WasteRule(
        category="underutilized",
        lens="WASTE",
        label="Underutilized capacity",
        remedy="Right-size to a smaller instance/cluster or consolidate workloads onto "
        "shared compute.",
        where_sql="utilization_pct IS NOT NULL AND utilization_pct <= {underutilized_pct}",
        detail_sql="'util ' || round(utilization_pct)::INT || '%'",
        recoverable_cost_sql="round(billed_cost * (1 - utilization_pct / 100.0), 2)",
        confidence_sql="CASE WHEN coalesce(pct_runs_underutilized, 0) >= 0.8 "
        "THEN 'high' ELSE 'candidate' END",
    ),
    WasteRule(
        category="idle",
        lens="WASTE",
        label="Idle (no measured activity)",
        remedy="Terminate, or add an auto-stop/auto-termination policy so idle time "
        "stops billing.",
        where_sql="activity_count = 0 AND billed_cost > 0",
        # activity_measured_since is Redshift-only (NULL/absent for every other
        # provider's idle row) — coalesce keeps this a no-op everywhere else.
        detail_sql="'no measured activity'"
        "|| coalesce(' (measured since ' || activity_measured_since || ', not the full '"
        "|| 'billing period)', '')",
        recoverable_cost_sql="round(billed_cost, 2)",
        confidence_sql="'high'",
    ),
    WasteRule(
        category="job_low_utilization",
        lens="WASTE",
        label="Job running below healthy utilization",
        remedy="Investigate CPU/memory usage across executors — right-size the cluster "
        "or the job's parallelism so it consistently runs above ~60% utilization. A "
        "healthy-looking average can still hide one hot executor (skew) or memory "
        "pressure — check max_cpu_pct/max_mem_pct in the detail before assuming this "
        "job is fine as-is.",
        # `idle` (activity_count=0) can't honestly classify jobs — a billed job with zero
        # recorded runs is indistinguishable from one whose runs simply aren't joined
        # (e.g. DLT-pipeline-triggered compute, billed as JOBS but not tracked in
        # system.lakeflow.job_run_timeline). utilization_pct sidesteps that entirely: it
        # comes from the node_timeline join (keyed by cluster_id), which DLT-billed
        # compute already has, so this covers DLT for free without a new/riskier join.
        # 20-60% band: below 20 is the more severe `underutilized` category above.
        where_sql="entity_type = 'job' AND utilization_pct IS NOT NULL "
        "AND utilization_pct > 20 AND utilization_pct < 60",
        detail_sql="'util ' || round(utilization_pct)::INT || '%'"
        "|| CASE WHEN max_mem_pct IS NOT NULL OR max_cpu_pct IS NOT NULL "
        "THEN ' (peak cpu ' || coalesce(round(max_cpu_pct)::INT, 0) || '%, mem ' "
        "|| coalesce(round(max_mem_pct)::INT, 0) || '%)' ELSE '' END",
        # ponytail: half-credit vs the severe `underutilized` formula — this is a softer,
        # broader-net band, never 'high' confidence.
        recoverable_cost_sql="round(billed_cost * (1 - utilization_pct / 100.0) * 0.5, 2)",
        confidence_sql="'candidate'",
    ),
    # ── Active: JOBS/ALL_PURPOSE spill/shuffle proxy signals (system.compute.node_timeline) ──
    # No direct spill/shuffle metric exists for these compute classes (see "Known
    # limitations" in docs/design/efficiency-waste.md) — these approximate it from what
    # node_timeline DOES carry. Both unpriced (recoverable_cost_sql="0") — these are
    # flags for review, not measured savings; see the module docstring for the exact
    # thresholds' live-data grounding. The job-only duration gate (avg_run_seconds >=
    # 300) is a materiality filter, not a signal: a job with an elevated proxy reading
    # that only ever runs 90 seconds has no meaningful optimization payoff, and NULL
    # avg_run_seconds (job runs not tracked in job_run_timeline — see
    # job_low_utilization's comment above for why that happens) fails closed rather than
    # guessing. interactive clusters have no per-run duration concept, so the gate is
    # skipped for them (billed_cost already reflects genuine usage volume).
    WasteRule(
        category="possible_memory_pressure",
        lens="WASTE",
        label="Possible memory pressure (spill proxy)",
        remedy="No direct spill telemetry exists for job/all-purpose compute — this "
        "combines three indirect node_timeline signals (CPU wait, memory swap, local "
        "disk headroom). Investigate memory allocation, partition sizing, and join/"
        "aggregation shape for this workload; an undersized cluster or a skewed "
        "operation are the usual causes.",
        where_sql="entity_type IN ('job', 'interactive') "
        "AND (entity_type != 'job' OR (avg_run_seconds IS NOT NULL AND avg_run_seconds >= 300)) "
        "AND (coalesce(pct_time_high_cpu_wait, 0) >= 0.1 "
        "OR coalesce(pct_time_high_mem_swap, 0) >= 0.1 "
        "OR (min_local_disk_free_bytes IS NOT NULL AND min_local_disk_free_bytes < 10000000000))",
        # Reports whichever signal is strongest, in order of directness (CPU wait is the
        # most literal "spilling to disk right now" tell; swap is memory exhaustion;
        # low local-disk headroom is spill running out of room) rather than concatenating
        # all three — simpler and avoids ambiguity about degree when more than one fires.
        detail_sql="CASE "
        "WHEN coalesce(pct_time_high_cpu_wait, 0) >= 0.1 "
        "THEN round(pct_time_high_cpu_wait * 100)::INT || '% time with elevated CPU wait' "
        "WHEN coalesce(pct_time_high_mem_swap, 0) >= 0.1 "
        "THEN round(pct_time_high_mem_swap * 100)::INT || '% of sampled time swapping' "
        "ELSE round(min_local_disk_free_bytes / 1e9, 1) || ' GB local disk free (low)' END "
        "|| coalesce(' — ' || worker_node_type, '') "
        "|| CASE WHEN entity_type = 'job' AND avg_run_seconds IS NOT NULL "
        "THEN ', avg run ' || round(avg_run_seconds / 60.0, 1) || ' min' ELSE '' END",
        recoverable_cost_sql="0",
        confidence_sql="'candidate'",
    ),
    WasteRule(
        category="possible_heavy_shuffle",
        lens="WASTE",
        label="Possible heavy shuffle (network proxy)",
        remedy="No direct shuffle telemetry exists for job/all-purpose compute — "
        "network I/O is a coarse proxy (shuffle is fundamentally inter-executor network "
        "traffic). Review partition count/skew and join strategy for this workload.",
        where_sql="entity_type IN ('job', 'interactive') "
        "AND (entity_type != 'job' OR (avg_run_seconds IS NOT NULL AND avg_run_seconds >= 300)) "
        "AND network_bytes IS NOT NULL AND network_bytes >= 500000000000",
        detail_sql="round(network_bytes / 1e9) || ' GB network I/O this month'"
        "|| coalesce(' — ' || worker_node_type, '') "
        "|| CASE WHEN entity_type = 'job' AND avg_run_seconds IS NOT NULL "
        "THEN ', avg run ' || round(avg_run_seconds / 60.0, 1) || ' min' ELSE '' END",
        recoverable_cost_sql="0",
        confidence_sql="'candidate'",
    ),
    WasteRule(
        category="placement",
        lens="OPPORTUNITY",
        label="Wrong compute placement",
        remedy="Migrate the named job from this all-purpose cluster to jobs compute for "
        "a lower unit rate — confirm with its owner before moving it.",
        # Only fires when job_shaped_cost identifies actual job-triggered usage on this
        # cluster (usage_metadata.job_id populated on an ALL_PURPOSE-billed row) — not on
        # every interactive cluster regardless of what ran on it. recoverable_cost re-prices
        # THAT job's usage_quantity at the real jobs-compute SKU rate (jobs_priced_cost, from
        # the same list_prices table billed_cost already uses) — a real $ delta that tracks
        # Databricks' current pricing, not a hand-picked percentage. Falls back to 0 (no
        # claimed saving) if the counterpart SKU can't be resolved — see
        # databricks_efficiency.sql's header comment.
        where_sql="entity_type = 'interactive' AND coalesce(job_shaped_cost, 0) > 0",
        detail_sql="coalesce(top_job_name, 'a job') || ', owner ' "
        "|| coalesce(top_job_owner, 'unknown') || ' → jobs compute'",
        recoverable_cost_sql="round(greatest(job_shaped_cost "
        "- coalesce(jobs_priced_cost, job_shaped_cost), 0), 2)",
        confidence_sql="'candidate'",
    ),
    WasteRule(
        category="failed",
        lens="WASTE",
        label="Failed / retried runs",
        remedy="Investigate the error/timeout causing retries; fixing the root cause "
        "removes the retry cost.",
        where_sql="coalesce(failed_cost, 0) > 0",
        detail_sql="'→ fix errors, reduce retries'",
        recoverable_cost_sql="round(failed_cost, 2)",
        confidence_sql="'high'",
    ),
    WasteRule(
        category="photon_no_gain",
        lens="WASTE",
        label="Photon, no utilization gain",
        remedy="Disable Photon here — the utilization isn't high enough for its premium "
        "to pay off.",
        # Jobs only — interactive clusters are covered unconditionally by
        # photon_on_interactive_cluster below (no utilization gate: there's no case for
        # paying Photon's premium on an exploratory cluster regardless of how busy it is).
        # Threshold raised from the old <=20% ("basically idle") to <80% — Photon's
        # premium needs sustained heavy utilization to pay off, not just "not idle".
        where_sql="entity_type != 'interactive' AND photon "
        "AND utilization_pct IS NOT NULL AND utilization_pct < 80",
        detail_sql="'photon, util ' || round(utilization_pct)::INT || '%'",
        # Photon's premium is a DBU-consumption multiplier (it burns more DBUs per
        # wall-clock hour), not a different $/DBU price — confirmed on this account that
        # the Photon and non-Photon SKU for the same tier charge identically per DBU (see
        # databricks_efficiency.sql's header comment). usage_quantity already has that
        # multiplier baked in by the time it reaches this query, so re-pricing it at "the
        # non-Photon rate" can never detect the premium. Uses Databricks' own published
        # jobs-compute figure (~2.9x DBUs) as a flat multiplier instead — a heuristic, not
        # a measured saving, hence 'candidate'.
        recoverable_cost_sql="round(billed_cost * (1 - 1 / 2.9), 2)",
        confidence_sql="'candidate'",
    ),
    WasteRule(
        category="photon_on_interactive_cluster",
        lens="WASTE",
        label="Photon enabled on an exploratory cluster",
        remedy="Disable Photon on this cluster — interactive/all-purpose clusters are "
        "for exploration, not the sustained heavy workloads Photon's premium is meant "
        "to pay for.",
        # Categorical, not a utilization threshold — the config itself is the waste.
        where_sql="entity_type = 'interactive' AND photon",
        detail_sql="'photon enabled on exploratory cluster'",
        # Same DBU-consumption-multiplier reasoning as photon_no_gain above, using
        # Databricks' published all-purpose figure (~2x DBUs) instead of the jobs one.
        recoverable_cost_sql="round(billed_cost * (1 - 1 / 2), 2)",
        # Heuristic multiplier, not a measured saving — same class as photon_no_gain/
        # missing_autotermination below, so 'candidate' rather than 'high' (the certain
        # fact is that Photon is enabled here, not the size of its premium).
        confidence_sql="'candidate'",
    ),
    # ── Active: cluster-config signals from system.compute.clusters/node_types ──────
    # All interactive-only (ephemeral job clusters aren't joined to this config data —
    # see databricks_efficiency.sql). All 'candidate' — the underlying config fact is
    # certain, but the recoverable-$ estimate is a heuristic multiplier (same class as
    # 'placement'/'photon_no_gain' above), not a measured saving.
    WasteRule(
        category="missing_autotermination",
        lens="WASTE",
        label="No auto-termination policy",
        remedy="Set an auto-termination timeout (e.g. 30-60 min) so idle time between "
        "interactive sessions stops billing.",
        where_sql="entity_type = 'interactive' AND auto_termination_minutes IS NULL",
        detail_sql="'no auto-termination policy'",
        # ponytail: flat estimate for time lost between sessions; tune with real
        # session-gap data once node_timeline is overlaid per-session instead of monthly.
        recoverable_cost_sql="round(billed_cost * 0.15, 2)",
        confidence_sql="'candidate'",
        source="optimnow-cloud-finops-skills (CC BY-SA 4.0), adapted",
    ),
    WasteRule(
        category="autoscale_misconfigured",
        lens="WASTE",
        label="Autoscaling misconfigured",
        remedy="Narrow the min/max worker range to match observed load; wide ranges "
        "let the cluster over-expand on transient spikes.",
        # ponytail: "misconfigured" = wide range (max >= 3x min) on a cluster that's
        # also independently underutilized — the range itself is providing no value.
        where_sql="entity_type = 'interactive' AND max_autoscale_workers IS NOT NULL "
        "AND max_autoscale_workers >= 3 * greatest(min_autoscale_workers, 1) "
        "AND utilization_pct IS NOT NULL AND utilization_pct <= 20",
        detail_sql="'autoscale ' || min_autoscale_workers || '-' || max_autoscale_workers "
        "|| ' workers, util ' || round(utilization_pct)::INT || '%'",
        recoverable_cost_sql="round(billed_cost * (1 - utilization_pct / 100.0) * 0.5, 2)",
        confidence_sql="'candidate'",
        source="optimnow-cloud-finops-skills (CC BY-SA 4.0), adapted",
    ),
    WasteRule(
        category="oversized_nodes",
        lens="WASTE",
        label="Oversized worker nodes",
        remedy="Right-size worker_node_type to observed CPU/memory demand — move to a "
        "smaller instance in the same family.",
        # ponytail: >=16 cores is a tunable "large" threshold, not a sizing catalog —
        # we don't have a target instance to recommend, just the current one + its size.
        where_sql="entity_type = 'interactive' AND core_count IS NOT NULL "
        "AND core_count >= 16 AND utilization_pct IS NOT NULL AND utilization_pct <= 20",
        detail_sql="worker_node_type || ' (' || core_count::INT || ' cores), util ' "
        "|| round(utilization_pct)::INT || '%'",
        recoverable_cost_sql="round(billed_cost * (1 - utilization_pct / 100.0) * 0.5, 2)",
        confidence_sql="'candidate'",
        source="optimnow-cloud-finops-skills (CC BY-SA 4.0), adapted",
    ),
    WasteRule(
        category="graviton_price_opportunity",
        lens="OPPORTUNITY",
        label="Non-Graviton instance (current pricing gap)",
        # This is a PRICING fact, not an obsolescence claim — x86 isn't "stale" hardware,
        # Graviton is just cheaper at today's AWS price list for equivalent capacity. If
        # AWS re-prices either architecture, this opportunity can shrink or vanish outright
        # — re-verify the price gap at decision time, don't treat this as a permanent win.
        remedy="At today's AWS pricing, an ARM/Graviton-equivalent node type is cheaper "
        "for the same capacity — worth moving for the savings, but verify perf parity "
        "first (not a drop-in win for every workload) and re-check the current price gap "
        "before committing, since it isn't fixed.",
        # ponytail: naming-convention heuristic (regex on the AWS family code), same
        # brittleness class flagged for compute_family — no authoritative "is Graviton"
        # field exists in system.compute.node_types. Best-effort only; never 'high'.
        where_sql="entity_type = 'interactive' AND worker_node_type IS NOT NULL "
        r"AND regexp_matches(worker_node_type, '^[a-z][0-9]+[a-z]*\.') "
        r"AND NOT regexp_matches(worker_node_type, '^[a-z][0-9]+g[a-z]*\.')",
        detail_sql="worker_node_type || ' (no Graviton/ARM generation detected)'",
        # ponytail: flat estimate of TODAY's Graviton/x86 price gap, not a fixed discount —
        # re-derive from actual list_prices if this rule's $ impact needs to stay accurate
        # as AWS pricing moves.
        recoverable_cost_sql="round(billed_cost * 0.15, 2)",
        confidence_sql="'candidate'",
        source="optimnow-cloud-finops-skills (CC BY-SA 4.0), adapted",
    ),
    WasteRule(
        category="on_demand_only",
        lens="OPPORTUNITY",
        label="On-demand only compute",
        remedy="Consider spot (with on-demand fallback) for fault-tolerant, "
        "non-critical clusters — confirm this workload can tolerate interruption "
        "before switching; we can't reliably tell prod from non-prod from tags alone.",
        where_sql="entity_type = 'interactive' AND availability = 'ON_DEMAND'",
        detail_sql="'100% on-demand'",
        recoverable_cost_sql="round(billed_cost * 0.5, 2)",  # ponytail: flat spot-discount estimate
        confidence_sql="'candidate'",
        source="optimnow-cloud-finops-skills (CC BY-SA 4.0), adapted",
    ),
    # ── Active: SQL warehouse query-pattern signal (system.query.history) ───────────
    WasteRule(
        category="sql_warehouse_low_cache_reuse",
        lens="WASTE",
        label="SQL warehouse rarely reusing cached results",
        remedy="Find what's driving traffic — recurring automated/health-check queries "
        "with a near-zero cache-hit rate are paying for fresh compute on work that's "
        "likely identical to a recent run. Lower the polling/refresh frequency, or fix "
        "cache invalidation so repeat queries can hit the result cache.",
        # ponytail: 1000 queries/month is a "this is clearly automated traffic, not a
        # human ad-hoc analyst" volume gate — a low-traffic warehouse with 3 queries and
        # a 0% cache-hit rate is noise, not a finding. Raise/lower if the traffic profile
        # of a given workspace needs a different cutoff.
        where_sql="entity_type = 'sql_warehouse' AND cache_hit_pct IS NOT NULL "
        "AND cache_hit_pct < 5 AND query_count IS NOT NULL AND query_count > 1000",
        detail_sql="'cache hit ' || round(cache_hit_pct, 1) || '%, ' "
        "|| query_count || ' queries'",
        # ponytail: flat estimate of the redundant-compute share; re-derive once
        # automated-vs-human query attribution (client_application/query_source) is
        # validated and can size this per the actual automated share instead of a flat cut.
        recoverable_cost_sql="round(billed_cost * 0.25, 2)",
        confidence_sql="'candidate'",
    ),
    WasteRule(
        category="sql_warehouse_disk_spill",
        lens="WASTE",
        label="SQL warehouse queries spilling to disk",
        remedy="Investigate memory pressure for the affected queries — repeated disk "
        "spill signals a skew, an oversized join/aggregation, or an undersized warehouse, "
        "not just slow queries.",
        # Same 2%-of-queries gate as the Redshift analog below — this is the same
        # underlying pattern (memory pressure → disk spill), different connector/source
        # column (system.query.history.spilled_local_bytes vs Redshift's WLM/STL views).
        where_sql="entity_type = 'sql_warehouse' AND spill_query_count IS NOT NULL "
        "AND query_count IS NOT NULL AND query_count > 0 "
        "AND spill_query_count::DOUBLE / query_count >= 0.02",
        detail_sql="spill_query_count || ' of ' || query_count || ' queries spilled to disk"
        "' || CASE WHEN spilled_bytes IS NOT NULL "
        "THEN ' (' || round(spilled_bytes / 1e9, 1) || ' GB)' ELSE '' END",
        # No clean $ tie — the fix is query/schema tuning, not a cost multiplier. Same
        # reasoning as redshift_disk_spill_queries below.
        recoverable_cost_sql="0",
        confidence_sql="'candidate'",
    ),
    # ── Active: SQL warehouse per-user attribution (system.query.history) ───────────
    WasteRule(
        category="sql_warehouse_user_concentration",
        lens="OPPORTUNITY",
        label="One user drives most of a shared warehouse's cost",
        remedy="Consider giving this user a dedicated, right-sized warehouse or applying "
        "a workload-management policy — a shared warehouse sized for many users is often "
        "oversized for the one user actually driving its spend.",
        # ponytail: duration-share is an estimate of DBU share under concurrency (DBUs
        # aren't billed per-query), not an exact split — always 'candidate'.
        where_sql="entity_type = 'sql_warehouse_user' AND duration_share_pct IS NOT NULL "
        "AND duration_share_pct >= 40",
        detail_sql="round(duration_share_pct)::INT || '% of warehouse spend, ' "
        "|| query_count || ' queries'",
        recoverable_cost_sql="round(billed_cost * 0.20, 2)",  # ponytail: flat rightsizing estimate
        confidence_sql="'candidate'",
    ),
    WasteRule(
        category="sql_warehouse_high_frequency_workload",
        lens="OPPORTUNITY",
        label="Recurring high-frequency workload on a serverless warehouse",
        remedy="This user's queries run on a tight, regular cadence (e.g. every few "
        "minutes) on a serverless warehouse — serverless bills per execution, so a less "
        "frequent schedule (hourly or daily instead of every few minutes) may cut cost "
        "roughly in proportion to the frequency reduction, if the workload's freshness "
        "needs allow it. We don't prescribe the new cadence — check with the workload's "
        "owner.",
        # ponytail: cadence is a visibility signal, not a recommended new schedule (a
        # deliberate scope decision) — always 'candidate', and only fires with enough
        # queries to make the interval meaningful (noise guard, same style as the
        # cache-reuse rule's query_count volume gate, lower here since this is per-user).
        where_sql="entity_type = 'sql_warehouse_user' AND warehouse_type = 'SERVERLESS' "
        "AND avg_interval_minutes IS NOT NULL AND avg_interval_minutes <= 60 "
        "AND query_count >= 20",
        detail_sql="'runs every ~' || round(avg_interval_minutes)::INT || ' min, ' "
        "|| query_count || ' queries this month'",
        # ponytail: flat placeholder — no target cadence is specified to size the real
        # saving against.
        recoverable_cost_sql="round(billed_cost * 0.5, 2)",
        confidence_sql="'candidate'",
    ),
    WasteRule(
        category="sql_warehouse_serverless_pricing_gap",
        lens="OPPORTUNITY",
        label="Serverless SQL warehouse with sustained usage",
        remedy="This warehouse runs on serverless pricing with sustained, high-volume "
        "query traffic — for a near-continuous workload, a classic/pro warehouse may be "
        "cheaper. Compare your account's serverless vs. classic/pro list price for this "
        "warehouse size before committing; serverless removes idle/cold-start management "
        "that a classic warehouse would need auto-stop tuning to match.",
        # No $ figure: comparing to a classic/pro equivalent needs a specific SKU-name
        # match in list_prices that hasn't been validated against a live account yet (see
        # databricks_efficiency.sql) — surfaced as a real but unpriced finding rather than
        # fabricating a discount, same pattern as snappy_to_zstd_compression below.
        where_sql="entity_type = 'sql_warehouse' AND warehouse_type = 'SERVERLESS' "
        "AND query_count IS NOT NULL AND query_count > 1000",
        detail_sql="'serverless, ' || query_count || ' queries this month'",
        recoverable_cost_sql="0",
        confidence_sql="'candidate'",
    ),
    # ── Active: serverless notebook placement (system.billing.usage) ────────────────
    WasteRule(
        category="notebook_could_move_to_jobs",
        lens="OPPORTUNITY",
        label="Serverless notebook workload could move to Jobs compute",
        remedy="If this notebook runs on a predictable, recurring cadence, wrapping it "
        "as a Job uses the lower jobs-compute rate instead of the interactive-notebook "
        "rate — confirm with its owner before automating it.",
        where_sql="entity_type = 'notebook'",
        detail_sql="'owner ' || coalesce(owner_user, 'unknown') || ' → jobs compute'",
        # Same real re-pricing as 'placement' above (jobs_priced_cost, from list_prices),
        # not a flat percentage.
        recoverable_cost_sql="round(greatest(billed_cost "
        "- coalesce(jobs_priced_cost, billed_cost), 0), 2)",
        confidence_sql="'candidate'",
    ),
    # ── Active: Model Serving endpoint signals (databricks._fetch_endpoint_efficiency) ──
    # `idle` and `failed` above already fire on endpoint rows with no rule of their own —
    # both are entity-type-agnostic, which is the whole point of putting serving into this
    # plane as ROWS rather than building a second findings surface. These two cover what
    # those can't: always-on capacity that IS being used, just barely.
    WasteRule(
        category="endpoint_scale_to_zero_disabled",
        lens="WASTE",
        label="Serving endpoint can't scale to zero on low traffic",
        remedy="Enable scale-to-zero on this endpoint so its provisioned capacity stops "
        "billing between requests — confirm the cold-start latency is acceptable for its "
        "callers first.",
        # Only on a MEASURED false. scale_to_zero_enabled IS NULL means served_entities
        # wasn't readable (the usage_only degradation rung), and firing on that would invent
        # a finding from config we never read — the same gate cluster_tagging's tag_count
        # uses. request_count IS NOT NULL for the same reason.
        where_sql="entity_type = 'endpoint' AND scale_to_zero_enabled = false "
        "AND request_count IS NOT NULL "
        "AND request_count < {low_traffic_endpoint_requests}",
        detail_sql="request_count || ' request(s), scale-to-zero off'",
        # Unpriced. The recoverable amount is the idle fraction of the endpoint's provisioned
        # wall-clock, and nothing we pull carries that: endpoint_usage has request timestamps,
        # not uptime. Real but unpriced beats a fabricated figure — the
        # snappy_to_zstd_compression precedent.
        recoverable_cost_sql="0",
        confidence_sql="'candidate'",
    ),
    WasteRule(
        category="endpoint_oversized_workload",
        lens="OPPORTUNITY",
        label="GPU serving endpoint on low traffic",
        remedy="Review this endpoint's workload size/GPU class against its actual request "
        "volume — a smaller class, or a CPU class for a small model, may serve it.",
        where_sql="entity_type = 'endpoint' "
        "AND workload_type IN ('GPU_MEDIUM', 'GPU_LARGE') "
        "AND request_count IS NOT NULL "
        "AND request_count < {low_traffic_endpoint_requests}",
        detail_sql="workload_type || ', ' || request_count || ' request(s)'",
        # Unpriced: no workload_size → SKU mapping exists in any system table we read, so
        # there is no real rate to re-price a step-down against. Contrast `placement`, which
        # IS priced precisely because jobs_priced_cost re-prices at a real list rate.
        recoverable_cost_sql="0",
        confidence_sql="'candidate'",
    ),
    # ── Active: AWS S3 storage-tiering signal (aws_focus.fetch_efficiency) ──────────
    WasteRule(
        category="s3_intelligent_tiering",
        lens="OPPORTUNITY",
        label="S3 storage not on Intelligent-Tiering",
        remedy="Add an S3 Lifecycle rule to auto-transition objects to Intelligent-Tiering "
        "— it moves objects between access tiers automatically with no retrieval fee, so "
        "there's little downside even for unpredictable access patterns.",
        # ponytail: text-match heuristic on ChargeDescription/SkuId (see aws_focus.py's
        # fetch_efficiency), not a real storage-class field — the AWS FOCUS export's exact
        # SKU wording for Intelligent-Tiering hasn't been confirmed against a live export
        # yet. Re-run `flashlight ingest` against a real AWS account and spot-check
        # cause_detail.storage_class before trusting this category, same discipline as the
        # NOT YET VALIDATED Databricks cluster-config columns above.
        where_sql="entity_type = 'storage' "
        "AND coalesce(storage_class, 'standard') != 'intelligent_tiering'",
        detail_sql="'storage_class=' || coalesce(storage_class, 'standard')",
        # ponytail: flat estimate of AWS's typical cited Intelligent-Tiering saving
        # (30-40%); re-derive from actual list_prices/access patterns if this needs to
        # track a specific account.
        recoverable_cost_sql="round(billed_cost * 0.35, 2)",
        confidence_sql="'candidate'",
    ),
    # ── Active: Delta compression signal (databricks._fetch_table_inventory) ────────
    WasteRule(
        category="snappy_to_zstd_compression",
        lens="WASTE",
        label="Delta table not on ZSTD compression",
        remedy="Migrate this table off Snappy to ZSTD (Databricks' current default) — "
        "ZSTD cites ~20% smaller files at a comparable read cost.",
        # Only fires on a CONFIRMED 'snappy' property — never on an absent/unset codec.
        # Most tables never have delta.parquet.compression.codec explicitly set at all
        # (it just inherits whatever the writing cluster's default was), so "unset"
        # tells us nothing about the actual on-disk codec and firing on it would violate
        # the waste-honesty invariant (never classify off a signal we don't measure).
        where_sql="entity_type = 'table' AND compression_codec = 'snappy'",
        detail_sql="'compression_codec=snappy, ' || coalesce(num_files, 0) || ' files'",
        # No per-table dollar figure exists — Databricks bills compute, not per-table
        # storage, so billed_cost is honestly 0 on a `table` row (see EntityType.TABLE
        # docstring). Fabricating a $ estimate from size × an assumed rate would violate
        # the waste-honesty invariant, so this stays visible but unpriced: a real "here
        # are your Snappy tables" list, not a dollar claim.
        recoverable_cost_sql="0",
        confidence_sql="'high'",  # the property value itself is a fact, not a heuristic
    ),
    # ── Active: Redshift cluster/workgroup signal (redshift.fetch_efficiency) ───────
    # entity_type='sql_warehouse' is reused, not new (see EntityType docstring: shared
    # SQL compute, cost-attributable but no honest per-entity utilization%) — these
    # rules key off cause_detail fields only the Redshift connector populates, so they
    # can never fire on a Databricks sql_warehouse row. `idle` (activity_count = 0)
    # already covers an idle Redshift cluster for free — no new category needed there.
    WasteRule(
        category="redshift_concurrency_scaling_overage",
        lens="OPPORTUNITY",
        label="Concurrency Scaling covering base-capacity gaps",
        remedy="Increase base cluster capacity (or move to a longer Reserved Instance "
        "term) so steady-state load doesn't spill into on-demand Concurrency Scaling — "
        "which bills per-second at a premium over committed base capacity.",
        where_sql="entity_type = 'sql_warehouse' AND concurrency_scaling_cost IS NOT NULL "
        "AND compute_cost IS NOT NULL AND compute_cost + concurrency_scaling_cost > 0 "
        "AND concurrency_scaling_cost / (compute_cost + concurrency_scaling_cost) >= 0.15",
        detail_sql="'concurrency scaling $' || round(concurrency_scaling_cost)::INT "
        "|| ' (' || round(100 * concurrency_scaling_cost "
        "/ (compute_cost + concurrency_scaling_cost))::INT || '% of compute+scaling spend)'",
        # Real measured $ (Cost Explorer), not a heuristic — eliminating/reducing
        # Concurrency Scaling usage recovers close to this whole amount.
        recoverable_cost_sql="round(concurrency_scaling_cost, 2)",
        confidence_sql="'candidate'",
    ),
    WasteRule(
        category="redshift_ri_coverage_gap",
        lens="OPPORTUNITY",
        label="On-demand Redshift node-hours alongside available RI coverage",
        remedy="Purchase or extend Reserved Instance coverage for this cluster's base "
        "nodes — a 3-year RI runs at roughly half the hourly rate of on-demand "
        "(the source analysis measured on-demand at ~2x the 3-year RI rate).",
        where_sql="entity_type = 'sql_warehouse' AND on_demand_node_hours IS NOT NULL "
        "AND on_demand_node_hours > 0",
        detail_sql="round(on_demand_node_hours)::INT || ' on-demand node-hours this month "
        "(vs ' || round(coalesce(reserved_node_hours, 0))::INT || ' reserved)'",
        # ponytail: flat 50% estimate from the deck's own on-demand-vs-3yr-RI price
        # ratio, applied to the on-demand share of compute cost — re-derive from actual
        # RI pricing if this needs to track a specific account's negotiated rates.
        recoverable_cost_sql="round(coalesce(compute_cost, 0) "
        "* (on_demand_node_hours "
        "/ nullif(on_demand_node_hours + coalesce(reserved_node_hours, 0), 0)) * 0.5, 2)",
        confidence_sql="'candidate'",
    ),
    WasteRule(
        category="redshift_spectrum_scan_cost",
        lens="OPPORTUNITY",
        label="Redshift Spectrum scan spend",
        remedy="Review external table partitioning and source file format — un-pruned "
        "scans over row-oriented formats (CSV/JSON) cost far more per query than "
        "partition-pruned, columnar (Parquet/ORC) sources.",
        where_sql="entity_type = 'sql_warehouse' AND spectrum_scan_cost IS NOT NULL "
        "AND spectrum_scan_cost > 0",
        detail_sql="'Spectrum scan $' || round(spectrum_scan_cost)::INT "
        "|| ' this month — verify partition pruning/columnar source format'",
        # ponytail: flat 30% estimate of a typical pruning/columnar-format win;
        # re-derive from actual bytes-scanned-vs-returned if that telemetry is added.
        recoverable_cost_sql="round(spectrum_scan_cost * 0.3, 2)",
        confidence_sql="'candidate'",
    ),
    WasteRule(
        category="redshift_disk_spill_queries",
        lens="WASTE",
        label="Queries spilling to disk",
        remedy="Investigate distribution/sort keys and WLM memory allocation for the "
        "affected queries — repeated disk spill signals a skew or memory-pressure "
        "problem, not just slow queries.",
        where_sql="entity_type = 'sql_warehouse' AND disk_spill_query_count IS NOT NULL "
        "AND query_count IS NOT NULL AND query_count > 0 "
        "AND disk_spill_query_count::DOUBLE / query_count >= 0.02",
        detail_sql="disk_spill_query_count || ' of ' || query_count "
        "|| ' queries spilled to disk'",
        # No clean $ tie — the fix is query/schema tuning, not a cost multiplier.
        recoverable_cost_sql="0",
        confidence_sql="'candidate'",
    ),
    WasteRule(
        category="redshift_wlm_queue_wait",
        lens="OPPORTUNITY",
        label="Sustained WLM queue wait",
        remedy="Consider Auto WLM, or add base concurrency/slots to the queue that's "
        "backing up — sustained queue wait means queries are waiting on slots more "
        "than they're waiting on Concurrency Scaling to spin up.",
        where_sql="entity_type = 'sql_warehouse' AND wlm_queue_wait_ms_p95 IS NOT NULL "
        "AND wlm_queue_wait_ms_p95 >= 5000",
        # p99 rides along in the detail text rather than a separate category — same
        # finding at a stricter percentile, not a distinct waste signal.
        detail_sql="'p95 queue wait ' || round(wlm_queue_wait_ms_p95 / 1000.0, 1) || 's'"
        " || coalesce(', p99 ' || round(wlm_queue_wait_ms_p99 / 1000.0, 1) || 's', '')",
        recoverable_cost_sql="0",  # a performance/config finding, not a priced one
        confidence_sql="'candidate'",
    ),
    # ── Active: Redshift query-pattern signal (redshift._fetch_query_patterns) ──────
    # entity_type='query_pattern' is new (see EntityType docstring) — a repeated SQL
    # shape run many times a month, the drill-down the cluster-level rows above can't
    # give (which query, not just "is the cluster spilling/skewed"). Gated on run_count
    # so a query that happened to run once isn't flagged as a "pattern".
    WasteRule(
        category="redshift_query_pattern_high_spill",
        lens="WASTE",
        label="Query pattern spilling to disk on most runs",
        remedy="Investigate this query's join/sort memory footprint and the WLM queue's "
        "memory allocation — a pattern that spills most times it runs has an "
        "underlying memory or plan problem, not just occasional bad luck.",
        where_sql="entity_type = 'query_pattern' AND run_count IS NOT NULL "
        "AND run_count >= 3 AND pct_runs_spilling IS NOT NULL AND pct_runs_spilling >= 0.5",
        detail_sql="round(pct_runs_spilling * 100)::INT || '% of ' || run_count "
        "|| ' runs spilled, avg ' || round(coalesce(avg_disk_spill_gb, 0), 2) || ' GB'",
        # Same reasoning as the cluster-level redshift_disk_spill_queries: a schema/
        # query-tuning finding, not a priced one.
        recoverable_cost_sql="0",
        confidence_sql="'candidate'",
    ),
    WasteRule(
        category="redshift_query_pattern_skew",
        lens="WASTE",
        label="Query pattern with high row skew across slices",
        remedy="Review the distribution key for the tables this query joins/aggregates "
        "— high skew means most slices sit idle while one slice does the bulk of the "
        "work, which caps parallelism regardless of cluster size.",
        where_sql="entity_type = 'query_pattern' AND run_count IS NOT NULL "
        "AND run_count >= 3 AND avg_skew_ratio IS NOT NULL AND avg_skew_ratio >= 2.0",
        detail_sql="'avg skew ' || round(avg_skew_ratio, 1) || 'x, max ' "
        "|| round(coalesce(max_skew_ratio, avg_skew_ratio), 1) || 'x over ' "
        "|| run_count || ' runs'",
        recoverable_cost_sql="0",  # distribution-design finding, not a priced one
        confidence_sql="'candidate'",
    ),
    # ── Active: Redshift table inventory (redshift._fetch_table_inventory) ──────────
    WasteRule(
        category="redshift_stale_compression_encoding",
        lens="WASTE",
        label="Redshift table without column encoding",
        remedy="Run ANALYZE COMPRESSION and apply the recommended column encodings — "
        "an unencoded table costs more in both storage and scan I/O than one with "
        "compression applied.",
        # Confirmed fact from SVV_TABLE_INFO, not a heuristic.
        where_sql="entity_type = 'table' AND encoded = 'N'",
        detail_sql="'encoding disabled, ' || coalesce(tbl_rows, 0) || ' rows'",
        # No per-table $ figure — RA3 storage bills in aggregate, not per-table (same
        # reasoning as snappy_to_zstd_compression above).
        recoverable_cost_sql="0",
        confidence_sql="'high'",
    ),
    WasteRule(
        category="redshift_table_maintenance_stale",
        lens="WASTE",
        label="Table due for VACUUM/ANALYZE",
        remedy="Run VACUUM (to re-sort and reclaim space) and/or ANALYZE (to refresh "
        "planner statistics) — a high unsorted-rows or stale-statistics percentage "
        "means query plans are working off outdated information.",
        where_sql="entity_type = 'table' "
        "AND (coalesce(unsorted_pct, 0) >= 20 OR coalesce(stats_off_pct, 0) >= 20)",
        detail_sql="'unsorted ' || round(coalesce(unsorted_pct, 0))::INT || '%, stats off ' "
        "|| round(coalesce(stats_off_pct, 0))::INT || '% — VACUUM/ANALYZE due'",
        recoverable_cost_sql="0",
        confidence_sql="'candidate'",
    ),
    WasteRule(
        category="redshift_table_unused",
        lens="WASTE",
        label="Table not queried in 90+ days",
        remedy="Confirm no downstream dependency (dashboards, exports, other jobs), then "
        "drop or archive the table — an unqueried table still consumes managed storage "
        "every month.",
        # Usage staleness, not maintenance staleness — a table can be perfectly sorted/
        # analyzed and still be dead weight nobody queries. days_since_last_access is
        # bounded by STL_SCAN's own retention (redshift.py's _TABLE_USAGE_SQL), so this
        # can only prove "not accessed within the window Redshift still logs" — a
        # 90+ day silence within that log window is still a real, if conservative, signal.
        where_sql="entity_type = 'table' AND days_since_last_access IS NOT NULL "
        "AND days_since_last_access >= 90",
        detail_sql="'not queried in ' || days_since_last_access || ' days'",
        # ponytail: Redshift doesn't bill managed storage per-table, so this is a flat
        # $/GB-month placeholder (RA3 managed storage list price, us-east-1, 2026) against
        # this table's own size — a tunable estimate, not a FOCUS-reconciled figure (same
        # honesty tier as redshift_ri_coverage_gap/redshift_spectrum_scan_cost above, which
        # already price real Redshift findings off assumed ratios/rates rather than an
        # exact billing split). Re-derive per-account if a customer's negotiated storage
        # rate differs materially from list price.
        recoverable_cost_sql="round(coalesce(native_quantity, 0) / 1024.0 * 0.024, 2)",
        confidence_sql="'candidate'",
    ),
    WasteRule(
        category="redshift_spectrum_table_scan",
        lens="OPPORTUNITY",
        label="External table driving Spectrum scan cost",
        remedy="Review this specific external table's partitioning and source file "
        "format — a low returned/scanned ratio means most bytes pulled from S3 are "
        "discarded before reaching the query result, the exact un-pruned-scan pattern "
        "the cluster-level Spectrum finding flags in aggregate.",
        where_sql="entity_type = 'table' AND spectrum_scanned_gb IS NOT NULL "
        "AND spectrum_scanned_gb >= 1",
        detail_sql="round(spectrum_scanned_gb, 1) || ' GB scanned'"
        "|| CASE WHEN coalesce(spectrum_returned_gb, 0) > 0 AND spectrum_scanned_gb > 0 "
        "THEN ', ' || round(100 * spectrum_returned_gb / spectrum_scanned_gb)::INT "
        "|| '% returned (pruning efficiency)' ELSE '' END "
        "|| ' across ' || coalesce(spectrum_scan_count, 0) || ' queries'",
        # Unpriced by design — the cluster-level redshift_spectrum_scan_cost above
        # already carries the real $ for this cluster's Spectrum spend (30% of the
        # actual billed Spectrum cost); pricing this per-table breakdown too would
        # double-count the same dollars against the same OPPORTUNITY lens.
        recoverable_cost_sql="0",
        confidence_sql="'candidate'",
    ),
    # ── Blocked: needs a new entity grain + doesn't cleanly reconcile to billed_cost ─
    WasteRule(
        category="classic_notebook_attribution",
        lens="WASTE",
        label="Per-notebook cost on classic all-purpose clusters",
        remedy="Not buildable from billing telemetry alone — classic (non-serverless) "
        "all-purpose clusters meter cost at the cluster level; Databricks emits no "
        "per-notebook cost in system.billing.usage for them (unlike serverless "
        "notebooks, see the 'notebook' entity_type above). Time-allocating cluster cost "
        "across notebooks would need a new join to system.access.audit notebook "
        "attach/command events — an estimate, not a measurement.",
        requires=(
            "system.access.audit notebook attach/command events for time-weighted "
            "cluster cost allocation",
        ),
    ),
    WasteRule(
        category="missing_delta_optimization",
        lens="WASTE",
        label="Missing Delta OPTIMIZE/VACUUM",
        remedy="Run OPTIMIZE (+ Z-ORDER) and VACUUM on high-volume tables — unmaintained "
        "tables inflate scan/compute cost on every downstream query.",
        requires=(
            "Delta table OPTIMIZE/VACUUM history (system.access.audit)",
            "a new 'table' entity_type — cost isn't billed per-table today",
        ),
        source="optimnow-cloud-finops-skills (CC BY-SA 4.0), adapted",
    ),
)


_DBX = ("Databricks",)
_AWS = ("AWS",)

# The evaluability scope of every rule: (providers, entity_types). Applied to the pool
# below, and a rule missing from here fails at import — so a new WasteRule cannot
# silently be absent from the coverage tables, which is exactly what the old
# hand-maintained map in the Redshift view allowed.
#
# `()` for providers means "any provider whose connector populates the fields the rule
# tests". Only four rules genuinely qualify; the dangerous mistake is leaving a rule
# there by default, because a coverage table then reports "clean" on a provider that
# never measured the signal. Each `()` below is a deliberate claim, checked against the
# connectors' own field docs:
#   underutilized / idle / failed  — utilization_pct / activity_count / failed_cost are
#       the base EfficiencyRecord fields, populated by any connector.
#   sql_warehouse_user_concentration — duration_share_pct, which redshift_user_activity.sql
#       populates for AWS exactly as Databricks does (see WasteRule's own docstring).
# Everything else names a field only one platform reports. The four sql_warehouse_* rules
# are the trap: they read cache_hit_pct / spill_query_count / warehouse_type='SERVERLESS',
# all Databricks-only, on an entity_type Redshift *does* emit — so scoping them by
# entity_type alone would put a false "clean" on every Redshift cluster.
_COVERAGE: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    # Cross-provider: base EfficiencyRecord fields, no entity_type restriction.
    "underutilized": ((), ()),
    "idle": ((), ()),
    "failed": ((), ()),
    "sql_warehouse_user_concentration": ((), ("sql_warehouse_user",)),
    # Databricks — compute utilization and node_timeline proxies.
    "job_low_utilization": (_DBX, ("job",)),
    "possible_memory_pressure": (_DBX, ("job", "interactive")),
    "possible_heavy_shuffle": (_DBX, ("job", "interactive")),
    "placement": (_DBX, ("interactive",)),
    # photon_no_gain's predicate is NEGATIVE (entity_type != 'interactive'), which a
    # tuple can't express — left open rather than stated wrongly.
    "photon_no_gain": (_DBX, ()),
    "photon_on_interactive_cluster": (_DBX, ("interactive",)),
    # Databricks — cluster configuration.
    "missing_autotermination": (_DBX, ("interactive",)),
    "autoscale_misconfigured": (_DBX, ("interactive",)),
    "oversized_nodes": (_DBX, ("interactive",)),
    "graviton_price_opportunity": (_DBX, ("interactive",)),
    "on_demand_only": (_DBX, ("interactive",)),
    # Databricks — SQL warehouse. Databricks-only by *field*, not by entity_type.
    "sql_warehouse_low_cache_reuse": (_DBX, ("sql_warehouse",)),  # cache_hit_pct
    "sql_warehouse_disk_spill": (_DBX, ("sql_warehouse",)),  # spill_query_count
    "sql_warehouse_serverless_pricing_gap": (_DBX, ("sql_warehouse",)),  # warehouse_type
    "sql_warehouse_high_frequency_workload": (_DBX, ("sql_warehouse_user",)),  # warehouse_type
    "notebook_could_move_to_jobs": (_DBX, ("notebook",)),
    # Databricks — Model Serving endpoint (needs system.serving; see the connector's
    # _resolve_serving_tables degradation rungs).
    "endpoint_scale_to_zero_disabled": (_DBX, ("endpoint",)),
    "endpoint_oversized_workload": (_DBX, ("endpoint",)),
    "snappy_to_zstd_compression": (_DBX, ("table",)),  # Delta compression_codec
    # AWS — S3.
    "s3_intelligent_tiering": (_AWS, ("storage",)),
    # AWS — Redshift cluster / workgroup.
    "redshift_concurrency_scaling_overage": (_AWS, ("sql_warehouse",)),
    "redshift_ri_coverage_gap": (_AWS, ("sql_warehouse",)),
    "redshift_spectrum_scan_cost": (_AWS, ("sql_warehouse",)),
    "redshift_disk_spill_queries": (_AWS, ("sql_warehouse",)),
    "redshift_wlm_queue_wait": (_AWS, ("sql_warehouse",)),
    # AWS — Redshift query shapes and tables.
    "redshift_query_pattern_high_spill": (_AWS, ("query_pattern",)),
    "redshift_query_pattern_skew": (_AWS, ("query_pattern",)),
    "redshift_stale_compression_encoding": (_AWS, ("table",)),
    "redshift_table_maintenance_stale": (_AWS, ("table",)),
    "redshift_table_unused": (_AWS, ("table",)),
    "redshift_spectrum_table_scan": (_AWS, ("table",)),
    # Blocked (where_sql is None) — both Databricks patterns awaiting telemetry.
    "classic_notebook_attribution": (_DBX, ()),
    "missing_delta_optimization": (_DBX, ("table",)),
}


def _with_coverage(rule: WasteRule) -> WasteRule:
    """Attach *rule*'s evaluability scope, failing loudly if it was never declared."""
    try:
        providers, entity_types = _COVERAGE[rule.category]
    except KeyError:  # pragma: no cover - import-time guard, asserted by tests
        raise RuntimeError(
            f"WasteRule '{rule.category}' has no _COVERAGE entry. Declare which "
            "provider_name values and entity_types it can be evaluated for — a rule "
            "shown outside its scope reports 'clean' for a check that never ran."
        ) from None
    return replace(rule, providers=providers, entity_types=entity_types)


WASTE_RULES: tuple[WasteRule, ...] = tuple(_with_coverage(r) for r in _RULES_RAW)


def is_blocked(rule: WasteRule) -> bool:
    """A rule that isn't evaluated at all — neither 'clean' nor 'no data'."""
    return rule.where_sql is None


def coverage_groups(
    provider_name: str,
) -> tuple[tuple[str, tuple[WasteRule, ...]], ...]:
    """Every ACTIVE rule evaluable for *provider_name*, grouped by the entity_type whose
    telemetry it needs — the rule-coverage table's own structure.

    Derived from the pool rather than restated, so adding a :class:`WasteRule` puts it in
    the right coverage table with no second edit (and, via :data:`_COVERAGE`, cannot be
    forgotten). Rules with no entity_type restriction land under ``""`` — they apply to
    any measured entity.

    Blocked rules are excluded: they aren't evaluated, so they're neither "clean" nor
    "no data". See :func:`blocked_rules`.
    """
    groups: dict[str, list[WasteRule]] = {}
    for rule in WASTE_RULES:
        if is_blocked(rule):
            continue
        if rule.providers and provider_name not in rule.providers:
            continue
        for entity_type in rule.entity_types or ("",):
            groups.setdefault(entity_type, []).append(rule)
    return tuple((et, tuple(rules)) for et, rules in groups.items())


def blocked_rules(provider_name: str) -> tuple[WasteRule, ...]:
    """*provider_name*'s BLOCKED rules — real patterns awaiting telemetry, listed apart
    from the evaluated ones so "not implemented" never reads as "checked and clean"."""
    return tuple(
        r
        for r in WASTE_RULES
        if is_blocked(r) and (not r.providers or provider_name in r.providers)
    )


def build_waste_record_sql() -> str:
    """Compile the ACTIVE rules into the ``gold.waste_record`` view SQL.

    Config-driven: adding a :class:`WasteRule` with ``where_sql`` set is picked up
    here with no other code change — the next ``flashlight transform`` classifies it.
    Rule SQL carrying ``{placeholder}`` names is filled from
    :mod:`~flashlight.efficiency.policy_config` here, so a threshold change lands in
    the published GOLD rather than being re-applied per reader.
    """
    # Pair each rule with its narrowed where_sql — the filter alone doesn't tell the
    # type checker the Optional is gone.
    active = [(r, r.where_sql) for r in WASTE_RULES if r.where_sql is not None]
    fill = threshold_values()
    branches = "\nUNION ALL\n".join(
        f"""SELECT provider_name, charge_month, entity_type, entity_id, entity_name,
       owner_user, owner_project, billed_cost,
       '{r.category}' AS waste_category, '{r.lens}' AS lens,
       {r.detail_sql.format(**fill)} AS detail,
       {r.recoverable_cost_sql.format(**fill)} AS recoverable_cost,
       {r.confidence_sql.format(**fill)} AS confidence
FROM e
WHERE {where_sql.format(**fill)}"""
        for r, where_sql in active
    )
    return f"""
CREATE OR REPLACE VIEW gold.waste_record AS
WITH e AS (
    SELECT
        provider_name,
        strptime(charge_month, '%Y-%m')::date              AS charge_month,
        entity_type,
        entity_id,
        entity_name,
        owner_user,
        owner_project,
        billed_cost,
        utilization_pct,
        activity_count,
        TRY_CAST(json_extract_string(cause_detail, '$.failed_cost') AS DOUBLE)
                                                           AS failed_cost,
        TRY_CAST(json_extract_string(cause_detail, '$.pct_runs_underutilized') AS DOUBLE)
                                                           AS pct_runs_underutilized,
        coalesce(TRY_CAST(json_extract_string(cause_detail, '$.photon') AS BOOLEAN), false)
                                                           AS photon,
        TRY_CAST(json_extract_string(cause_detail, '$.min_autoscale_workers') AS BIGINT)
                                                           AS min_autoscale_workers,
        TRY_CAST(json_extract_string(cause_detail, '$.max_autoscale_workers') AS BIGINT)
                                                           AS max_autoscale_workers,
        TRY_CAST(json_extract_string(cause_detail, '$.auto_termination_minutes') AS BIGINT)
                                                           AS auto_termination_minutes,
        json_extract_string(cause_detail, '$.worker_node_type')
                                                           AS worker_node_type,
        TRY_CAST(json_extract_string(cause_detail, '$.core_count') AS DOUBLE)
                                                           AS core_count,
        json_extract_string(cause_detail, '$.availability')
                                                           AS availability,
        json_extract_string(cause_detail, '$.storage_class')
                                                           AS storage_class,
        json_extract_string(cause_detail, '$.compression_codec')
                                                           AS compression_codec,
        TRY_CAST(json_extract_string(cause_detail, '$.num_files') AS BIGINT)
                                                           AS num_files,
        TRY_CAST(json_extract_string(cause_detail, '$.max_cpu_pct') AS DOUBLE)
                                                           AS max_cpu_pct,
        TRY_CAST(json_extract_string(cause_detail, '$.max_mem_pct') AS DOUBLE)
                                                           AS max_mem_pct,
        TRY_CAST(json_extract_string(cause_detail, '$.job_shaped_cost') AS DOUBLE)
                                                           AS job_shaped_cost,
        json_extract_string(cause_detail, '$.top_job_name')
                                                           AS top_job_name,
        json_extract_string(cause_detail, '$.top_job_owner')
                                                           AS top_job_owner,
        TRY_CAST(json_extract_string(cause_detail, '$.cache_hit_pct') AS DOUBLE)
                                                           AS cache_hit_pct,
        TRY_CAST(json_extract_string(cause_detail, '$.query_count') AS BIGINT)
                                                           AS query_count,
        TRY_CAST(json_extract_string(cause_detail, '$.spill_query_count') AS BIGINT)
                                                           AS spill_query_count,
        TRY_CAST(json_extract_string(cause_detail, '$.spilled_bytes') AS DOUBLE)
                                                           AS spilled_bytes,
        TRY_CAST(json_extract_string(cause_detail, '$.shuffle_bytes') AS DOUBLE)
                                                           AS shuffle_bytes,
        TRY_CAST(json_extract_string(cause_detail, '$.pct_time_high_cpu_wait') AS DOUBLE)
                                                           AS pct_time_high_cpu_wait,
        TRY_CAST(json_extract_string(cause_detail, '$.pct_time_high_mem_swap') AS DOUBLE)
                                                           AS pct_time_high_mem_swap,
        TRY_CAST(json_extract_string(cause_detail, '$.min_local_disk_free_bytes') AS DOUBLE)
                                                           AS min_local_disk_free_bytes,
        TRY_CAST(json_extract_string(cause_detail, '$.network_bytes') AS DOUBLE)
                                                           AS network_bytes,
        TRY_CAST(json_extract_string(cause_detail, '$.avg_run_seconds') AS DOUBLE)
                                                           AS avg_run_seconds,
        json_extract_string(cause_detail, '$.warehouse_type')
                                                           AS warehouse_type,
        TRY_CAST(json_extract_string(cause_detail, '$.avg_interval_minutes') AS DOUBLE)
                                                           AS avg_interval_minutes,
        TRY_CAST(json_extract_string(cause_detail, '$.duration_share_pct') AS DOUBLE)
                                                           AS duration_share_pct,
        -- Model Serving endpoint keys (databricks._fetch_endpoint_efficiency).
        json_extract_string(cause_detail, '$.serving_mode')
                                                           AS serving_mode,
        TRY_CAST(json_extract_string(cause_detail, '$.scale_to_zero_enabled') AS BOOLEAN)
                                                           AS scale_to_zero_enabled,
        json_extract_string(cause_detail, '$.workload_type')
                                                           AS workload_type,
        json_extract_string(cause_detail, '$.workload_size')
                                                           AS workload_size,
        TRY_CAST(json_extract_string(cause_detail, '$.request_count') AS BIGINT)
                                                           AS request_count,
        TRY_CAST(json_extract_string(cause_detail, '$.total_tokens') AS BIGINT)
                                                           AS total_tokens,
        TRY_CAST(json_extract_string(cause_detail, '$.jobs_priced_cost') AS DOUBLE)
                                                           AS jobs_priced_cost,
        TRY_CAST(json_extract_string(cause_detail, '$.compute_cost') AS DOUBLE)
                                                           AS compute_cost,
        TRY_CAST(json_extract_string(cause_detail, '$.concurrency_scaling_cost') AS DOUBLE)
                                                           AS concurrency_scaling_cost,
        TRY_CAST(json_extract_string(cause_detail, '$.spectrum_scan_cost') AS DOUBLE)
                                                           AS spectrum_scan_cost,
        TRY_CAST(json_extract_string(cause_detail, '$.wlm_queue_wait_ms_p95') AS DOUBLE)
                                                           AS wlm_queue_wait_ms_p95,
        TRY_CAST(json_extract_string(cause_detail, '$.wlm_queue_wait_ms_p99') AS DOUBLE)
                                                           AS wlm_queue_wait_ms_p99,
        TRY_CAST(json_extract_string(cause_detail, '$.disk_spill_query_count') AS BIGINT)
                                                           AS disk_spill_query_count,
        TRY_CAST(json_extract_string(cause_detail, '$.on_demand_node_hours') AS DOUBLE)
                                                           AS on_demand_node_hours,
        TRY_CAST(json_extract_string(cause_detail, '$.reserved_node_hours') AS DOUBLE)
                                                           AS reserved_node_hours,
        -- Only present when STL_QUERY's retention doesn't reach back to the window's
        -- start — the date activity *is* measured from, so a rule firing on a partial
        -- window (see redshift.py's _activity) can caveat "measured since X" instead of
        -- implying full-window coverage.
        json_extract_string(cause_detail, '$.activity_measured_since')
                                                           AS activity_measured_since,
        json_extract_string(cause_detail, '$.diststyle')  AS diststyle,
        TRY_CAST(json_extract_string(cause_detail, '$.unsorted_pct') AS DOUBLE)
                                                           AS unsorted_pct,
        TRY_CAST(json_extract_string(cause_detail, '$.stats_off_pct') AS DOUBLE)
                                                           AS stats_off_pct,
        TRY_CAST(json_extract_string(cause_detail, '$.tbl_rows') AS BIGINT)
                                                           AS tbl_rows,
        json_extract_string(cause_detail, '$.encoded')    AS encoded,
        -- Redshift query_pattern entities (redshift._fetch_query_patterns) — a repeated
        -- query shape's run distribution for the month.
        TRY_CAST(json_extract_string(cause_detail, '$.run_count') AS BIGINT)
                                                           AS run_count,
        TRY_CAST(json_extract_string(cause_detail, '$.pct_runs_spilling') AS DOUBLE)
                                                           AS pct_runs_spilling,
        TRY_CAST(json_extract_string(cause_detail, '$.avg_disk_spill_gb') AS DOUBLE)
                                                           AS avg_disk_spill_gb,
        TRY_CAST(json_extract_string(cause_detail, '$.avg_skew_ratio') AS DOUBLE)
                                                           AS avg_skew_ratio,
        TRY_CAST(json_extract_string(cause_detail, '$.max_skew_ratio') AS DOUBLE)
                                                           AS max_skew_ratio,
        -- Redshift table entities (redshift._fetch_table_inventory) — usage, not just
        -- maintenance-staleness (unsorted_pct/stats_off_pct above are a different signal).
        TRY_CAST(json_extract_string(cause_detail, '$.days_since_last_access') AS BIGINT)
                                                           AS days_since_last_access,
        -- Redshift Spectrum per-table scan usage (redshift._fetch_spectrum_table_usage) —
        -- a different table row from the internal-storage fields above.
        TRY_CAST(json_extract_string(cause_detail, '$.spectrum_scan_count') AS BIGINT)
                                                           AS spectrum_scan_count,
        TRY_CAST(json_extract_string(cause_detail, '$.spectrum_scanned_gb') AS DOUBLE)
                                                           AS spectrum_scanned_gb,
        TRY_CAST(json_extract_string(cause_detail, '$.spectrum_returned_gb') AS DOUBLE)
                                                           AS spectrum_returned_gb,
        native_quantity
    FROM metrics.efficiency_record
)
{branches};
"""
