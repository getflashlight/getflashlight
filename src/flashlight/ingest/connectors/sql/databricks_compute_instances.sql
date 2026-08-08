-- Databricks System Tables -> ComputeInstanceRecord aggregation (backing-compute plane).
--
-- One row per (cluster_id, instance_id, month) — which cloud VM instance backed which
-- Databricks cluster, aggregated AT SOURCE from system.compute.node_timeline. Metadata
-- only (no billed_cost, no utilization) — see docs/design/backing-compute.md. Substituted
-- by the connector: :start_date, :end_date.
--
-- Scope, confirmed against Databricks' own docs (system.compute.node_timeline reference):
-- this table covers all-purpose, jobs, Lakeflow-pipeline and pipeline-maintenance CLASSIC
-- compute only — serverless SQL warehouses, serverless jobs and DLT serverless pipelines
-- have NO rows here at all (no customer-visible instance to report), so this map is a
-- floor on Databricks' cloud-compute footprint, never a ceiling. instance_id is the raw
-- cloud instance id (e.g. "i-1234a6c12a2681234" on AWS) — joined against the AWS FOCUS
-- bill's ResourceId in 066_gold_compute.sql. Retention on this table is ~90 days, so the
-- map is only as good as what was actually captured while a window was ingested; there is
-- no way to backfill an instance's history for a window Flashlight never pulled.
--
-- ANY_VALUE(driver)/ANY_VALUE(node_type): an instance's driver/worker role and node type
-- are fixed for its lifetime, so collapsing node_timeline's per-minute rows down to one
-- per (cluster_id, instance_id, month) is lossless, not a "pick a representative value"
-- approximation (contrast with the MAX()-based proxy signals in databricks_efficiency.sql,
-- which genuinely do need a representative-value choice).
--
-- cluster_meta: a bare cluster_id is not a readable grouping key on a dashboard, so this
-- joins system.compute.clusters for its name and owner — the identical pattern (and the
-- identical QUALIFY ROW_NUMBER() latest-row-per-cluster_id dedup) as databricks_efficiency.
-- sql's own cluster_meta CTE. LEFT JOIN, not INNER: a cluster old enough to have aged out
-- of system.compute.clusters (or one the token can't read) must not drop its cost rows
-- here — it just loses the readable name/owner, falling back to the bare id downstream
-- (066_gold_compute.sql).
--
-- job_meta reuses databricks_efficiency.sql's own owner-resolution logic verbatim:
-- run_as_user_name resolves run_as (human OR service principal) to a readable name in one
-- column — no separate service-principal directory exists in Databricks. A JOB-triggered
-- cluster's cluster_name follows Databricks' own "job-<job_id>-run-<run_id>[-name]"
-- convention (confirmed against live data), so the job_id needed to join job_meta is
-- already sitting in a column this query selects anyway — no extra system-table read.
-- COALESCE prefers the resolved name and falls back to the cluster's raw owned_by (itself
-- often just a user/service-principal ID, not an email — system.compute.clusters has no
-- richer field) only when no job_id parses (a non-job/interactive cluster) or job_meta has
-- no match: confirmed live that run_as_user_name is genuinely NULL for rows Databricks
-- emitted before ~November 2025, so this is a best-effort upgrade, not a guaranteed name.
WITH cluster_meta AS (
  SELECT cluster_id, cluster_name, owned_by
  FROM system.compute.clusters
  QUALIFY ROW_NUMBER() OVER (PARTITION BY cluster_id ORDER BY change_time DESC) = 1
),
job_meta AS (
  SELECT job_id, run_as_user_name
  FROM system.lakeflow.jobs
  QUALIFY ROW_NUMBER() OVER (PARTITION BY job_id ORDER BY change_time DESC) = 1
)
SELECT
  'Databricks'                                AS provider_name,
  date_trunc('MONTH', nt.start_time)          AS charge_month,
  nt.cluster_id,
  ANY_VALUE(cm.cluster_name)                  AS cluster_name,
  ANY_VALUE(COALESCE(jm.run_as_user_name, cm.owned_by)) AS owner_user,
  nt.instance_id,
  ANY_VALUE(nt.driver)                        AS is_driver,
  ANY_VALUE(nt.node_type)                     AS node_type
FROM system.compute.node_timeline nt
LEFT JOIN cluster_meta cm ON cm.cluster_id = nt.cluster_id
LEFT JOIN job_meta jm
  ON jm.job_id = regexp_extract(cm.cluster_name, '^job-([0-9]+)-run-', 1)
WHERE nt.start_time >= :start_date AND nt.start_time < :end_date + INTERVAL 1 DAY
GROUP BY nt.cluster_id, nt.instance_id, date_trunc('MONTH', nt.start_time)
