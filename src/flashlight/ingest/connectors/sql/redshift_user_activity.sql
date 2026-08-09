-- Redshift per-user activity aggregate for the ingest window — one row per user.
-- Feeds sql_warehouse_user rows: exec_microseconds drives duration_share_pct (which
-- share of the cluster's execution time this user drove — reuses the existing
-- sql_warehouse_user_concentration waste rule with no new category needed), and the
-- other columns carry CPU/scan/spill pressure detail for the "which user" drill-down
-- the cluster-level snapshot in redshift_efficiency.sql can't give.
--
-- Provisioned-cluster system tables only (STL_*/SVL_*).
--
-- NOT YET VALIDATED against a live cluster — column names follow AWS's published
-- SVL_QUERY_METRICS_SUMMARY docs. Re-run `flashlight ingest` against a real cluster and
-- spot-check cause_detail before trusting this, same discipline as redshift_efficiency.sql.
--
-- :start_date / :end_date / :top_n are substituted by the connector (inclusive window;
-- top_n caps rows to the heaviest users by exec time — a cluster can have many distinct
-- DB logins (one per job/service account), this is a triage signal, not an exhaustive
-- audit, same bounded-pool reasoning as the table inventory and query-pattern queries.
-- total_exec_microseconds is a window aggregate over ALL users before the LIMIT is
-- applied, so duration_share_pct stays correct (share of the true cluster total) even
-- though only the top_n heaviest users are returned.
WITH q AS (
    SELECT query, userid, starttime, endtime
    FROM stl_query
    WHERE starttime >= :start_date AND starttime < :end_date AND userid > 1
),
spill AS (
    SELECT
        r.query AS query,
        r.userid AS userid,
        sum(CASE WHEN is_diskbased = 't' THEN bytes ELSE 0 END) / 1024.0 / 1024 / 1024
                                                                        AS spill_gb
    -- Scope the expensive step-level view to the requested query IDs before
    -- aggregating it.  This avoids grouping all retained query-report history.
    FROM svl_query_report r
    JOIN q ON q.query = r.query AND q.userid = r.userid
    GROUP BY r.query, r.userid
)
SELECT
    u.usename                          AS username,
    count(DISTINCT q.query)             AS query_count,
    sum(m.query_execution_time)         AS exec_microseconds,
    -- Grand total across ALL users, computed before LIMIT truncates the row set —
    -- a window aggregate over the grouped result, the standard "share of total"
    -- idiom (SUM(SUM(x)) OVER ()), not a nested aggregate call.
    sum(sum(m.query_execution_time)) OVER () AS total_exec_microseconds,
    sum(m.query_queue_time)             AS queue_microseconds,
    sum(m.query_cpu_time)               AS cpu_microseconds,
    sum(m.query_blocks_read)            AS blocks_read,
    sum(m.query_temp_blocks_to_disk)    AS temp_blocks_to_disk,
    sum(m.scan_row_count)               AS scan_rows,
    sum(m.spectrum_scan_row_count)      AS spectrum_scan_rows,
    sum(m.spectrum_scan_size_mb)        AS spectrum_scan_mb,
    sum(coalesce(spill.spill_gb, 0))    AS spill_gb
FROM q
JOIN svl_query_metrics_summary m
    ON m.query = q.query AND m.userid = q.userid
JOIN pg_user u
    ON u.usesysid = q.userid
LEFT JOIN spill
    ON spill.query = q.query AND spill.userid = q.userid
GROUP BY u.usename
ORDER BY exec_microseconds DESC
LIMIT :top_n;
