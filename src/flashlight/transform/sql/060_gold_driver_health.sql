-- GOLD: client-driver fleet-health passthrough. ONE consumer view; the dashboard reads it.
--
-- Already aggregated at source (Databricks / Redshift / Snowflake driver-health queries
-- GROUP BY cluster × driver × application × user × month) — no further classification needed.
-- support_status is populated by the Snowflake connector via its reference table of
-- minimum supported versions (docs.snowflake.com/en/release-notes/requirements); NULL
-- for providers without automated version checking (e.g. Databricks). cluster_id is set
-- for Redshift provisioned clusters and NULL elsewhere.
CREATE OR REPLACE VIEW gold.driver_health AS
SELECT
    provider_name,
    strptime(charge_month, '%Y-%m')::date              AS charge_month,
    client_driver,
    client_application,
    executed_by,
    cluster_id,
    query_count,
    support_status
FROM raw.driver_health;
