-- GOLD: the metrics contract. Grafana and the MCP server read ONLY these views,
-- never raw/silver — so dashboards and agents always return identical numbers.
--
-- These are MATERIALIZED views: the ingest pipeline refreshes them after BRONZE is
-- updated (REFRESH MATERIALIZED VIEW CONCURRENTLY), so dashboards read precomputed
-- data without recomputing joins on every query. Each has a UNIQUE index so the
-- concurrent refresh works. Created WITH DATA on first run; `auralake-transform
-- --rebuild` drops + recreates them when a definition here changes.
--
-- Cost rules carried from SILVER: the single canonical metric is `cost`
-- (= EffectiveCost), which already nets Databricks corrections (RETRACTION rows have
-- negative cost and cancel their ORIGINAL). Never sum across FOCUS cost columns.
-- `net_cost` = credits applied; `gross_cost` = usage/purchase only.

-- ── "What is my monthly bill?" — headline spend per provider per month ──────────
CREATE MATERIALIZED VIEW IF NOT EXISTS gold.monthly_bill AS
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

CREATE UNIQUE INDEX IF NOT EXISTS uq_gold_monthly_bill
    ON gold.monthly_bill (provider_name, charge_month);


-- ── "Where is the money going?" — by service / product ──────────────────────────
CREATE MATERIALIZED VIEW IF NOT EXISTS gold.spend_by_service_month AS
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

CREATE UNIQUE INDEX IF NOT EXISTS uq_gold_spend_by_service_month
    ON gold.spend_by_service_month (provider_name, service_category, service_name, charge_month);


-- ── "Where is the money going?" — by SKU (with consumed quantity, e.g. DBUs) ─────
CREATE MATERIALIZED VIEW IF NOT EXISTS gold.spend_by_sku_month AS
SELECT
    provider_name,
    service_name,
    coalesce(sku_id, '(unknown)')                        AS sku_id,
    charge_month,
    sum(cost)                                            AS net_cost,
    sum(cost) FILTER (WHERE NOT is_credit)               AS gross_cost,
    sum(consumed_quantity)                               AS consumed_quantity,
    max(consumed_unit)                                   AS consumed_unit,
    bool_or(is_partial_period)                           AS is_partial_period
FROM silver.focus_normalized
GROUP BY provider_name, service_name, coalesce(sku_id, '(unknown)'), charge_month;

CREATE UNIQUE INDEX IF NOT EXISTS uq_gold_spend_by_sku_month
    ON gold.spend_by_sku_month (provider_name, service_name, sku_id, charge_month);


-- ── "Where is the money going?" — by workspace / sub-account ─────────────────────
CREATE MATERIALIZED VIEW IF NOT EXISTS gold.spend_by_workspace_month AS
SELECT
    provider_name,
    coalesce(sub_account_id, '(none)')                   AS sub_account_id,
    charge_month,
    sum(cost)                                            AS net_cost,
    sum(cost) FILTER (WHERE NOT is_credit)               AS gross_cost,
    bool_or(is_partial_period)                           AS is_partial_period
FROM silver.focus_normalized
GROUP BY provider_name, coalesce(sub_account_id, '(none)'), charge_month;

CREATE UNIQUE INDEX IF NOT EXISTS uq_gold_spend_by_workspace_month
    ON gold.spend_by_workspace_month (provider_name, sub_account_id, charge_month);


-- ── "Where is the money going?" — by tag (cost allocation: team/env/etc.) ────────
-- Explodes the Tags map; rows with no tags don't appear here (they're covered by the
-- service/sku views). One row per (tag_key, tag_value) so any allocation tag works.
CREATE MATERIALIZED VIEW IF NOT EXISTS gold.spend_by_tag_month AS
SELECT
    t.key                                                AS tag_key,
    t.value                                              AS tag_value,
    provider_name,
    charge_month,
    sum(cost)                                            AS net_cost,
    bool_or(is_partial_period)                           AS is_partial_period
FROM silver.focus_normalized f
CROSS JOIN LATERAL jsonb_each_text(f.tags) AS t(key, value)
GROUP BY t.key, t.value, provider_name, charge_month;

CREATE UNIQUE INDEX IF NOT EXISTS uq_gold_spend_by_tag_month
    ON gold.spend_by_tag_month (tag_key, tag_value, provider_name, charge_month);


-- ── "Am I realizing my negotiated discount?" — list vs effective ─────────────────
CREATE MATERIALIZED VIEW IF NOT EXISTS gold.savings_summary_month AS
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

CREATE UNIQUE INDEX IF NOT EXISTS uq_gold_savings_summary_month
    ON gold.savings_summary_month (provider_name, charge_month);


-- ── Daily spend trend per provider — drives the time-series panels ───────────────
CREATE MATERIALIZED VIEW IF NOT EXISTS gold.spend_trend_daily AS
SELECT
    charge_day,
    provider_name,
    sum(cost)                                            AS net_cost,
    sum(cost) FILTER (WHERE NOT is_credit)               AS gross_cost,
    bool_or(is_partial_period)                           AS is_partial_period
FROM silver.focus_normalized
GROUP BY charge_day, provider_name;

CREATE UNIQUE INDEX IF NOT EXISTS uq_gold_spend_trend_daily
    ON gold.spend_trend_daily (charge_day, provider_name);


-- ── TCO per Databricks cluster per month (DBU + attributed AWS infra) ────────────
CREATE MATERIALIZED VIEW IF NOT EXISTS gold.tco_by_cluster_month AS
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

CREATE UNIQUE INDEX IF NOT EXISTS uq_gold_tco_by_cluster_month
    ON gold.tco_by_cluster_month (charge_month, sub_account_id, cluster_id);


-- ── Monthly TCO rollup: total DBU vs infra vs the unattributed AWS bucket ────────
CREATE MATERIALIZED VIEW IF NOT EXISTS gold.tco_summary_month AS
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

CREATE UNIQUE INDEX IF NOT EXISTS uq_gold_tco_summary_month
    ON gold.tco_summary_month (charge_month);
