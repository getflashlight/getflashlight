-- SILVER: Total Cost of Ownership — the join that makes this product distinct.
--
-- A Databricks cluster's true monthly cost = its DBU spend (billed by Databricks)
-- PLUS the AWS infra (EC2/EBS/S3) that backs it (billed by AWS). The two are
-- complementary slices, but ONLY for CLASSIC compute:
--
--   * classic  → DBU + attributed AWS infra   (tco_basis = 'dbu_plus_infra')
--   * serverless → DBU only; adding AWS infra would DOUBLE COUNT
--                                              (tco_basis = 'serverless_inclusive')
--
-- Attribution links AWS rows to a cluster via the `ClusterId` tag (Databricks
-- tag propagation → AWS cost allocation tag). v1 assumes that tag key.

CREATE OR REPLACE VIEW silver.tco_resource_month AS
WITH databricks_cost AS (
    SELECT
        sub_account_id,
        resource_id                          AS cluster_id,
        charge_month,
        -- A cluster's compute class is stable; max() collapses the group safely.
        max(x_compute_class)                 AS compute_class,
        sum(cost)                            AS dbu_cost,
        bool_or(is_partial_period)           AS is_partial_period
    FROM silver.focus_normalized
    WHERE provider_name = 'Databricks'
      AND resource_id IS NOT NULL
      AND charge_category = 'Usage'
    GROUP BY sub_account_id, resource_id, charge_month
),
aws_attributed AS (
    SELECT
        json_extract_string(tags, '$.ClusterId')   AS cluster_id,
        charge_month,
        sum(cost)                                   AS infra_cost
    FROM silver.focus_normalized
    WHERE provider_name = 'AWS'
      AND coalesce(json_extract_string(tags, '$.ClusterId'), '') <> ''
    GROUP BY json_extract_string(tags, '$.ClusterId'), charge_month
)
SELECT
    d.sub_account_id,
    d.cluster_id,
    d.charge_month,
    d.compute_class,
    d.is_partial_period,
    d.dbu_cost,
    -- Double-count guard lives here.
    CASE WHEN d.compute_class = 'classic'
         THEN coalesce(a.infra_cost, 0) ELSE 0 END                       AS infra_cost,
    d.dbu_cost + CASE WHEN d.compute_class = 'classic'
                      THEN coalesce(a.infra_cost, 0) ELSE 0 END          AS tco_cost,
    CASE WHEN d.compute_class = 'classic'
         THEN 'dbu_plus_infra' ELSE 'serverless_inclusive' END           AS tco_basis
FROM databricks_cost d
LEFT JOIN aws_attributed a
       ON a.cluster_id = d.cluster_id
      AND a.charge_month = d.charge_month;


-- The honest "unattributed" bucket: AWS spend we could NOT tie to a cluster.
-- Surfaced in dashboards rather than hidden, so attribution never lies by omission.
CREATE OR REPLACE VIEW silver.tco_unattributed AS
SELECT
    charge_month,
    service_category,
    service_name,
    sum(cost)                                AS cost,
    bool_or(is_partial_period)               AS is_partial_period
FROM silver.focus_normalized
WHERE provider_name = 'AWS'
  AND coalesce(json_extract_string(tags, '$.ClusterId'), '') = ''
GROUP BY charge_month, service_category, service_name;
