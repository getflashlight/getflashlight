-- Redshift efficiency aggregation — one row for the whole ingest window.
--
-- Provisioned-cluster system tables only (STL_*/SVL_*). Flashlight supports
-- provisioned Redshift clusters only.
--
-- NOT YET VALIDATED against a live cluster — column names follow AWS's published
-- system-table docs (docs.aws.amazon.com/redshift/latest/dg/c_intro_STL_tables.html).
-- Re-run `flashlight ingest` against a real cluster and spot-check cause_detail
-- before trusting this, same discipline as databricks_efficiency.sql.
--
-- :start_date / :end_date are substituted by the connector (inclusive window).
WITH q AS (
    SELECT query, starttime, endtime
    FROM stl_query
    WHERE starttime >= :start_date AND starttime < :end_date
      AND userid > 1  -- exclude bootstrap/system queries
),
spill AS (
    SELECT DISTINCT query
    FROM svl_query_summary
    WHERE is_diskbased = 't'
      AND query IN (SELECT query FROM q)
),
scaling AS (
    SELECT sum(datediff(seconds, start_time, end_time)) AS active_seconds
    FROM svcs_concurrency_scaling_usage
    WHERE start_time >= :start_date AND start_time < :end_date
),
wlm AS (
    -- Scoped to the same non-system query population as `q`/`spill` above (via the
    -- `query IN` join, mirroring `spill`'s own scoping) — STL_WLM_QUERY carries every
    -- queued statement including internal/bootstrap ones, which would otherwise dilute
    -- the percentile toward zero with near-instant system-queue entries that never
    -- reflect real user wait.
    SELECT total_queue_time, total_exec_time
    FROM stl_wlm_query
    WHERE queue_start_time >= :start_date AND queue_start_time < :end_date
      AND query IN (SELECT query FROM q)
)
SELECT
    (SELECT count(*) FROM q)                                       AS query_count,
    (SELECT percentile_cont(0.95) WITHIN GROUP (ORDER BY total_queue_time) FROM wlm)
                                                                    AS wlm_queue_wait_us_p95,
    (SELECT percentile_cont(0.99) WITHIN GROUP (ORDER BY total_queue_time) FROM wlm)
                                                                    AS wlm_queue_wait_us_p99,
    -- avg queue time / avg exec time — how much of a query's wall time is spent waiting
    -- on a WLM slot vs. actually running, mirrors the runbook's own
    -- admin.v_wlm_queue_queries_stats_agg_day_h "avg_wait_to_exec_ratio".
    (SELECT avg(total_queue_time)::double precision
            / nullif(avg(total_exec_time), 0) FROM wlm)
                                                                    AS wlm_wait_to_exec_ratio,
    (SELECT count(*) FROM spill)                                   AS disk_spill_query_count,
    coalesce((SELECT active_seconds FROM scaling), 0)              AS concurrency_scaling_active_seconds,
    -- STL_QUERY's own retention floor (typically a handful of days, see the module
    -- docstring). The connector compares this against :start_date to tell "confirmed
    -- zero queries" apart from "the window predates what STL_QUERY still retains" —
    -- count(*) returns 0 either way, but only the first one is honestly "idle".
    (SELECT min(starttime) FROM stl_query)                         AS earliest_retained_query_ts;
