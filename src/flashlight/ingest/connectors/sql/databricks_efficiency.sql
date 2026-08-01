-- Databricks System Tables -> EfficiencyRecord aggregation (the waste plane).
--
-- Emits ONE row per (entity x month), aggregated AT SOURCE, for the efficiency/waste
-- GOLD view. Reuses the same warehouse + account-prices table as the FOCUS pull, so
-- billed_cost reconciles to FOCUS. Run as a single statement (no internal `;`).
--
-- Substituted by the connector: :account_prices (price table), :start_date, :end_date.
--
-- VALIDATED against a live warehouse 2026-06-28: all columns/struct fields below confirmed
-- (pricing.default, usage_metadata.*, identity_metadata.run_as, job_run_timeline.result_state,
-- node_timeline.cpu_*), and billing_origin_product values include JOBS / ALL_PURPOSE / SQL /
-- MODEL_SERVING. Re-validate if pointed at a different account/region/release.
-- Utilization here is the cluster's monthly avg attributed to the workload — the precise
-- version overlaps node_timeline with each run window (cause_detail.pct_runs_underutilized),
-- a later enrichment. ponytail: cluster-month avg is the tractable approximation; upgrade to
-- per-run overlap when the signal needs it.
--
-- Name/owner resolution below reuses the same system tables/columns as the sibling FOCUS
-- query (databricks_focus_1_3.sql — vendored, already proven against this exact workspace):
-- usage_metadata.job_name (a struct field on system.billing.usage, no join needed) and the
-- cluster_names/warehouse_names SCD-dedup pattern over system.compute.clusters/warehouses.
-- NOT YET VALIDATED against a live warehouse: job_meta (system.lakeflow.jobs, for
-- run_as_user_name/owner resolution only — the FOCUS query doesn't need owner names, so it
-- doesn't prove this table). Re-run `flashlight ingest` and check owner_user before trusting
-- it in production; system.lakeflow.jobs.run_as_user_name is documented as unpopulated for
-- rows emitted before ~December 2025.
--
-- VALIDATED against a live warehouse 2026-07-04 (via DESCRIBE TABLE): the
-- system.compute.clusters columns below all exist exactly as named — min/
-- max_autoscale_workers, auto_termination_minutes, worker_node_type, owned_by, and
-- aws/azure/gcp_attributes.availability (nested struct fields). There is NO
-- single_user_name (or equivalent) column on this table — a single-user cluster's owner
-- still falls back to owned_by/heaviest-cost-user, same as any other cluster.
-- NOT YET VALIDATED against a live warehouse: core_count from system.compute.node_types.
-- Re-run `flashlight ingest` and spot-check cause_detail before trusting the
-- efficiency.waste_record categories that depend on it (oversized_nodes — see
-- efficiency/waste_rules.py).
-- These only apply to ALL_PURPOSE (interactive) clusters — job clusters aren't joined
-- to cluster_meta/node_types here (ephemeral per-run config, lower-value signal; can
-- be added later by joining cluster_meta/node_types into the JOBS branch too).
--
-- NOT YET VALIDATED against a live warehouse: max_cpu_pct/max_mem_pct (MAX alongside the
-- existing AVG in the `util` CTE, same node_timeline source). Average utilization can't
-- distinguish "well-utilized" from "one hot executor skewing the average" or from genuine
-- memory pressure/spill — this surfaces the per-cluster-month peak as a visibility signal
-- (see job_low_utilization's detail_sql in waste_rules.py), not an automated leak verdict.
-- Re-run `flashlight ingest` and spot-check cause_detail.max_cpu_pct/max_mem_pct before
-- trusting it.
--
-- VALIDATED against a live warehouse 2026-07-11 (via DESCRIBE TABLE + a 30-day sample):
-- system.query.history DOES carry a real spill metric — spilled_local_bytes ("size of
-- data, in bytes, temporarily written to disk while executing the statement") and
-- shuffle_read_bytes ("total amount of data in bytes sent over the network"), both
-- top-level, populated columns (the header note above, previously claiming no such
-- metric is known to exist, was wrong). Sample: 78 of 66,709 SQL-warehouse/serverless
-- queries spilled in 30 days (worst case ~21 TB spilled in one statement); 8,151 (12%)
-- triggered shuffle. compute.type on every row is WAREHOUSE or SERVERLESS_COMPUTE —
-- this table has no visibility into JOBS/ALL_PURPOSE Spark execution, so spill/shuffle
-- stays sql_warehouse-only (see 'sql_warehouse_disk_spill' in waste_rules.py).
--
-- INVESTIGATED 2026-07-11, deliberately NOT extended (see "Known limitations" in
-- docs/design/efficiency-waste.md for the full writeup):
-- (a) JOBS/ALL_PURPOSE spill/shuffle has no system-table source at all — checked
--     `SHOW TABLES IN system.compute`/`system.lakeflow`, neither has a Spark
--     stage-metrics table. The only real paths (native Compute Metrics UI — no API;
--     Spark event log replay — UI-only, no API/SQL; self-hosted spark-metrics ->
--     Prometheus — new infra + new connector architecture) are all out of scope.
-- (b) DLT/Lakeflow serverless pipeline compute (usage_metadata.dlt_pipeline_id) is
--     NOT picked up by any branch below — it has warehouse_id/cluster_id/job_id all
--     NULL, so it fails every branch's join condition. Confirmed
--     query_source.pipeline_info.pipeline_id (system.query.history) does join to
--     usage_metadata.dlt_pipeline_id (system.billing.usage) correctly, but a real fix
--     needs a new EntityType + a 6th UNION ALL branch with its own billed_cost
--     attribution, not a join into an existing branch — job_id on these REFRESH rows
--     does NOT match usage_metadata.job_id (checked, zero matches). Deferred: 30 days
--     of this billing line showed zero spill, zero shuffle — revisit if that changes.
--     FOCUS/TCO cost is unaffected (databricks_focus_1_3.sql sums billing.usage
--     unconditionally) — only waste classification can't see this compute class.
--
-- ADDED 2026-07-12, VALIDATED against a live warehouse (30-day percentile scan, see
-- inline comments in the `util`/`runs` CTEs for exact numbers): four proxy signals for
-- JOBS/ALL_PURPOSE spill/shuffle-adjacent memory pressure, since (a) above rules out a
-- direct measurement. pct_time_high_cpu_wait/pct_time_high_mem_swap/
-- min_local_disk_free_bytes/network_bytes all come from system.compute.node_timeline
-- (already joined for CPU/mem utilization) — no new join. avg_run_seconds
-- (system.lakeflow.job_run_timeline, JOBS only) is a materiality gate on these proxies,
-- not a signal itself — see 'possible_memory_pressure'/'possible_heavy_shuffle' in
-- waste_rules.py for how they combine. worker_node_type/core_count are now also
-- populated for JOBS (joined via u.cluster_id into cluster_meta/node_types, the same
-- pattern the interactive branch already used) — context for the proxy detail text,
-- not a gate themselves.
--
-- NOT YET VALIDATED against a live warehouse: job_shaped_cost/top_job_name/top_job_owner
-- (the interactive_jobs CTE) — relies on usage_metadata.job_id being populated on rows
-- billed as ALL_PURPOSE, which is documented Databricks behavior (a job run against an
-- existing interactive cluster still carries job_id) but hasn't been spot-checked on a
-- live account yet. Re-run `flashlight ingest` and confirm cause_detail.top_job_name is
-- populated before trusting the 'placement' rule's per-workload detail (waste_rules.py).
--
-- VALIDATED (schema) against a live warehouse 2026-07-04: system.query.history has
-- executed_by, total_duration_ms, from_result_cache, start_time/end_time exactly as
-- named — but NO top-level warehouse_id; it's nested at compute.warehouse_id (also
-- compute.cluster_id, compute.type). Both warehouse_query_stats and
-- warehouse_query_users join/filter/group on compute.warehouse_id accordingly (an
-- earlier version used the bare column name and failed at runtime with
-- UNRESOLVED_COLUMN). cache_hit_pct/query_count/duration_share_pct/avg_interval_minutes
-- values themselves are still worth a data-level spot-check (not just column names) —
-- see cause_detail on a real ingest before fully trusting
-- sql_warehouse_low_cache_reuse/sql_warehouse_user_concentration/
-- sql_warehouse_high_frequency_workload (waste_rules.py). automated-vs-human query
-- attribution (client_application/query_source) is a further enrichment, deliberately
-- left out.
--
-- VALIDATED (schema) against a live warehouse 2026-07-04: warehouse_type exists on
-- system.compute.warehouses exactly as named (values CLASSIC/PRO/SERVERLESS per
-- Databricks docs — the actual values on this workspace's warehouses are still worth a
-- spot-check). duration_share_pct allocates the warehouse's real billed_cost by each
-- user's share of measured query duration that month — an estimate under concurrency
-- (DBUs aren't billed per-query), not an exact split; always 'candidate' downstream (see
-- sql_warehouse_user_concentration, sql_warehouse_high_frequency_workload,
-- sql_warehouse_serverless_pricing_gap in waste_rules.py).
--
-- ADDED 2026-07-31, NOT YET VALIDATED against a live warehouse: policy_id (cluster-only —
-- system.compute.clusters, no counterpart on warehouses) and tag_count (size(tags),
-- clusters AND warehouses both — a "has this resource been tagged at all" fact, distinct
-- from the per-usage-row custom_tags on system.billing.usage already read into `project`
-- above). Feeds the policy-compliance plane (efficiency/policy_rules.py's
-- cluster_tagging/warehouse_tagging/cluster_policy_assigned categories via
-- gold.policy_record) — a pass/fail governance signal, not a $ waste classification. Column
-- names are Databricks' documented system-table schema, not yet confirmed against a live
-- workspace the way every other column in this file has been — re-run `flashlight ingest`
-- and spot-check cause_detail.policy_id/tag_count before trusting the policy_record rows
-- that depend on them.
--
-- NOT YET VALIDATED against a live warehouse: notebook_id/notebook_path on rows where
-- billing_origin_product IN ('INTERACTIVE','NOTEBOOKS') (serverless notebooks) — the
-- fields themselves are proven for FOCUS resource naming (databricks_focus_1_3.sql:325,
-- 346) but not yet confirmed for this efficiency aggregation's GROUP BY. Classic
-- (non-serverless) all-purpose clusters carry no notebook identity in billing.usage at
-- all and stay rolled into the 'interactive'/cluster grain — see the 'notebook'
-- EntityType docstring (efficiency/model.py) and the BLOCKED classic_notebook_attribution
-- rule (waste_rules.py) for why that gap isn't estimated instead.
--
-- VALIDATED against a live warehouse 2026-07-04 (via system.billing.account_prices):
-- jobs_priced_cost (the p_jobs join in the `usage` CTE) re-prices the SAME
-- usage_quantity at the jobs-compute counterpart SKU's real rate from the SAME
-- list_prices table already joined above — a real dollar figure that stays correct as
-- Databricks reprices SKUs (no flat % to go stale), instead of a hand-picked constant.
-- Confirmed the ALL_PURPOSE/INTERACTIVE/NOTEBOOKS → JOBS swap resolves real SKU pairs
-- across all three tiers and every regional serverless variant present on this account
-- (e.g. STANDARD_ALL_PURPOSE_COMPUTE ↔ STANDARD_JOBS_COMPUTE,
-- ENTERPRISE_ALL_PURPOSE_SERVERLESS_COMPUTE_AP_JAKARTA ↔
-- ENTERPRISE_JOBS_SERVERLESS_COMPUTE_AP_JAKARTA). Only feeds the rules that swap between
-- two Databricks compute SKUs (placement, notebook_could_move_to_jobs) — the other
-- flat-percentage rules (spot/on-demand, S3 tiering, Graviton) price against AWS's
-- list, not Databricks', and are out of scope here.
--
-- REMOVED 2026-07-04 (was nonphoton_priced_cost, a p_nonphoton join re-pricing the same
-- usage_quantity at the SKU stripped of its `_(PHOTON)` suffix): confirmed against this
-- account that JOBS and JOBS_(PHOTON) charge the identical $/DBU rate (11,151 non-Photon
-- / 30 Photon rows, same SKU ENTERPRISE_JOBS_COMPUTE_(PHOTON), zero price delta). Photon's
-- real premium mechanism is burning more DBUs per wall-clock hour on the same VM (~2.9×
-- jobs / ~2× all-purpose per Databricks' own published figures), not a different $/DBU
-- price — and that premium is already baked into usage_quantity by the time it reaches
-- this query, so re-pricing that same usage_quantity at "the non-Photon rate" can never
-- detect it; it always nets to ~$0. photon_no_gain/photon_on_interactive_cluster
-- (waste_rules.py) now price the premium as a flat multiplier of billed_cost instead.
--
-- VALIDATED against a live warehouse 2026-07-04 (cross-checked two independent signals):
-- `photon` is sourced from usage_metadata's sibling struct product_features.is_photon —
-- Databricks' own authoritative flag — NOT a sku_name LIKE '%PHOTON%' text match (an
-- earlier version used the text match). The two signals agree exactly for JOBS on this
-- account (11,151 non-Photon / 30 Photon rows either way, same rows, same SKU
-- ENTERPRISE_JOBS_COMPUTE_(PHOTON)) — so the JOBS photon_no_gain finding above is not a
-- detection artifact. But they diverge sharply elsewhere: SQL warehouses show
-- is_photon=true on 100% of usage while the SKU name never contains "PHOTON" at all (same
-- for DATABASE/DATA_SHARING/PREDICTIVE_OPTIMIZATION) — Photon is baked into those SKUs
-- with no separate opt-in name, so the old text-match silently mismarked them as
-- non-Photon. Switching to the real flag doesn't change any currently-firing rule (
-- photon_no_gain/photon_on_interactive_cluster both require utilization_pct IS NOT NULL,
-- which sql_warehouse never has), but the cause_detail.photon fact itself is now
-- accurate for every entity_type instead of only the ones whose SKU happens to spell out
-- "PHOTON".

WITH prices AS (
  SELECT
    sku_name,
    cloud,
    CAST(pricing.default AS DOUBLE)                          AS unit_price,
    price_start_time,
    COALESCE(price_end_time, date_add(current_date, 1))      AS price_end_time
  FROM IDENTIFIER(:account_prices)
  WHERE currency_code = 'USD'
),
usage AS (
  SELECT
    u.usage_metadata.job_id                                  AS job_id,
    u.usage_metadata.job_name                                AS job_name,
    u.usage_metadata.cluster_id                              AS cluster_id,
    u.usage_metadata.warehouse_id                            AS warehouse_id,
    u.usage_metadata.notebook_id                             AS notebook_id,
    u.usage_metadata.notebook_path                           AS notebook_path,
    u.identity_metadata.run_as                               AS run_as,
    u.billing_origin_product                                 AS product,
    u.sku_name                                               AS sku_name,
    element_at(u.custom_tags, 'project')                     AS project,
    date_trunc('MONTH', u.usage_date)                        AS charge_month,
    u.usage_quantity                                         AS usage_quantity,
    u.usage_quantity * COALESCE(p.unit_price, 0)             AS cost,
    -- Re-price the same usage_quantity at the jobs-compute / non-Photon counterpart SKU's
    -- real rate (both from the SAME `prices` CTE) — falls back to this row's own price
    -- (no-op, zero implied saving) when the counterpart SKU can't be resolved, rather than
    -- fabricating a number. See the header comment for the regex/validation caveat.
    u.usage_quantity * COALESCE(p_jobs.unit_price, p.unit_price, 0)      AS jobs_priced_cost,
    coalesce(u.product_features.is_photon, false)            AS photon
  FROM system.billing.usage u
  LEFT JOIN prices p
    ON u.sku_name = p.sku_name AND u.cloud = p.cloud
   AND u.usage_end_time >= p.price_start_time
   AND u.usage_end_time <  p.price_end_time
  LEFT JOIN prices p_jobs
    ON p_jobs.sku_name = regexp_replace(u.sku_name, 'ALL_PURPOSE|INTERACTIVE|NOTEBOOKS', 'JOBS')
   AND p_jobs.cloud = u.cloud
   AND u.usage_end_time >= p_jobs.price_start_time
   AND u.usage_end_time <  p_jobs.price_end_time
  WHERE u.usage_date BETWEEN :start_date AND :end_date
),
util AS (
  SELECT
    cluster_id,
    date_trunc('MONTH', start_time)                          AS charge_month,
    AVG(cpu_user_percent + cpu_system_percent)               AS avg_cpu,
    AVG(mem_used_percent)                                    AS avg_mem,
    MAX(cpu_user_percent + cpu_system_percent)               AS max_cpu,
    MAX(mem_used_percent)                                    AS max_mem,
    -- Proxy signals for spill/shuffle on JOBS/ALL_PURPOSE — no direct spill/shuffle
    -- metric exists for these compute classes (see the "Known limitations" section in
    -- docs/design/efficiency-waste.md), so these approximate it from what node_timeline
    -- DOES carry. Fraction-of-time-elevated (not a single-minute MAX) is deliberate: a
    -- cluster runs hundreds of one-minute samples a month, so a MAX-based gate would
    -- fire on almost every cluster from one noisy minute alone (order-statistics
    -- inflation) — requiring a sustained share of the month's sampled minutes above
    -- threshold is the honest bar for "this was a real pattern, not a blip."
    -- Thresholds VALIDATED against a live warehouse 2026-07-11 (30-day percentile scan
    -- of job/all-purpose cluster node_timeline rows): cpu_wait_percent p50=0.08%,
    -- p90=5.4%, p99=61.5% — 20% sits between p90/p99, a real tail-only cutoff.
    AVG(CASE WHEN cpu_wait_percent >= 20 THEN 1.0 ELSE 0.0 END)
                                                              AS pct_time_high_cpu_wait,
    -- mem_swap_percent VALUE distribution (not a >0 binary — 78% of all rows have SOME
    -- nonzero swap, background-OS-level noise, a useless gate): p50=1.1%, p90=27.5%,
    -- p99=41.4% — 25% sits just below p90, a real elevated cutoff.
    AVG(CASE WHEN mem_swap_percent >= 25 THEN 1.0 ELSE 0.0 END)
                                                              AS pct_time_high_mem_swap,
    -- Spill writes to the node's LOCAL scratch disk specifically — /local_disk0, not
    -- the map's other keys (/, /var/lib/lxc are OS partitions, always small — ~7GB free
    -- — regardless of spill activity; blindly MIN-ing across the whole map would be
    -- dominated by that irrelevant OS partition). Observed 30-day minimum on this
    -- account across job/all-purpose clusters was 53.6 GB free — nowhere near
    -- exhaustion — so 10 GB is a safely-below-observed, genuinely-low absolute cutoff.
    MIN(element_at(disk_free_bytes_per_mount_point, '/local_disk0'))
                                                              AS min_local_disk_free_bytes,
    -- Shuffle is fundamentally inter-executor network I/O — a coarser, cluster-level
    -- proxy for shuffle volume than system.query.history's exact per-query
    -- shuffle_read_bytes (sql_warehouse only). Monthly SUM per cluster: p50=7.0GB,
    -- p90=400GB, p99=19.3TB — 500GB sits just above p90, catching real outliers without
    -- reaching into the p99 extreme.
    SUM(network_sent_bytes + network_received_bytes)        AS network_bytes
  FROM system.compute.node_timeline
  WHERE start_time >= :start_date AND start_time < :end_date + INTERVAL 1 DAY
  GROUP BY cluster_id, date_trunc('MONTH', start_time)
),
runs AS (
  SELECT
    job_id,
    date_trunc('MONTH', period_start_time)                  AS charge_month,
    COUNT(*)                                                AS run_count,
    SUM(CASE WHEN result_state IN ('ERROR','FAILED','TIMED_OUT') THEN 1 ELSE 0 END)
                                                           AS failed_runs,
    -- Materiality gate for the proxy signals above: a job with a healthy-looking
    -- elevated-wait/swap/network reading that only ever runs for 90 seconds isn't worth
    -- flagging — there's no meaningful optimization payoff. VALIDATED against a live
    -- warehouse 2026-07-11: run-duration p10=431s, p50=1183s, p90=3600s — 300s (5 min)
    -- sits just below p10, excluding only the shortest ~8-9% of runs.
    AVG(datediff(SECOND, period_start_time, period_end_time))
                                                           AS avg_run_seconds
  FROM system.lakeflow.job_run_timeline
  WHERE period_start_time >= :start_date AND period_start_time < :end_date + INTERVAL 1 DAY
  GROUP BY job_id, date_trunc('MONTH', period_start_time)
),
-- Owner lookup only (job name comes from usage_metadata.job_name above, same as the FOCUS
-- query). system.lakeflow.jobs is SCD — take the most-recent row per job_id.
-- run_as_user_name resolves run_as (human OR service principal) to a readable name in one
-- column — no separate service-principal directory exists in Databricks.
job_meta AS (
  SELECT job_id, run_as_user_name
  FROM system.lakeflow.jobs
  QUALIFY ROW_NUMBER() OVER (PARTITION BY job_id ORDER BY change_time DESC) = 1
),
-- Cluster/warehouse name lookups: identical pattern to cluster_names/warehouse_names in
-- databricks_focus_1_3.sql (already proven against this workspace). VALIDATED against a
-- live warehouse 2026-07-04: system.compute.clusters has no single-user-assignment column
-- (confirmed via DESCRIBE TABLE — no `single_user_name`; only `data_security_mode`, which
-- names the access-mode category, not the assigned user) — owner_user for single-user
-- clusters falls back to owned_by/heaviest-cost-user like every other cluster, same as
-- before this session's single_user_name attempt.
cluster_meta AS (
  SELECT
    cluster_id, cluster_name, owned_by,
    min_autoscale_workers, max_autoscale_workers, auto_termination_minutes,
    worker_node_type,
    COALESCE(aws_attributes.availability, azure_attributes.availability,
             gcp_attributes.availability)                  AS availability,
    policy_id,
    size(tags)                                              AS tag_count
  FROM system.compute.clusters
  QUALIFY ROW_NUMBER() OVER (PARTITION BY cluster_id ORDER BY change_time DESC) = 1
),
-- warehouse_type (CLASSIC/PRO/SERVERLESS) is a real fact, not a naming-convention guess —
-- drives sql_warehouse_serverless_pricing_gap / sql_warehouse_high_frequency_workload
-- (waste_rules.py) instead of inferring serverless from SKU text.
warehouse_meta AS (
  SELECT warehouse_id, warehouse_name, warehouse_type, size(tags) AS tag_count
  FROM system.compute.warehouses
  QUALIFY ROW_NUMBER() OVER (PARTITION BY warehouse_id ORDER BY change_time DESC) = 1
),
-- Real per-node-type capacity (core_count/memory_mb), not a naming-convention guess —
-- lets 'oversized_nodes' compare actual cores, not just an instance-name string.
node_types AS (
  SELECT node_type, core_count
  FROM system.compute.node_types
),
-- Jobs billed as ALL_PURPOSE (usage_metadata.job_id set even though billing_origin_product
-- is ALL_PURPOSE) are the actual migratable workload behind a 'placement' finding — a job
-- triggered against an existing interactive cluster instead of ephemeral jobs compute. This
-- is job_shaped_cost, a strict subset of the cluster's total billed_cost (genuinely
-- interactive/exploratory time on the same cluster isn't migratable and stays excluded).
-- top_job_* names the single largest such job so the finding points at one real workload
-- and owner, not just "this cluster" — see waste_rules.py's placement rule.
interactive_jobs AS (
  SELECT
    cluster_id,
    charge_month,
    SUM(cost)             AS job_shaped_cost,
    -- The job-shaped slice re-priced at jobs-compute rates — real recoverable_cost for
    -- 'placement' is job_shaped_cost - job_shaped_jobs_priced_cost (waste_rules.py),
    -- not a flat percentage.
    SUM(jobs_priced_cost) AS job_shaped_jobs_priced_cost,
    max_by(job_id, cost)  AS top_job_id,
    max_by(job_name, cost) AS top_job_name,
    max_by(run_as, cost)  AS top_job_owner
  FROM usage
  WHERE product = 'ALL_PURPOSE' AND job_id IS NOT NULL
  GROUP BY cluster_id, charge_month
),
-- Query-pattern health for SQL warehouses — a shared-compute entity has no per-entity
-- utilization (see EntityType.INTERACTIVE docstring), but cache reuse and query volume are
-- measurable regardless and catch a real waste pattern utilization can't: a warehouse
-- getting hammered by redundant automated queries that never hit the result cache (see
-- 'sql_warehouse_low_cache_reuse' in waste_rules.py).
-- VALIDATED against a live warehouse 2026-07-04: system.query.history has no top-level
-- warehouse_id — it's nested under the `compute` struct (compute.warehouse_id,
-- compute.cluster_id, compute.type). Was previously unvalidated and wrong.
warehouse_query_stats AS (
  SELECT
    compute.warehouse_id                                      AS warehouse_id,
    date_trunc('MONTH', start_time)                          AS charge_month,
    COUNT(*)                                                  AS query_count,
    100.0 * SUM(CASE WHEN from_result_cache THEN 1 ELSE 0 END) / COUNT(*)
                                                               AS cache_hit_pct,
    -- Disk spill + shuffle volume — see 'sql_warehouse_disk_spill' in waste_rules.py.
    -- shuffle_bytes rides along as a visibility number only (no rule reads it): shuffle
    -- is a normal consequence of joins/aggregations, not itself a waste signal, and
    -- there's no established "too much shuffle" threshold to gate on.
    SUM(CASE WHEN spilled_local_bytes > 0 THEN 1 ELSE 0 END)  AS spill_query_count,
    SUM(spilled_local_bytes)                                  AS spilled_bytes,
    SUM(shuffle_read_bytes)                                   AS shuffle_bytes
  FROM system.query.history
  WHERE start_time >= :start_date AND start_time < :end_date + INTERVAL 1 DAY
    AND compute.warehouse_id IS NOT NULL
  GROUP BY compute.warehouse_id, date_trunc('MONTH', start_time)
),
-- Per-user cost attribution + cadence for SQL warehouses. system.query.history has real
-- per-query user identity (executed_by) and timestamps that billing.usage doesn't carry at
-- all — billing only meters the warehouse as a whole. warehouse_duration_ms (a window sum
-- over all named users in the warehouse+month) lets the sql_warehouse_user branch below
-- allocate the warehouse's real billed_cost proportionally to each user's share of
-- measured query duration — an estimate under concurrency (DBUs aren't billed per-query),
-- not an exact split. avg_interval_minutes is a cadence visibility signal ('this workload
-- runs every N minutes on serverless — does it need to?'), not a recommended new schedule;
-- NULL until a user has run >= 2 queries in the month (a single query has no interval).
warehouse_query_users AS (
  SELECT
    compute.warehouse_id                                       AS warehouse_id,
    executed_by,
    date_trunc('MONTH', start_time)                           AS charge_month,
    COUNT(*)                                                   AS query_count,
    SUM(total_duration_ms)                                     AS user_duration_ms,
    SUM(SUM(total_duration_ms)) OVER (
      PARTITION BY compute.warehouse_id, date_trunc('MONTH', start_time)
    )                                                           AS warehouse_duration_ms,
    CASE WHEN COUNT(*) > 1
         THEN datediff(MINUTE, MIN(start_time), MAX(start_time)) / (COUNT(*) - 1)
         ELSE NULL END                                         AS avg_interval_minutes
  FROM system.query.history
  WHERE start_time >= :start_date AND start_time < :end_date + INTERVAL 1 DAY
    AND compute.warehouse_id IS NOT NULL AND executed_by IS NOT NULL
  GROUP BY compute.warehouse_id, executed_by, date_trunc('MONTH', start_time)
)
-- JOBS / DLT — per-job utilization (cluster avg), run count, failed-run cost
SELECT
  CAST(u.job_id AS STRING)                                  AS entity_id,
  'job'                                                     AS entity_type,
  COALESCE(MAX(u.job_name), CAST(u.job_id AS STRING))       AS entity_name,
  COALESCE(MAX(jm.run_as_user_name), u.run_as)              AS owner_user,
  MAX(u.project)                                            AS owner_project,
  CAST(u.charge_month AS DATE)                              AS charge_month,
  SUM(u.cost)                                               AS billed_cost,
  SUM(u.usage_quantity)                                     AS native_quantity,
  LEAST(100, AVG(GREATEST(ut.avg_cpu, ut.avg_mem)))         AS utilization_pct,
  MAX(r.run_count)                                          AS activity_count,
  MAX(r.run_count)                                          AS run_count,
  CAST(NULL AS DOUBLE)                                      AS pct_runs_underutilized,
  SUM(u.cost * (CASE WHEN r.run_count > 0
                     THEN r.failed_runs / r.run_count ELSE 0 END))  AS failed_cost,
  BOOL_OR(u.photon)                                         AS photon,
  CAST(NULL AS BIGINT)                                      AS min_autoscale_workers,
  CAST(NULL AS BIGINT)                                      AS max_autoscale_workers,
  CAST(NULL AS BIGINT)                                      AS auto_termination_minutes,
  -- Ephemeral per-run job cluster config, joined the same way as the interactive
  -- branch's cm/nt (same u.cluster_id key, already used above for the ut join) — a
  -- job with many distinct ephemeral cluster_ids across its runs gets MAX's "pick a
  -- representative value" treatment, same rigor as every other MAX() in this branch.
  -- Context for the proxy-signal detail text below, not a gate on its own.
  MAX(cmj.worker_node_type)                                 AS worker_node_type,
  MAX(ntj.core_count)                                       AS core_count,
  CAST(NULL AS STRING)                                      AS availability,
  MAX(ut.max_cpu)                                           AS max_cpu_pct,
  MAX(ut.max_mem)                                           AS max_mem_pct,
  CAST(NULL AS DOUBLE)                                      AS job_shaped_cost,
  CAST(NULL AS STRING)                                      AS top_job_name,
  CAST(NULL AS STRING)                                      AS top_job_owner,
  CAST(NULL AS DOUBLE)                                      AS cache_hit_pct,
  CAST(NULL AS BIGINT)                                      AS query_count,
  CAST(NULL AS STRING)                                      AS warehouse_type,
  CAST(NULL AS DOUBLE)                                      AS avg_interval_minutes,
  CAST(NULL AS DOUBLE)                                      AS duration_share_pct,
  CAST(NULL AS DOUBLE)                                      AS jobs_priced_cost,
  CAST(NULL AS BIGINT)                                      AS spill_query_count,
  CAST(NULL AS DOUBLE)                                      AS spilled_bytes,
  CAST(NULL AS DOUBLE)                                      AS shuffle_bytes,
  -- MAX not SUM: `ut`/`r` are already aggregated per (cluster_id|job_id, month) in
  -- their own CTEs, and this GROUP BY can join the SAME ut/r row onto multiple usage
  -- line-items — SUM would multiply-count by however many usage rows share that
  -- cluster_id/job_id this month. MAX (like the existing max_cpu_pct/max_mem_pct
  -- above) is idempotent under that duplication; SUM is not.
  MAX(ut.pct_time_high_cpu_wait)                            AS pct_time_high_cpu_wait,
  MAX(ut.pct_time_high_mem_swap)                            AS pct_time_high_mem_swap,
  MIN(ut.min_local_disk_free_bytes)                         AS min_local_disk_free_bytes,
  MAX(ut.network_bytes)                                     AS network_bytes,
  MAX(r.avg_run_seconds)                                    AS avg_run_seconds,
  -- Same ephemeral per-run cluster join as worker_node_type/core_count above — policy_id/
  -- tag_count describe the job's underlying cluster config, not the job itself.
  MAX(cmj.policy_id)                                        AS policy_id,
  MAX(cmj.tag_count)                                        AS tag_count
FROM usage u
LEFT JOIN util ut ON ut.cluster_id = u.cluster_id AND ut.charge_month = u.charge_month
LEFT JOIN runs r  ON r.job_id      = u.job_id     AND r.charge_month  = u.charge_month
LEFT JOIN job_meta jm ON jm.job_id = u.job_id
LEFT JOIN cluster_meta cmj ON cmj.cluster_id = u.cluster_id
LEFT JOIN node_types ntj ON ntj.node_type = cmj.worker_node_type
WHERE u.product = 'JOBS' AND u.job_id IS NOT NULL
GROUP BY u.job_id, u.run_as, u.charge_month
UNION ALL
-- ALL-PURPOSE (interactive) — grain is the CLUSTER (the actionable unit). Cluster-level
-- utilization is honest at this grain (it describes the cluster, not a user). owner_user
-- prefers the cluster's designated owner (owned_by), else the heaviest-cost user that
-- month (system.compute.clusters has no single-user-assignment column to prefer instead
-- — see cluster_meta above). Catches underutilized/idle all-purpose clusters (a top waste
-- vector). job_shaped_cost/top_job_* (from interactive_jobs) name the specific migratable
-- workload + owner behind a placement finding — see waste_rules.py. This stays
-- cluster-grain regardless of access mode: billing.usage carries no per-notebook identity
-- for classic (non-serverless) compute — see the 'notebook' branch below and its
-- EntityType docstring.
SELECT
  CAST(u.cluster_id AS STRING), 'interactive',
  COALESCE(MAX(cm.cluster_name), CAST(u.cluster_id AS STRING)),
  COALESCE(MAX(cm.owned_by), max_by(u.run_as, u.cost)),
  max_by(u.project, u.cost), CAST(u.charge_month AS DATE),
  SUM(u.cost), SUM(u.usage_quantity),
  LEAST(100, AVG(GREATEST(ut.avg_cpu, ut.avg_mem))),
  CAST(NULL AS BIGINT), CAST(NULL AS BIGINT), CAST(NULL AS DOUBLE),
  CAST(NULL AS DOUBLE), BOOL_OR(u.photon),
  MAX(cm.min_autoscale_workers), MAX(cm.max_autoscale_workers),
  MAX(cm.auto_termination_minutes), MAX(cm.worker_node_type),
  MAX(nt.core_count), MAX(cm.availability),
  MAX(ut.max_cpu), MAX(ut.max_mem),
  MAX(ij.job_shaped_cost),
  MAX(ij.top_job_name),
  COALESCE(MAX(jm2.run_as_user_name), MAX(ij.top_job_owner)),
  CAST(NULL AS DOUBLE), CAST(NULL AS BIGINT),
  CAST(NULL AS STRING), CAST(NULL AS DOUBLE), CAST(NULL AS DOUBLE),
  MAX(ij.job_shaped_jobs_priced_cost),
  CAST(NULL AS BIGINT), CAST(NULL AS DOUBLE), CAST(NULL AS DOUBLE),
  -- MAX not SUM — same duplication reasoning as the JOBS branch above.
  MAX(ut.pct_time_high_cpu_wait), MAX(ut.pct_time_high_mem_swap),
  MIN(ut.min_local_disk_free_bytes), MAX(ut.network_bytes),
  CAST(NULL AS DOUBLE),  -- avg_run_seconds: no job-run concept for interactive clusters
  MAX(cm.policy_id), MAX(cm.tag_count)
FROM usage u
LEFT JOIN util ut ON ut.cluster_id = u.cluster_id AND ut.charge_month = u.charge_month
LEFT JOIN cluster_meta cm ON cm.cluster_id = u.cluster_id
LEFT JOIN node_types nt ON nt.node_type = cm.worker_node_type
LEFT JOIN interactive_jobs ij ON ij.cluster_id = u.cluster_id AND ij.charge_month = u.charge_month
LEFT JOIN job_meta jm2 ON jm2.job_id = ij.top_job_id
WHERE u.product = 'ALL_PURPOSE' AND u.cluster_id IS NOT NULL
GROUP BY u.cluster_id, u.charge_month
UNION ALL
-- SQL WAREHOUSE — shared: cost-attributable; no per-entity utilization (NULL). No owner
-- column exists for warehouses in system.compute.warehouses, so owner_user stays raw run_as.
SELECT
  CAST(u.warehouse_id AS STRING), 'sql_warehouse',
  COALESCE(MAX(wm.warehouse_name), CAST(u.warehouse_id AS STRING)),
  u.run_as, MAX(u.project), CAST(u.charge_month AS DATE),
  SUM(u.cost), SUM(u.usage_quantity),
  CAST(NULL AS DOUBLE), CAST(NULL AS BIGINT), CAST(NULL AS BIGINT),
  CAST(NULL AS DOUBLE), CAST(NULL AS DOUBLE), BOOL_OR(u.photon),
  CAST(NULL AS BIGINT), CAST(NULL AS BIGINT), CAST(NULL AS BIGINT),
  CAST(NULL AS STRING), CAST(NULL AS DOUBLE), CAST(NULL AS STRING),
  CAST(NULL AS DOUBLE), CAST(NULL AS DOUBLE),
  CAST(NULL AS DOUBLE), CAST(NULL AS STRING), CAST(NULL AS STRING),
  MAX(qs.cache_hit_pct), MAX(qs.query_count),
  MAX(wm.warehouse_type), CAST(NULL AS DOUBLE), CAST(NULL AS DOUBLE),
  CAST(NULL AS DOUBLE),
  MAX(qs.spill_query_count), MAX(qs.spilled_bytes), MAX(qs.shuffle_bytes),
  CAST(NULL AS DOUBLE), CAST(NULL AS DOUBLE),
  CAST(NULL AS BIGINT), CAST(NULL AS DOUBLE), CAST(NULL AS DOUBLE),
  CAST(NULL AS STRING),  -- policy_id: cluster-only concept, no warehouse counterpart
  MAX(wm.tag_count)
FROM usage u
LEFT JOIN warehouse_meta wm ON wm.warehouse_id = u.warehouse_id
LEFT JOIN warehouse_query_stats qs
  ON qs.warehouse_id = u.warehouse_id AND qs.charge_month = u.charge_month
WHERE u.product = 'SQL' AND u.warehouse_id IS NOT NULL
GROUP BY u.warehouse_id, u.run_as, u.charge_month
UNION ALL
-- SQL WAREHOUSE, PER USER — real per-query attribution from system.query.history
-- (executed_by), allocated against the warehouse's actual billed_cost by each user's
-- share of measured query duration that month (candidate: DBUs aren't billed per-query,
-- duration-share is an estimate under concurrency, not an exact split). warehouse_type +
-- avg_interval_minutes ride along so a serverless, high-frequency, concentrated workload
-- is visible without us prescribing a new schedule — see sql_warehouse_user_concentration
-- / sql_warehouse_high_frequency_workload in waste_rules.py.
SELECT
  CAST(u.warehouse_id AS STRING) || ':' || wqu.executed_by,
  'sql_warehouse_user',
  COALESCE(MAX(wm.warehouse_name), CAST(u.warehouse_id AS STRING)) || ' (' || wqu.executed_by || ')',
  wqu.executed_by, MAX(u.project), CAST(u.charge_month AS DATE),
  SUM(u.cost) * (MAX(wqu.user_duration_ms) / NULLIF(MAX(wqu.warehouse_duration_ms), 0)),
  CAST(NULL AS DOUBLE), CAST(NULL AS DOUBLE), MAX(wqu.query_count),
  CAST(NULL AS BIGINT), CAST(NULL AS DOUBLE), CAST(NULL AS DOUBLE), false,
  CAST(NULL AS BIGINT), CAST(NULL AS BIGINT), CAST(NULL AS BIGINT),
  CAST(NULL AS STRING), CAST(NULL AS DOUBLE), CAST(NULL AS STRING),
  CAST(NULL AS DOUBLE), CAST(NULL AS DOUBLE),
  CAST(NULL AS DOUBLE), CAST(NULL AS STRING), CAST(NULL AS STRING),
  CAST(NULL AS DOUBLE), MAX(wqu.query_count),
  MAX(wm.warehouse_type), MAX(wqu.avg_interval_minutes),
  100.0 * MAX(wqu.user_duration_ms) / NULLIF(MAX(wqu.warehouse_duration_ms), 0),
  CAST(NULL AS DOUBLE),
  CAST(NULL AS BIGINT), CAST(NULL AS DOUBLE), CAST(NULL AS DOUBLE),
  CAST(NULL AS DOUBLE), CAST(NULL AS DOUBLE),
  CAST(NULL AS BIGINT), CAST(NULL AS DOUBLE), CAST(NULL AS DOUBLE),
  CAST(NULL AS STRING),  -- policy_id: cluster-only concept, no warehouse counterpart
  MAX(wm.tag_count)
FROM usage u
JOIN warehouse_query_users wqu
  ON wqu.warehouse_id = u.warehouse_id AND wqu.charge_month = u.charge_month
LEFT JOIN warehouse_meta wm ON wm.warehouse_id = u.warehouse_id
WHERE u.product = 'SQL' AND u.warehouse_id IS NOT NULL
GROUP BY u.warehouse_id, wqu.executed_by, u.charge_month
UNION ALL
-- SERVERLESS NOTEBOOKS — billing_origin_product IN ('INTERACTIVE','NOTEBOOKS') carries
-- real per-notebook identity (usage_metadata.notebook_id/notebook_path, already proven in
-- the sibling FOCUS query databricks_focus_1_3.sql:325,346) — unlike classic all-purpose
-- clusters, which bill at the cluster level with no notebook identity in billing.usage at
-- all (see the 'interactive' branch above). So billed_cost here is a real per-notebook
-- sum, not an allocation, and owner_user is the real per-record run_as directly — no
-- heaviest-user fallback needed. utilization_pct stays NULL — no per-entity CPU signal
-- exists for serverless notebooks either (same shared-compute limitation as sql_warehouse).
SELECT
  CAST(u.notebook_id AS STRING), 'notebook',
  COALESCE(MAX(u.notebook_path), CAST(u.notebook_id AS STRING)),
  u.run_as, MAX(u.project), CAST(u.charge_month AS DATE),
  SUM(u.cost), SUM(u.usage_quantity),
  CAST(NULL AS DOUBLE), CAST(NULL AS BIGINT),
  CAST(NULL AS BIGINT), CAST(NULL AS DOUBLE), CAST(NULL AS DOUBLE), BOOL_OR(u.photon),
  CAST(NULL AS BIGINT), CAST(NULL AS BIGINT), CAST(NULL AS BIGINT),
  CAST(NULL AS STRING), CAST(NULL AS DOUBLE), CAST(NULL AS STRING),
  CAST(NULL AS DOUBLE), CAST(NULL AS DOUBLE),
  CAST(NULL AS DOUBLE), CAST(NULL AS STRING), CAST(NULL AS STRING),
  CAST(NULL AS DOUBLE), CAST(NULL AS BIGINT),
  CAST(NULL AS STRING), CAST(NULL AS DOUBLE), CAST(NULL AS DOUBLE),
  SUM(u.jobs_priced_cost),
  CAST(NULL AS BIGINT), CAST(NULL AS DOUBLE), CAST(NULL AS DOUBLE),
  CAST(NULL AS DOUBLE), CAST(NULL AS DOUBLE),
  CAST(NULL AS BIGINT), CAST(NULL AS DOUBLE), CAST(NULL AS DOUBLE),
  -- Serverless notebooks have no cluster/warehouse identity to join for policy_id/tag_count.
  CAST(NULL AS STRING), CAST(NULL AS BIGINT)
FROM usage u
WHERE u.product IN ('INTERACTIVE', 'NOTEBOOKS') AND u.notebook_id IS NOT NULL
GROUP BY u.notebook_id, u.run_as, u.charge_month
