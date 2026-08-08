-- Proposed BRONZE extract: current external-table catalog.
--
-- One row per external table. The S3 URI is a prefix, not a measurement of bytes
-- or objects below it. Partition enumeration is deliberately excluded: walking
-- SVV_EXTERNAL_PARTITIONS can be expensive on a large Glue/Hive catalog and belongs
-- in a future, separately scheduled Glue inventory job.
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
    e.parameters AS table_parameters
FROM svv_external_tables AS e;
