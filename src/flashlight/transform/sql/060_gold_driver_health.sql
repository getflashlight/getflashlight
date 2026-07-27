-- GOLD: client-driver fleet-health passthrough. ONE consumer view; the dashboard reads it.
--
-- Already aggregated at source (databricks_driver_health.sql GROUPs by driver ×
-- application × user × month) — no further classification needed, unlike waste_record.
-- No dollar figure, no automated "stale version" verdict: humans read the leaderboard.
CREATE OR REPLACE VIEW gold.driver_health AS
SELECT
    provider_name,
    strptime(charge_month, '%Y-%m')::date              AS charge_month,
    client_driver,
    client_application,
    executed_by,
    query_count
FROM metrics.driver_health;
