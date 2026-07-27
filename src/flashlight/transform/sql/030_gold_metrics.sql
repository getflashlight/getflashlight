-- GOLD: the metrics contract. The dashboard and the MCP server read ONLY these
-- views, never raw/silver — so charts and agents always return identical numbers.
--
-- These are plain DuckDB VIEWS. The transform runner materializes each to a zstd
-- Parquet file (COPY) after BRONZE is rebuilt; that COPY *is* the refresh. No
-- matviews, no REFRESH, no indexes — at single-user scale a full rebuild is
-- sub-second.
--
-- Cost rules carried from SILVER: the single canonical metric is `cost`
-- (= EffectiveCost), which already nets Databricks corrections (RETRACTION rows have
-- negative cost and cancel their ORIGINAL). Never sum across FOCUS cost columns.
-- `net_cost` = credits applied; `gross_cost` = usage/purchase only.

-- ── "What is my monthly bill?" — headline spend per provider per month ──────────
CREATE OR REPLACE VIEW gold.monthly_bill AS
SELECT
    provider_name,
    charge_month,
    sum(cost)                                            AS net_cost,
    sum(cost) FILTER (WHERE NOT is_credit)               AS gross_cost,
    sum(cost) FILTER (WHERE is_credit)                   AS credit_cost,
    sum(list_cost)                                       AS list_cost,
    sum(list_cost) - sum(cost)                           AS savings,
    bool_or(is_partial_period)                           AS is_partial_period,
    max(billing_currency)                                AS currency
FROM silver.focus_normalized
GROUP BY provider_name, charge_month;


-- ── "Where is the money going?" — by service / product ──────────────────────────
CREATE OR REPLACE VIEW gold.spend_by_service_month AS
SELECT
    provider_name,
    service_category,
    service_name,
    charge_month,
    sum(cost)                                            AS net_cost,
    sum(cost) FILTER (WHERE NOT is_credit)               AS gross_cost,
    sum(cost) FILTER (WHERE is_credit)                   AS credit_cost,
    bool_or(is_partial_period)                           AS is_partial_period,
    bool_or(x_effective_is_list)                         AS effective_is_list,
    max(billing_currency)                                AS currency
FROM silver.focus_normalized
GROUP BY provider_name, service_category, service_name, charge_month;


-- Databricks-only compute grouping, keyed on `service_name` — which for Databricks
-- carries `billing_origin_product` verbatim (databricks_focus_1_3.sql), a Databricks-
-- maintained enum, not a Flashlight-guessed one. Unmapped products (governance,
-- storage, networking, new AI SKUs, or any other provider's service_name) stay NULL
-- rather than being forced into a bucket — "not applicable", never miscategorized.
-- ponytail: add a WHEN here if a new Databricks compute product needs its own family.
CREATE OR REPLACE MACRO gold.compute_family(service_name) AS
    CASE service_name
        WHEN 'ALL_PURPOSE' THEN 'interactive'
        WHEN 'INTERACTIVE' THEN 'interactive'
        WHEN 'NOTEBOOKS' THEN 'interactive'
        WHEN 'SHARED_SERVERLESS_COMPUTE' THEN 'interactive'
        WHEN 'JOBS' THEN 'job'
        WHEN 'DLT' THEN 'job'
        WHEN 'SQL' THEN 'sql_warehouse'
        WHEN 'MODEL_SERVING' THEN 'endpoint'
    END;


-- ── "Where is the money going?" — by SKU (with consumed quantity, e.g. DBUs) ─────
CREATE OR REPLACE VIEW gold.spend_by_sku_month AS
SELECT
    provider_name,
    service_name,
    gold.compute_family(service_name)                    AS compute_family,
    coalesce(sku_id, '(unknown)')                        AS sku_id,
    -- A SkuId is opaque for some providers (e.g. AWS's Redshift SKUs — random-looking
    -- codes with no meaning on their own), so carry the human-readable pricing text
    -- too. The ARN AWS embeds in "Unused commitment for arn:aws:..." lines (RDS/
    -- ElastiCache/Redshift RIs, Savings Plans) is a per-reservation identifier, not
    -- part of the price description — stripped so the many reservations behind one
    -- SKU collapse to one description instead of arg_max picking whichever
    -- reservation's single line happened to cost the most that period.
    arg_max(
        regexp_replace(charge_description, 'arn:aws:\S+', 'a reservation'), cost
    )                                                      AS sku_description,
    charge_month,
    sum(cost)                                            AS net_cost,
    sum(cost) FILTER (WHERE NOT is_credit)               AS gross_cost,
    sum(consumed_quantity)                               AS consumed_quantity,
    max(consumed_unit)                                   AS consumed_unit,
    bool_or(is_partial_period)                           AS is_partial_period
FROM silver.focus_normalized
GROUP BY provider_name, service_name, coalesce(sku_id, '(unknown)'), charge_month;


-- ── "Where inside a service did the money go, below SKU granularity?" ───────────
-- Only populated where a connector stamps x_cost_subcategory (currently: Redshift
-- compute vs concurrency-scaling vs storage vs Spectrum scan vs serverless, derived
-- from AWS UsageType in aws_focus.py). Rows without a subcategory are absent here by
-- construction — reconcile against spend_by_service_month for the full total.
CREATE OR REPLACE VIEW gold.spend_by_cost_subcategory_month AS
SELECT
    provider_name,
    service_name,
    x_cost_subcategory                                   AS cost_subcategory,
    charge_month,
    sum(cost)                                            AS net_cost,
    bool_or(is_partial_period)                           AS is_partial_period
FROM silver.focus_normalized
WHERE x_cost_subcategory IS NOT NULL
GROUP BY provider_name, service_name, x_cost_subcategory, charge_month;


-- ── "Where exactly inside a SKU did the money land?" — resource-grain drill ──────
-- The finest consumer-facing grain: one row per (SKU, resource, resource_type,
-- workspace, region, month). Drives the dashboard drill-down from a SKU into the
-- individual resources moving its cost — e.g. a specific SQL warehouse. Carries
-- consumed_quantity so a mover can be read as *more usage* vs *higher rate*.
-- NOTE: consumed_quantity is the billable usage unit (DBUs for Databricks), NOT an
-- operation/query count — this billing data carries no operation counts.
CREATE OR REPLACE VIEW gold.resource_month AS
SELECT
    provider_name,
    service_name,
    gold.compute_family(service_name)                    AS compute_family,
    coalesce(sku_id, '(unknown)')                        AS sku_id,
    -- Same rationale as spend_by_sku_month.sku_description: an opaque SkuId (e.g.
    -- AWS Redshift) needs a human-readable label; the ARN in "Unused commitment
    -- for arn:aws:..." is a per-reservation identifier, not price description.
    arg_max(
        regexp_replace(charge_description, 'arn:aws:\S+', 'a reservation'), cost
    )                                                      AS sku_description,
    coalesce(resource_type, '(none)')                    AS resource_type,
    coalesce(resource_id, '(none)')                      AS resource_id,
    coalesce(resource_name, resource_id, '(unattributed)') AS resource_name,
    coalesce(sub_account_id, '(none)')                   AS sub_account_id,
    coalesce(region_id, '(none)')                        AS region_id,
    charge_month,
    sum(cost)                                            AS net_cost,
    sum(consumed_quantity)                               AS consumed_quantity,
    max(consumed_unit)                                   AS consumed_unit,
    bool_or(is_partial_period)                           AS is_partial_period
FROM silver.focus_normalized
GROUP BY provider_name, service_name, coalesce(sku_id, '(unknown)'),
         coalesce(resource_type, '(none)'), coalesce(resource_id, '(none)'),
         coalesce(resource_name, resource_id, '(unattributed)'),
         coalesce(sub_account_id, '(none)'), coalesce(region_id, '(none)'), charge_month;


-- ── "Which project/team does a SKU's spend belong to?" — SKU × tag drill ─────────
-- Crosses SKU with each cost-allocation tag so a SKU's movement can be attributed to
-- a project/team. Untagged spend is absent here by construction (rows with no tags
-- produce no tag row); the dashboard reconciles against spend_by_sku_month to surface
-- the unattributed remainder (attribution honesty — never silently dropped).
CREATE OR REPLACE VIEW gold.spend_by_sku_tag_month AS
SELECT
    f.provider_name,
    coalesce(f.sku_id, '(unknown)')                      AS sku_id,
    t.tag_key                                            AS tag_key,
    json_extract_string(f.tags, '$."' || t.tag_key || '"') AS tag_value,
    f.charge_month,
    sum(f.cost)                                          AS net_cost
FROM silver.focus_normalized f
CROSS JOIN unnest(json_keys(f.tags)) AS t(tag_key)
GROUP BY f.provider_name, coalesce(f.sku_id, '(unknown)'), t.tag_key, tag_value, f.charge_month;


-- ── "Where is the money going?" — by workspace / sub-account ─────────────────────
CREATE OR REPLACE VIEW gold.spend_by_workspace_month AS
SELECT
    provider_name,
    coalesce(sub_account_id, '(none)')                   AS sub_account_id,
    charge_month,
    sum(cost)                                            AS net_cost,
    sum(cost) FILTER (WHERE NOT is_credit)               AS gross_cost,
    bool_or(is_partial_period)                           AS is_partial_period
FROM silver.focus_normalized
GROUP BY provider_name, coalesce(sub_account_id, '(none)'), charge_month;


-- ── "Where is the money going?" — by tag (cost allocation: team/env/etc.) ────────
-- Explodes the Tags JSON; rows with no tags don't appear here (they're covered by
-- the service/sku views). One row per (tag_key, tag_value) so any allocation tag
-- works. json_keys + json_extract_string is DuckDB's stand-in for jsonb_each_text.
CREATE OR REPLACE VIEW gold.spend_by_tag_month AS
SELECT
    t.tag_key                                            AS tag_key,
    json_extract_string(f.tags, '$."' || t.tag_key || '"') AS tag_value,
    f.provider_name,
    f.charge_month,
    sum(f.cost)                                          AS net_cost,
    bool_or(f.is_partial_period)                         AS is_partial_period
FROM silver.focus_normalized f
CROSS JOIN unnest(json_keys(f.tags)) AS t(tag_key)
GROUP BY tag_key, tag_value, f.provider_name, f.charge_month;


-- ── "Am I realizing my negotiated discount?" — list vs effective ─────────────────
CREATE OR REPLACE VIEW gold.savings_summary_month AS
SELECT
    provider_name,
    charge_month,
    sum(list_cost)                                       AS list_cost,
    sum(cost)                                            AS effective_cost,
    sum(list_cost) - sum(cost)                           AS savings,
    CASE WHEN sum(list_cost) > 0
         THEN round(100 * (sum(list_cost) - sum(cost)) / sum(list_cost), 1)
         ELSE 0 END                                      AS savings_pct,
    bool_or(x_effective_is_list)                         AS effective_is_list,
    bool_or(is_partial_period)                           AS is_partial_period
FROM silver.focus_normalized
GROUP BY provider_name, charge_month;


-- ── Daily spend trend per provider — drives the time-series panels ───────────────
CREATE OR REPLACE VIEW gold.spend_trend_daily AS
SELECT
    charge_day,
    provider_name,
    sum(cost)                                            AS net_cost,
    sum(cost) FILTER (WHERE NOT is_credit)               AS gross_cost,
    bool_or(is_partial_period)                           AS is_partial_period
FROM silver.focus_normalized
GROUP BY charge_day, provider_name;


-- ── TCO per Databricks cluster per month (DBU + attributed AWS infra) ────────────
CREATE OR REPLACE VIEW gold.tco_by_cluster_month AS
SELECT
    charge_month,
    coalesce(sub_account_id, '(none)')                   AS sub_account_id,
    cluster_id,
    compute_class,
    tco_basis,
    dbu_cost,
    infra_cost,
    tco_cost,
    CASE WHEN tco_cost > 0 THEN round(100 * infra_cost / tco_cost, 1) ELSE 0 END
                                                         AS infra_pct_of_tco,
    is_partial_period
FROM silver.tco_resource_month;


-- ── EKS TCO per cluster per month (control plane + AWS-attributed node EC2/EBS) ──
-- Node spend is keyed on AWS-generated tags (aws:eks:cluster-name /
-- kubernetes.io/cluster/<name>); the control-plane line carries the cluster ARN.
-- nodes_attributed = false with control_plane_cost > 0 flags clusters whose node
-- tags were not activated as cost-allocation tags upstream (under-attribution
-- surfaced, not hidden).
CREATE OR REPLACE VIEW gold.tco_eks_by_cluster_month AS
SELECT
    charge_month,
    coalesce(cluster_name, '(unresolved)')               AS cluster_name,
    control_plane_cost,
    node_ec2_cost,
    node_ebs_cost,
    node_cost,
    eks_tco,
    (node_cost > 0)                                      AS nodes_attributed,
    is_partial_period
FROM silver.tco_eks_resource_month;


-- ── Monthly TCO rollup: total DBU vs infra vs the unattributed AWS bucket ────────
CREATE OR REPLACE VIEW gold.tco_summary_month AS
WITH attributed AS (
    SELECT charge_month,
           sum(dbu_cost)   AS dbu_cost,
           sum(infra_cost) AS infra_cost,
           sum(tco_cost)   AS tco_cost,
           bool_or(is_partial_period) AS is_partial_period
    FROM silver.tco_resource_month
    GROUP BY charge_month
),
unattributed AS (
    SELECT charge_month, sum(cost) AS unattributed_infra_cost
    FROM silver.tco_unattributed
    GROUP BY charge_month
)
SELECT
    coalesce(a.charge_month, u.charge_month)             AS charge_month,
    coalesce(a.dbu_cost, 0)                              AS dbu_cost,
    coalesce(a.infra_cost, 0)                            AS attributed_infra_cost,
    coalesce(u.unattributed_infra_cost, 0)              AS unattributed_infra_cost,
    coalesce(a.tco_cost, 0) + coalesce(u.unattributed_infra_cost, 0)
                                                         AS total_cost,
    coalesce(a.is_partial_period, false)                AS is_partial_period
FROM attributed a
FULL OUTER JOIN unattributed u ON u.charge_month = a.charge_month;


-- ── "What changed month-over-month, and why?" — per-SKU cost variance ────────────
-- Decomposes each SKU's cost change into VOLUME vs RATE so you can tell "more jobs
-- ran" from "the same jobs cost more per DBU":
--   cost_delta   = net_cost − prev_cost
--   volume_effect = Δquantity × prior unit rate          (consumption changed)
--   rate_effect   = cost_delta − volume_effect           (price/mix changed)
-- These two always sum to cost_delta. Rolled up to pure SKU (matches invoice lines).
CREATE OR REPLACE VIEW gold.sku_month_over_month AS
WITH base AS (
    SELECT
        provider_name,
        coalesce(sku_id, '(unknown)')                    AS sku_id,
        charge_month,
        sum(cost)                                        AS net_cost,
        sum(consumed_quantity)                           AS consumed_quantity,
        bool_or(is_partial_period)                       AS is_partial_period
    FROM silver.focus_normalized
    GROUP BY provider_name, coalesce(sku_id, '(unknown)'), charge_month
),
lagged AS (
    SELECT base.*,
        lag(net_cost) OVER w           AS prev_cost,
        lag(consumed_quantity) OVER w  AS prev_qty
    FROM base
    WINDOW w AS (PARTITION BY provider_name, sku_id ORDER BY charge_month)
)
SELECT
    provider_name,
    sku_id,
    charge_month,
    is_partial_period,
    net_cost,
    consumed_quantity,
    CASE WHEN consumed_quantity > 0 THEN net_cost / consumed_quantity END   AS unit_rate,
    prev_cost,
    net_cost - prev_cost                                                    AS cost_delta,
    CASE WHEN prev_cost > 0
         THEN round(100 * (net_cost - prev_cost) / prev_cost, 1) END        AS cost_pct_change,
    consumed_quantity - prev_qty                                            AS qty_delta,
    CASE WHEN prev_qty > 0
         THEN (consumed_quantity - prev_qty) * (prev_cost / prev_qty) END   AS volume_effect,
    CASE WHEN prev_cost IS NOT NULL
         THEN (net_cost - prev_cost)
              - CASE WHEN prev_qty > 0
                     THEN (consumed_quantity - prev_qty) * (prev_cost / prev_qty)
                     ELSE 0 END
    END                                                                     AS rate_effect
FROM lagged;
