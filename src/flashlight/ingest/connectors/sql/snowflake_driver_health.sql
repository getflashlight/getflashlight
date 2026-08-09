-- Snowflake ACCOUNT_USAGE -> DriverHealthRecord aggregation (fleet-health plane).
--
-- One row per (client_driver, user, month), aggregated AT SOURCE — a client-fleet
-- visibility signal. Joins QUERY_HISTORY to SESSIONS for CLIENT_APPLICATION_ID which
-- carries the driver name + version (e.g. "PythonConnector 4.7.1", "Go 2.0.2").
-- Substituted by the connector: %(start)s, %(end)s, {database}.
SELECT
  'Snowflake'                                         AS provider_name,
  DATE_TRUNC('MONTH', q.START_TIME)                   AS charge_month,
  s.CLIENT_APPLICATION_ID                             AS client_driver,
  NULL                                                AS client_application,
  q.USER_NAME                                         AS executed_by,
  COUNT(*)                                            AS query_count
FROM {database}.ACCOUNT_USAGE.QUERY_HISTORY q
JOIN {database}.ACCOUNT_USAGE.SESSIONS s
  ON q.SESSION_ID = s.SESSION_ID
WHERE q.START_TIME >= %(start)s::TIMESTAMP_LTZ
  AND q.START_TIME < DATEADD('day', 1, %(end)s::DATE)
  AND s.CLIENT_APPLICATION_ID IS NOT NULL
GROUP BY s.CLIENT_APPLICATION_ID, q.USER_NAME, DATE_TRUNC('MONTH', q.START_TIME)
