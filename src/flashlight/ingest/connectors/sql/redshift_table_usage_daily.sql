-- Proposed BRONZE extract: durable Redshift internal-table access facts.
--
-- Execute once per day and persist the result before STL_SCAN retention rolls off.
-- A zero-row result is meaningful only when it is joined to the same day's full
-- SVV_TABLE_INFO inventory snapshot: it means the table was present but was not
-- scanned during this measurement window.
--
-- Substituted by the connector: :start_date / :end_date.  The interval is
-- half-open [start_date, end_date), so a daily run should use consecutive UTC
-- midnights and never double-count a scan at the boundary.
--
-- Provisioned clusters only.  STL_SCAN excludes concurrency-scaling queries;
-- this is a main-cluster access history, not a complete cost allocation.
SELECT
    CAST(:start_date AS date) AS snapshot_date,
    s.tbl AS table_id,
    COUNT(DISTINCT s.query) AS query_count,
    COUNT(*) AS scan_step_count,
    SUM(GREATEST(COALESCE(s.bytes, 0), 0)) AS scan_bytes,
    SUM(GREATEST(COALESCE(s.rows_pre_filter, 0), 0)) AS rows_pre_filter,
    SUM(GREATEST(COALESCE(s.rows, 0), 0)) AS rows_returned,
    MIN(s.starttime) AS first_scan_at,
    MAX(s.starttime) AS last_scan_at
FROM stl_scan AS s
WHERE s.userid > 1
  AND s.starttime >= :start_date
  AND s.starttime < :end_date
  AND s.perm_table_name NOT IN ('Internal Worktable', 'S3', 'Runtime Filter')
GROUP BY 1, 2;
