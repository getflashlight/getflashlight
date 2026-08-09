-- Proposed BRONZE extract: daily Redshift Spectrum external-query efficiency.
--
-- SYS_EXTERNAL_QUERY_DETAIL gives segment-level facts.  Persist this daily because
-- system-view history is finite.  This query deliberately retains no query text;
-- query text may carry sensitive literals and is not necessary for the initial
-- table/path, partition-pruning, file-count, and catalog/listing diagnostics.
--
-- `returned_bytes` means bytes scanned for source_type='S3', per AWS
-- documentation.  It is therefore named source_bytes here rather than being
-- presented as result bytes.
--
-- Substituted by the connector: :start_date / :end_date.  Half-open interval.
SELECT
    CAST(:start_date AS date) AS usage_date,
    d.source_type,
    NULLIF(TRIM(d.table_name), '') AS external_table,
    NULLIF(TRIM(d.file_location), '') AS file_location,
    NULLIF(TRIM(d.file_format), '') AS file_format,
    COUNT(DISTINCT d.query_id) AS query_count,
    COUNT(*) AS segment_count,
    SUM(COALESCE(d.duration, 0)) / 1000000.0 AS duration_seconds,
    SUM(COALESCE(d.total_partitions, 0)) AS total_partitions,
    SUM(COALESCE(d.qualified_partitions, 0)) AS qualified_partitions,
    SUM(COALESCE(d.scanned_files, 0)) AS scanned_files,
    SUM(COALESCE(d.returned_rows, 0)) AS source_rows,
    SUM(COALESCE(d.returned_bytes, 0)) AS source_bytes,
    SUM(COALESCE(d.s3list_time, 0)) AS s3_listing_milliseconds,
    SUM(COALESCE(d.get_partition_time, 0)) AS partition_catalog_milliseconds
FROM sys_external_query_detail AS d
WHERE d.start_time >= :start_date
  AND d.start_time < :end_date
  AND d.source_type = 'S3'
GROUP BY 1, 2, 3, 4, 5;
