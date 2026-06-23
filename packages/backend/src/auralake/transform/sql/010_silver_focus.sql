-- SILVER: cleaned, analysis-ready view over BRONZE raw.focus_record.
--
-- FOCUS-handling rules enforced here so no downstream view has to remember them:
--   * EffectiveCost is the canonical metric (post-discount, amortized). The other
--     cost columns are carried but views must not sum across metrics.
--   * Aggregation grain is the CHARGE period (additive); never the billing period.
--   * Credits/refunds keep their sign; `is_credit` lets views offer gross vs net.
--   * `is_partial_period` flags the in-progress billing month so trends don't show
--     a fake cliff at the current edge.

CREATE OR REPLACE VIEW silver.focus_normalized AS
SELECT
    f.id,
    f.provider_name,
    f.billing_account_id,
    f.sub_account_id,
    f.billing_currency,
    -- Canonical cost metric for all downstream aggregation.
    f.effective_cost                                   AS cost,
    f.billed_cost,
    f.list_cost,
    f.contracted_cost,
    f.charge_category,
    f.charge_class,
    f.service_category,
    f.service_name,
    f.sku_id,
    f.region_id,
    f.resource_id,
    f.resource_name,
    f.resource_type,
    f.consumed_quantity,
    f.consumed_unit,
    f.tags,
    f.x_compute_class,
    f.x_source_connector,
    f.x_effective_is_list,
    f.charge_period_start,
    f.charge_period_end,
    -- Derived dimensions.
    date_trunc('day', f.charge_period_start)::date     AS charge_day,
    date_trunc('month', f.charge_period_start)::date   AS charge_month,
    (f.charge_category = 'Credit')                     AS is_credit,
    (f.charge_class = 'Correction')                    AS is_correction,
    -- True when the row's month is the current (still-accruing) calendar month.
    (date_trunc('month', f.charge_period_start)
        = date_trunc('month', CURRENT_DATE))           AS is_partial_period
FROM raw.focus_record f;
