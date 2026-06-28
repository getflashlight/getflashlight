-- Databricks System Tables -> EfficiencyRecord aggregation (the waste plane).
--
-- Emits ONE row per (entity x month), aggregated AT SOURCE, for the efficiency/waste
-- GOLD view. Reuses the same warehouse + account-prices table as the FOCUS pull, so
-- billed_cost reconciles to FOCUS. Run as a single statement (no internal `;`).
--
-- Substituted by the connector: :account_prices (price table), :start_date, :end_date.
--
-- VALIDATED against a live warehouse 2026-06-28: all columns/struct fields below confirmed
-- (pricing.default, usage_metadata.*, identity_metadata.run_as, job_run_timeline.result_state,
-- node_timeline.cpu_*), and billing_origin_product values include JOBS / ALL_PURPOSE / SQL /
-- MODEL_SERVING. Re-validate if pointed at a different account/region/release.
-- Utilization here is the cluster's monthly avg attributed to the workload — the precise
-- version overlaps node_timeline with each run window (cause_detail.pct_runs_underutilized),
-- a later enrichment. ponytail: cluster-month avg is the tractable approximation; upgrade to
-- per-run overlap when the signal needs it.

WITH prices AS (
  SELECT
    sku_name,
    cloud,
    CAST(pricing.default AS DOUBLE)                          AS unit_price,
    price_start_time,
    COALESCE(price_end_time, date_add(current_date, 1))      AS price_end_time
  FROM IDENTIFIER(:account_prices)
  WHERE currency_code = 'USD'
),
usage AS (
  SELECT
    u.usage_metadata.job_id                                  AS job_id,
    u.usage_metadata.cluster_id                              AS cluster_id,
    u.usage_metadata.warehouse_id                            AS warehouse_id,
    u.identity_metadata.run_as                               AS run_as,
    u.billing_origin_product                                 AS product,
    u.sku_name                                               AS sku_name,
    element_at(u.custom_tags, 'project')                     AS project,
    date_trunc('MONTH', u.usage_date)                        AS charge_month,
    u.usage_quantity                                         AS usage_quantity,
    u.usage_quantity * COALESCE(p.unit_price, 0)             AS cost,
    (UPPER(u.sku_name) LIKE '%PHOTON%')                      AS photon
  FROM system.billing.usage u
  LEFT JOIN prices p
    ON u.sku_name = p.sku_name AND u.cloud = p.cloud
   AND u.usage_end_time >= p.price_start_time
   AND u.usage_end_time <  p.price_end_time
  WHERE u.usage_date BETWEEN :start_date AND :end_date
),
util AS (
  SELECT
    cluster_id,
    date_trunc('MONTH', start_time)                          AS charge_month,
    AVG(cpu_user_percent + cpu_system_percent)               AS avg_cpu,
    AVG(mem_used_percent)                                    AS avg_mem
  FROM system.compute.node_timeline
  WHERE start_time >= :start_date AND start_time < :end_date + INTERVAL 1 DAY
  GROUP BY cluster_id, date_trunc('MONTH', start_time)
),
runs AS (
  SELECT
    job_id,
    date_trunc('MONTH', period_start_time)                  AS charge_month,
    COUNT(*)                                                AS run_count,
    SUM(CASE WHEN result_state IN ('ERROR','FAILED','TIMED_OUT') THEN 1 ELSE 0 END)
                                                           AS failed_runs
  FROM system.lakeflow.job_run_timeline
  WHERE period_start_time >= :start_date AND period_start_time < :end_date + INTERVAL 1 DAY
  GROUP BY job_id, date_trunc('MONTH', period_start_time)
)
-- JOBS / DLT — per-job utilization (cluster avg), run count, failed-run cost
SELECT
  CAST(u.job_id AS STRING)                                  AS entity_id,
  'job'                                                     AS entity_type,
  CAST(u.job_id AS STRING)                                  AS entity_name,
  u.run_as                                                  AS owner_user,
  MAX(u.project)                                            AS owner_project,
  CAST(u.charge_month AS DATE)                              AS charge_month,
  SUM(u.cost)                                               AS billed_cost,
  SUM(u.usage_quantity)                                     AS native_quantity,
  LEAST(100, AVG(GREATEST(ut.avg_cpu, ut.avg_mem)))         AS utilization_pct,
  MAX(r.run_count)                                          AS activity_count,
  MAX(r.run_count)                                          AS run_count,
  CAST(NULL AS DOUBLE)                                      AS pct_runs_underutilized,
  SUM(u.cost * (CASE WHEN r.run_count > 0
                     THEN r.failed_runs / r.run_count ELSE 0 END))  AS failed_cost,
  BOOL_OR(u.photon)                                         AS photon
FROM usage u
LEFT JOIN util ut ON ut.cluster_id = u.cluster_id AND ut.charge_month = u.charge_month
LEFT JOIN runs r  ON r.job_id      = u.job_id     AND r.charge_month  = u.charge_month
WHERE u.product = 'JOBS' AND u.job_id IS NOT NULL
GROUP BY u.job_id, u.run_as, u.charge_month
UNION ALL
-- ALL-PURPOSE (interactive) — grain is the CLUSTER (the actionable unit). Cluster-level
-- utilization is honest at this grain (it describes the cluster, not a user). owner_user
-- is the heaviest user, a best-effort hint. Catches underutilized/idle all-purpose
-- clusters (a top waste vector) AND is a placement candidate downstream.
SELECT
  CAST(u.cluster_id AS STRING), 'interactive', CAST(u.cluster_id AS STRING),
  max_by(u.run_as, u.cost), max_by(u.project, u.cost), CAST(u.charge_month AS DATE),
  SUM(u.cost), SUM(u.usage_quantity),
  LEAST(100, AVG(GREATEST(ut.avg_cpu, ut.avg_mem))),
  CAST(NULL AS BIGINT), CAST(NULL AS BIGINT), CAST(NULL AS DOUBLE),
  CAST(NULL AS DOUBLE), BOOL_OR(u.photon)
FROM usage u
LEFT JOIN util ut ON ut.cluster_id = u.cluster_id AND ut.charge_month = u.charge_month
WHERE u.product = 'ALL_PURPOSE' AND u.cluster_id IS NOT NULL
GROUP BY u.cluster_id, u.charge_month
UNION ALL
-- SQL WAREHOUSE — shared: cost-attributable; no per-entity utilization (NULL)
SELECT
  CAST(u.warehouse_id AS STRING), 'sql_warehouse', CAST(u.warehouse_id AS STRING),
  u.run_as, MAX(u.project), CAST(u.charge_month AS DATE),
  SUM(u.cost), SUM(u.usage_quantity),
  CAST(NULL AS DOUBLE), CAST(NULL AS BIGINT), CAST(NULL AS BIGINT),
  CAST(NULL AS DOUBLE), CAST(NULL AS DOUBLE), BOOL_OR(u.photon)
FROM usage u
WHERE u.product = 'SQL' AND u.warehouse_id IS NOT NULL
GROUP BY u.warehouse_id, u.run_as, u.charge_month
