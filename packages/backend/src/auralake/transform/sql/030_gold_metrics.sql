-- GOLD: the metrics contract. Grafana and the MCP server read ONLY these views,
-- never raw/silver — so dashboards and agents always return identical numbers.
--
-- Every view exposes `net_cost` (credits applied) and `gross_cost` (usage/purchase
-- only) so consumers choose the lens without re-deriving credit logic. All sums use
-- the single canonical `cost` (= EffectiveCost). `is_partial_period` is preserved.

-- Spend by provider/service/month — the top-level breakdown.
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
    -- True if any cost here is at list rates (no negotiated price) — not discounted.
    bool_or(x_effective_is_list)                          AS effective_is_list,
    max(billing_currency)                                AS currency
FROM silver.focus_normalized
GROUP BY provider_name, service_category, service_name, charge_month;


-- Daily spend trend per provider — drives the time-series panels.
CREATE OR REPLACE VIEW gold.spend_trend_daily AS
SELECT
    charge_day,
    provider_name,
    sum(cost)                                            AS net_cost,
    sum(cost) FILTER (WHERE NOT is_credit)               AS gross_cost,
    bool_or(is_partial_period)                           AS is_partial_period
FROM silver.focus_normalized
GROUP BY charge_day, provider_name;


-- TCO per Databricks cluster per month (DBU + attributed AWS infra).
CREATE OR REPLACE VIEW gold.tco_by_cluster_month AS
SELECT
    charge_month,
    sub_account_id,
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


-- Monthly TCO rollup: total DBU vs infra vs the unattributed AWS bucket.
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
