-- Redshift connection log -> DriverHealthRecord aggregation (fleet-health plane).
--
-- One row per (cluster, driver, application, database user, month), aggregated at
-- source. The configured cluster identifier is stamped by RedshiftConnector because
-- STL_CONNECTION_LOG is scoped to the one cluster being queried.
-- STL_CONNECTION_LOG's driver_version contains both the driver's family and version
-- (for example, "Redshift JDBC Driver 2.0.0.0"); application_name is the optional
-- client connection property.  Connection logs are superuser-visible and have the
-- same finite retention caveat as the other STL-based telemetry pulls.
--
-- Substituted by the connector: :start_date, :end_date.
SELECT
  'AWS'                                      AS provider_name,
  date_trunc('month', recordtime)::date      AS charge_month,
  NULLIF(trim(driver_version), '')           AS client_driver,
  NULLIF(trim(application_name), '')         AS client_application,
  NULLIF(trim(username), '')                 AS executed_by,
  COUNT(*)                                   AS query_count
FROM stl_connection_log
WHERE event = 'initiating session'
  AND recordtime >= :start_date
  -- DATEADD avoids Redshift interpreting a substituted bare date as an
  -- interval value (which caused the driver-health pull to fail on 2026-08-08).
  AND recordtime < DATEADD(day, 1, :end_date)
GROUP BY
  date_trunc('month', recordtime)::date,
  NULLIF(trim(driver_version), ''),
  NULLIF(trim(application_name), ''),
  NULLIF(trim(username), '')
