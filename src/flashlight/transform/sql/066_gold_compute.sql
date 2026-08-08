-- GOLD: the cloud compute bill behind a Databricks cluster.
--
-- WHY THIS EXISTS: Databricks' FOCUS bill (system.billing.usage) covers DBU compute only.
-- For a CLASSIC (non-serverless) cluster, Databricks orchestrates the creation of the
-- underlying cloud VM on the customer's own cloud account, and that VM is billed
-- separately — by AWS, as EC2. "What does a Databricks cluster's EC2 footprint cost?" is
-- invisible in the Databricks plane. system.compute.node_timeline knows which EC2 instance
-- backed which cluster; the AWS FOCUS export knows what that instance costs. This joins
-- the two — the identical shape as 065_gold_storage.sql, for compute instead of storage.
--
-- WHAT COUNTS — CLASSIC COMPUTE ONLY, and this is a genuine coverage gap, not a filter:
-- node_timeline reports all-purpose, jobs, Lakeflow-pipeline and pipeline-maintenance
-- CLASSIC compute. Serverless SQL warehouses, serverless jobs and DLT serverless
-- pipelines have NO rows there at all — there is no customer-visible instance for
-- Databricks to report, so this figure is a FLOOR on Databricks' cloud-compute
-- footprint, never a ceiling, and the gap grows as serverless adoption grows.
--
-- THE INVARIANT (CLAUDE.md, "No cross-provider cost join"): this joins AWS **cost** to
-- Databricks **metadata** — an instance/cluster map. It never joins AWS cost to
-- Databricks cost, and nothing here writes into gold/databricks/, so
-- databricks.monthly_bill and the Databricks KPIs are untouched. Every row carries both
-- billing_provider_name (who invoices: AWS) and platform_provider_name (whose metadata
-- claims it: Databricks) precisely so a consumer can't mistake one for the other. The two
-- bills are reported side by side, never summed.
-- Provider-facing GOLD (silver.focus_provider_bill → aws.*) excludes Amazon Elastic
-- Compute Cloud entirely; mapped rows here are named `Databricks Compute` and are that
-- spend's only GOLD home.
--
-- HONESTY:
--   * The map is only as good as what was actually captured while a window was ingested —
--     node_timeline's retention is ~90 days, so there is no backfilling an instance's
--     history for a window Flashlight never pulled. Unlike backing storage's UC snapshot
--     (one present-tense map applied to every month of history), this join is PER MONTH:
--     an instance/cluster pairing is matched against the exact charge_month it actually
--     ran in, because node_timeline itself reports bounded historical activity, not
--     current state.
--   * A non-instance EC2 resource (an EBS volume, an Elastic IP, …) still carries a
--     ResourceId, so it reads 'unmapped', never 'no_resource_id' — that label is reserved
--     for a genuinely absent ResourceId (a cost_explorer-sourced AWS connection, same as
--     storage). Every EC2 row is kept (mapped, unmapped, or carrying no ResourceId at all)
--     so the mapped figure has a real denominator.
--
-- cluster_name/owner_user come from a second Databricks source joined at collection time
-- (system.compute.clusters, see databricks_compute_instances.sql) — a bare cluster_id is
-- not a readable grouping key on a dashboard. pricing_category is FOCUS's own column,
-- carried straight from the AWS bill with no Databricks-side join at all: DYNAMIC is
-- FOCUS's term for Spot (and other provider-variable pricing), COMMITTED means an
-- existing RI/Savings Plan discounted the charge, STANDARD is on-demand/negotiated-rate.

CREATE OR REPLACE VIEW gold.compute_instance AS
SELECT
    provider_name                                      AS platform_provider_name,
    strptime(charge_month, '%Y-%m')::date              AS charge_month,
    cluster_id,
    cluster_name,
    owner_user,
    instance_id,
    is_driver,
    node_type,
    x_source_connector
FROM metrics.compute_instance;


CREATE OR REPLACE VIEW gold.backing_compute_month AS
WITH instance_map AS (
    -- EXACTLY ONE ROW PER (instance_id, charge_month). Load-bearing, not tidiness: an
    -- EC2 instance id is never reused across clusters (AWS never reissues a terminated
    -- instance's id), so this is a safety net against a duplicate node_timeline pull —
    -- same multiply-counting guard as bucket_map in 065_gold_storage.sql, not an expected
    -- real case.
    SELECT
        instance_id,
        strptime(charge_month, '%Y-%m')::date          AS charge_month,
        any_value(provider_name)                       AS platform_provider_name,
        any_value(cluster_id)                          AS cluster_id,
        any_value(cluster_name)                        AS cluster_name,
        any_value(owner_user)                          AS owner_user,
        any_value(is_driver)                           AS is_driver,
        any_value(node_type)                           AS node_type,
        count(*)                                       AS mapping_row_count
    FROM metrics.compute_instance
    GROUP BY instance_id, charge_month
),
ec2_cost AS (
    SELECT
        provider_name,
        service_name,
        region_id,
        -- FOCUS's own pricing-model dimension: DYNAMIC is Spot (and other
        -- provider-variable pricing) on AWS, COMMITTED is RI/Savings-Plan-covered,
        -- STANDARD is on-demand/negotiated-rate. Never dropped to '(unclassified)'
        -- like a subcategory would be — NULL here is a real FOCUS value (absence of a
        -- pricing model, e.g. an older export that predates this column), not a gap in
        -- our own classification, so it's surfaced as its own bucket downstream.
        pricing_category,
        charge_month,
        cost,
        is_credit,
        is_partial_period,
        -- A real FOCUS export carries EC2's ResourceId as the instance ARN
        -- (arn:aws:ec2:region:account:instance/i-xxx); fall back to the raw ResourceId,
        -- which is what a bare instance id (or any other EC2-service resource, e.g. an
        -- EBS volume) already looks like. A non-instance resource id simply never matches
        -- anything in instance_map, so it reads 'unmapped' below, not 'no_resource_id' —
        -- that label is reserved for a genuinely absent ResourceId.
        nullif(
            coalesce(
                nullif(
                    regexp_extract(
                        resource_id, '^arn:aws:ec2:[^:]*:[^:]*:instance/([^/]+)', 1
                    ),
                    ''
                ),
                resource_id
            ),
            ''
        )                                              AS instance_id
    FROM silver.focus_normalized
    WHERE provider_name = 'AWS'
      AND service_name = 'Amazon Elastic Compute Cloud'
)
SELECT
    c.provider_name                                    AS billing_provider_name,
    -- Mapped rows are Databricks Compute at transform time — not Amazon Elastic Compute
    -- Cloud under AWS (provider GOLD excludes EC2 entirely; this plane is their only
    -- GOLD home).
    CASE WHEN m.instance_id IS NOT NULL THEN 'Databricks Compute' ELSE c.service_name END
                                                       AS service_name,
    coalesce(c.instance_id, '(no resource id)')        AS instance_id,
    CASE
        WHEN c.instance_id IS NULL     THEN 'no_resource_id'
        WHEN m.instance_id IS NOT NULL THEN 'databricks'
        ELSE 'unmapped'
    END                                                AS mapping,
    m.platform_provider_name,
    -- Which Databricks cluster this instance backed, or '(not managed)' for every
    -- unmapped row. Group by this to read compute cost per cluster. cluster_name/
    -- owner_user fall back to the cluster_id itself / '(unknown)' when
    -- system.compute.clusters has no row for this cluster_id (a cluster older than
    -- that table's own retention, or a token without cluster-read access) — the id
    -- alone is still a valid, if less readable, grouping key.
    coalesce(m.cluster_id, '(not managed)')            AS cluster_id,
    coalesce(m.cluster_name, m.cluster_id, '(not managed)') AS cluster_name,
    coalesce(m.owner_user, CASE WHEN m.cluster_id IS NOT NULL THEN '(unknown)' END)
                                                       AS owner_user,
    CASE
        WHEN m.is_driver IS TRUE  THEN 'driver'
        WHEN m.is_driver IS FALSE THEN 'worker'
        ELSE 'n/a'
    END                                                AS instance_role,
    coalesce(m.node_type, '(not managed)')             AS node_type,
    coalesce(c.region_id, '(none)')                    AS region_id,
    coalesce(c.pricing_category, '(unknown)')          AS pricing_category,
    c.charge_month,
    coalesce(max(m.mapping_row_count), 0)              AS mapping_row_count,
    sum(c.cost)                                        AS net_cost,
    sum(c.cost) FILTER (WHERE NOT c.is_credit)         AS gross_cost,
    bool_or(c.is_partial_period)                       AS is_partial_period
FROM ec2_cost c
LEFT JOIN instance_map m
    ON m.instance_id = c.instance_id AND m.charge_month = c.charge_month
GROUP BY 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13;
