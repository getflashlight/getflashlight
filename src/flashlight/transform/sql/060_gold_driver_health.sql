-- GOLD: client-driver fleet-health passthrough. ONE consumer view; the dashboard reads it.
--
-- Already aggregated at source (the Databricks/Redshift driver-health queries GROUP by
-- driver × application × user × month) — no further classification needed, unlike
-- waste_record. Reads local typed Bronze only; GOLD never queries either provider.
-- No dollar figure, no automated "stale version" verdict: humans read the leaderboard.
CREATE OR REPLACE VIEW gold.driver_health AS
SELECT
    provider_name,
    strptime(charge_month, '%Y-%m')::date              AS charge_month,
    client_driver,
    client_application,
    executed_by,
    query_count
FROM raw.driver_health;
