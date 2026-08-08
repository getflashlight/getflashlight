-- Redshift query-pattern aggregate — one row per repeated query shape (hashed, not
-- stored verbatim) for the ingest window, capped to the heaviest patterns by total
-- runtime. This is the runtime/spill/skew drill-down the cluster-level snapshot in
-- redshift_efficiency.sql can't give: that query answers "is the cluster spilling
-- overall", this one answers "which query pattern".
--
-- Provisioned-cluster system tables only (STL_*/SVL_*).
--
-- NOT YET VALIDATED against a live cluster — column names follow AWS's published
-- system-table docs (docs.aws.amazon.com/redshift/latest/dg/c_intro_STL_tables.html).
-- Re-run `flashlight ingest` against a real cluster and spot-check cause_detail
-- before trusting this, same discipline as redshift_efficiency.sql.
--
-- :start_date / :end_date / :min_duration_secs / :top_n are substituted by the connector.
-- min_duration_secs floors out trivial sub-second queries; top_n bounds cardinality — a
-- cluster can have thousands of distinct query shapes, this is a triage signal, not an
-- exhaustive audit (same bounded-pool reasoning as the table inventory query).
--
-- Performance design: SVL_QUERY_REPORT is the expensive step-level view.  Determine
-- the top patterns from STL_QUERY first, then join that view only for the queries in
-- those patterns.  Do not move the LIMIT below the spill CTE: that would recreate a
-- full retained-history SVL_QUERY_REPORT scan for every telemetry pull.
WITH base_queries AS (
    SELECT
        query,
        userid,
        starttime,
        endtime,
        datediff(seconds, starttime, endtime) AS duration_secs,
        CASE
            WHEN userid = 102 AND querytxt LIKE '-- Looker Query Context%'
                THEN MD5(TRIM(SUBSTRING(querytxt, 144, 160)))
            ELSE MD5(TRIM(SUBSTRING(querytxt, 1, 160)))
        END AS qry_md5
    FROM stl_query
    WHERE starttime >= :start_date AND starttime < :end_date
      AND userid > 1
      AND datediff(seconds, starttime, endtime) >= :min_duration_secs
),
top_patterns AS (
    SELECT
        qry_md5,
        count(*) AS run_count,
        sum(duration_secs) / 60.0 AS total_run_min
    FROM base_queries
    GROUP BY qry_md5
    ORDER BY sum(duration_secs) DESC
    LIMIT :top_n
),
candidate_queries AS (
    SELECT q.*
    FROM base_queries q
    JOIN top_patterns p ON p.qry_md5 = q.qry_md5
),
wlm AS (
    -- The same candidate set prevents a retained-history WLM scan.
    SELECT w.query, w.total_queue_time, w.total_exec_time
    FROM stl_wlm_query w
    JOIN candidate_queries q ON q.query = w.query
),
spill AS (
    SELECT
        r.query AS query,
        max(CASE WHEN is_diskbased = 't' THEN 1 ELSE 0 END)::double precision AS spilled,
        sum(CASE WHEN is_diskbased = 't' THEN bytes ELSE 0 END) / 1024.0 / 1024 / 1024
                                                                              AS spill_gb,
        sum(workmem) / 1024.0 / 1024 / 1024                                  AS workmem_gb,
        max(rows) AS max_rows,
        min(rows) AS min_rows,
        sum(rows) AS total_rows,
        count(rows) AS slices
    FROM svl_query_report r
    JOIN candidate_queries q ON q.query = r.query
    GROUP BY r.query
),
user_counts AS (
    SELECT
        q.qry_md5,
        u.usename,
        row_number() OVER (PARTITION BY q.qry_md5 ORDER BY count(*) DESC) AS rnk
    FROM candidate_queries q
    JOIN pg_user u ON u.usesysid = q.userid
    GROUP BY q.qry_md5, u.usename
)
SELECT
    q.qry_md5,
    max(pattern.run_count)                                         AS run_count,
    max(pattern.total_run_min)                                     AS total_run_min,
    avg(coalesce(wlm.total_exec_time, 0)) / 1000000.0 / 60          AS avg_exec_min,
    avg(coalesce(wlm.total_queue_time, 0)) / 1000000.0 / 60         AS avg_queue_min,
    avg(coalesce(spill.spilled, 0))                                 AS pct_runs_spilling,
    avg(coalesce(spill.spill_gb, 0))                                AS avg_disk_spill_gb,
    avg(coalesce(spill.workmem_gb, 0))                              AS avg_workmem_gb,
    avg(
        CASE WHEN coalesce(spill.total_rows, 0) > 0
             THEN (spill.max_rows - spill.min_rows)::double precision * spill.slices
                  / spill.total_rows
             ELSE 0 END
    )                                                                AS avg_skew_ratio,
    max(
        CASE WHEN coalesce(spill.total_rows, 0) > 0
             THEN (spill.max_rows - spill.min_rows)::double precision * spill.slices
                  / spill.total_rows
             ELSE 0 END
    )                                                                AS max_skew_ratio,
    avg(coalesce(spill.slices, 0))                                  AS avg_slices_in_use,
    max(uc.usename)                                                 AS top_user
FROM candidate_queries q
JOIN top_patterns pattern ON pattern.qry_md5 = q.qry_md5
LEFT JOIN wlm ON wlm.query = q.query
LEFT JOIN spill ON spill.query = q.query
LEFT JOIN user_counts uc ON uc.qry_md5 = q.qry_md5 AND uc.rnk = 1
GROUP BY q.qry_md5
ORDER BY total_run_min DESC
