-- Databricks System Tables -> DriverHealthRecord aggregation (fleet-health plane).
--
-- One row per (client_driver, client_application, executed_by, month), aggregated AT
-- SOURCE — a client-fleet visibility signal, not a waste/cost signal (no billed_cost,
-- no entity_type). Substituted by the connector: :start_date, :end_date.
--
-- VALIDATED against a live warehouse 2026-07-11 (via DESCRIBE TABLE + a 30-day sample):
-- system.query.history.client_driver carries driver name AND version together, e.g.
-- "DatabricksJDBCDriver, 2.7.1" (Retool), "DatabricksJDBCDriverOSS, 3.3.1" (Fivetran),
-- "PyDatabricksSqlConnector, 4.2.5" (Airflow/Monte Carlo/direct Python usage),
-- "DatabricksSqlExecApi, 2.0" (Databricks' own UI/CLI surfaces). client_application
-- names the integration/product (Fivetran, Monte Carlo, Tableau, Databricks Notebooks,
-- …). Both are top-level, populated columns — no struct nesting, unlike compute.*.
-- No "current version" reference exists in this data, so no staleness verdict is
-- computed here — humans read the (driver, application, user, query_count) leaderboard
-- and judge for themselves.
SELECT
  'Databricks'                                AS provider_name,
  date_trunc('MONTH', start_time)             AS charge_month,
  client_driver,
  client_application,
  executed_by,
  COUNT(*)                                    AS query_count
FROM system.query.history
WHERE start_time >= :start_date AND start_time < :end_date + INTERVAL 1 DAY
GROUP BY client_driver, client_application, executed_by, date_trunc('MONTH', start_time)
