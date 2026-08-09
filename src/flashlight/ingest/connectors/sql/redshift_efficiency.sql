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
    SELECT DISTINCT s.query
    FROM svl_query_summary s
    JOIN q ON q.query = s.query
    WHERE s.is_diskbased = 't'
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
    SELECT w.total_queue_time, w.total_exec_time
    FROM stl_wlm_query w
    JOIN q ON q.query = w.query
    WHERE w.queue_start_time >= :start_date AND w.queue_start_time < :end_date
),
q_stats AS (
    SELECT count(*) AS query_count FROM q
),
spill_stats AS (
    SELECT count(*) AS disk_spill_query_count FROM spill
),
wlm_stats AS (
    -- Keep the two percentile calculations and wait/execute ratio in one
    -- aggregate over the already-scoped WLM rows.  The former scalar subqueries
    -- could each re-read/sort the same system-table result.
    SELECT
        percentile_cont(0.95) WITHIN GROUP (ORDER BY total_queue_time) AS wlm_queue_wait_us_p95,
        percentile_cont(0.99) WITHIN GROUP (ORDER BY total_queue_time) AS wlm_queue_wait_us_p99,
        avg(total_queue_time)::double precision / nullif(avg(total_exec_time), 0)
                                                                    AS wlm_wait_to_exec_ratio
    FROM wlm
)
SELECT
    q_stats.query_count,
    wlm_stats.wlm_queue_wait_us_p95,
    wlm_stats.wlm_queue_wait_us_p99,
    wlm_stats.wlm_wait_to_exec_ratio,
    spill_stats.disk_spill_query_count,
    coalesce(scaling.active_seconds, 0)                             AS concurrency_scaling_active_seconds,
    -- STL_QUERY's own retention floor (typically a handful of days, see the module
    -- docstring). The connector compares this against :start_date to tell "confirmed
    -- zero queries" apart from "the window predates what STL_QUERY still retains" —
    -- count(*) returns 0 either way, but only the first one is honestly "idle".
    (SELECT min(starttime) FROM stl_query)                          AS earliest_retained_query_ts
FROM q_stats
CROSS JOIN spill_stats
CROSS JOIN wlm_stats
LEFT JOIN scaling ON true;
