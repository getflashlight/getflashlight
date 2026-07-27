-- Redshift Spectrum per-external-table scan aggregate for the ingest window — which
-- external table is actually driving the cluster's Spectrum scan spend, not just "the
-- cluster scanned $X this month" (the cluster-level redshift_spectrum_scan_cost finding).
-- SVL_S3QUERY_SUMMARY carries external_table_name directly, so this needs no query-text
-- parsing. scanned vs returned bytes give a pruning-efficiency ratio — a low returned/
-- scanned share is the "un-pruned scan" pattern the Spectrum remedy text asks the reader
-- to verify.
--
-- NOT YET VALIDATED against a live cluster — column names follow AWS's published
-- SVL_S3QUERY_SUMMARY docs. Re-run `flashlight ingest` against a real cluster and
-- spot-check cause_detail before trusting this, same discipline as the other Redshift
-- efficiency queries.
--
-- :start_date / :end_date are substituted by the connector (inclusive window).
SELECT
    external_table_name,
    count(DISTINCT query)                              AS scan_count,
    sum(s3_scanned_bytes) / 1024.0 / 1024 / 1024        AS scanned_gb,
    sum(s3query_returned_bytes) / 1024.0 / 1024 / 1024  AS returned_gb
FROM svl_s3query_summary
WHERE starttime >= :start_date AND starttime < :end_date
GROUP BY external_table_name
ORDER BY scanned_gb DESC;
