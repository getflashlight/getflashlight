-- Proposed BRONZE extract: current external-table catalog and partition footprint.
--
-- One row per external table.  This is metadata only: the S3 URI is a prefix, not
-- a measurement of bytes or objects below it.  Join it to an S3 Inventory/Storage
-- Lens extract to obtain authoritative S3 footprint and lifecycle facts.
--
-- SVV_EXTERNAL_PARTITIONS.location is documented as truncated at 128 characters,
-- so sample_partition_location is diagnostic only and must not be used as a
-- canonical S3 prefix.
WITH partitions AS (
    SELECT
        schemaname,
        tablename,
        COUNT(*) AS partition_count,
        MIN(location) AS sample_partition_location
    FROM svv_external_partitions
    GROUP BY 1, 2
)
SELECT
    CURRENT_TIMESTAMP AS snapshot_at,
    e.redshift_database_name,
    e.schemaname AS external_schema,
    e.tablename AS external_table,
    e.tabletype,
    e.location AS table_location,
    e.input_format,
    e.output_format,
    e.serialization_lib,
    e.compressed,
    e.parameters AS table_parameters,
    COALESCE(p.partition_count, 0) AS partition_count,
    p.sample_partition_location
FROM svv_external_tables AS e
LEFT JOIN partitions AS p
  ON p.schemaname = e.schemaname
 AND p.tablename = e.tablename
ORDER BY e.schemaname, e.tablename;
