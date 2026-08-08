-- GOLD: client-driver fleet-health passthrough. ONE consumer view; the dashboard reads it.
--
-- Already aggregated at source (databricks_driver_health.sql / snowflake_driver_health.sql
-- GROUP BY driver × application × user × month) — no further classification needed.
-- support_status is populated by the Snowflake connector via its reference table of
-- minimum supported versions (docs.snowflake.com/en/release-notes/requirements); NULL
-- for providers without automated version checking (e.g. Databricks).
CREATE OR REPLACE VIEW gold.driver_health AS
SELECT
    provider_name,
    strptime(charge_month, '%Y-%m')::date              AS charge_month,
    client_driver,
    client_application,
    executed_by,
    query_count,
    support_status
FROM metrics.driver_health;
