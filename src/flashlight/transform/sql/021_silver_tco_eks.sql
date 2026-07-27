-- SILVER: EKS Total Cost of Ownership — deterministic cluster attribution.
--
-- An EKS cluster's true monthly cost = its control-plane charge PLUS the EC2/EBS
-- that backs its worker nodes. Crucially, the node attribution here is keyed on
-- tags AWS *itself* stamps, not on user tagging discipline:
--
--   * aws:eks:cluster-name              — AWS-generated on managed-node-group EC2/EBS
--   * kubernetes.io/cluster/<name>=owned — on self-managed / Karpenter capacity
--   * the control-plane line carries the cluster ARN in ResourceId (no tag needed)
--
-- That makes EKS attribution *more* deterministic than the Databricks ClusterId
-- join (which depends on the vendor propagating its own tag).
--
-- PREREQUISITE / ATTRIBUTION HONESTY: the AWS-generated node tags only reach the
-- FOCUS Tags column once activated as cost-allocation tags in the payer account.
-- Where a control-plane charge exists but no node spend resolves to it, node_cost
-- is 0 (surfaced, not hidden) — a signal those tags were not activated upstream.

CREATE OR REPLACE VIEW silver.tco_eks_resource_month AS
WITH control_plane AS (
    SELECT
        coalesce(
            json_extract_string(tags, '$."aws:eks:cluster-name"'),
            nullif(regexp_extract(resource_id, 'cluster/(.+)$', 1), '')
        )                                    AS cluster_name,
        charge_month,
        sum(cost)                            AS control_plane_cost,
        bool_or(is_partial_period)           AS is_partial_period
    FROM silver.focus_normalized
    WHERE provider_name = 'AWS'
      AND service_name = 'Amazon Elastic Kubernetes Service'
    GROUP BY 1, charge_month
),
node_rows AS (
    SELECT
        f.charge_month,
        f.service_name,
        f.cost,
        f.is_partial_period,
        coalesce(
            json_extract_string(f.tags, '$."aws:eks:cluster-name"'),
            json_extract_string(f.tags, '$."eks:cluster-name"'),
            (SELECT nullif(regexp_extract(k, 'kubernetes\.io/cluster/(.+)$', 1), '')
               FROM unnest(json_keys(f.tags)) AS t(k)
              WHERE k LIKE 'kubernetes.io/cluster/%'
                AND json_extract_string(f.tags, '$."' || k || '"') = 'owned'
              LIMIT 1)
        )                                    AS cluster_name
    FROM silver.focus_normalized f
    WHERE f.provider_name = 'AWS'
      AND f.service_name IN (
          'Amazon Elastic Compute Cloud - Compute',
          'Amazon Elastic Block Store'
      )
),
nodes AS (
    SELECT
        cluster_name,
        charge_month,
        sum(cost) FILTER (WHERE service_name = 'Amazon Elastic Compute Cloud - Compute')
                                             AS node_ec2_cost,
        sum(cost) FILTER (WHERE service_name = 'Amazon Elastic Block Store')
                                             AS node_ebs_cost,
        sum(cost)                            AS node_cost,
        bool_or(is_partial_period)           AS is_partial_period
    FROM node_rows
    WHERE cluster_name IS NOT NULL
    GROUP BY cluster_name, charge_month
)
SELECT
    coalesce(c.cluster_name, n.cluster_name)             AS cluster_name,
    coalesce(c.charge_month, n.charge_month)             AS charge_month,
    coalesce(c.control_plane_cost, 0)                    AS control_plane_cost,
    coalesce(n.node_ec2_cost, 0)                         AS node_ec2_cost,
    coalesce(n.node_ebs_cost, 0)                         AS node_ebs_cost,
    coalesce(n.node_cost, 0)                             AS node_cost,
    coalesce(c.control_plane_cost, 0) + coalesce(n.node_cost, 0)
                                                         AS eks_tco,
    coalesce(c.is_partial_period, n.is_partial_period, false)
                                                         AS is_partial_period
FROM control_plane c
FULL OUTER JOIN nodes n
       ON n.cluster_name = c.cluster_name
      AND n.charge_month = c.charge_month;
