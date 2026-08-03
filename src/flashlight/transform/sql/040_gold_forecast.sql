-- GOLD: forward-looking spend. Everything else in this layer reports what already
-- happened; this projects what the current month lands at, and roughly where the next
-- few months go.
--
-- Two deliberately simple methods, both plain SQL — no statistical dependency, nothing
-- an operator can't re-derive by hand:
--
--   run_rate  the current month's completed-day average, extended to the whole month.
--             This is the number a FinOps practitioner actually acts on mid-month.
--   trend     an ordinary least-squares line through daily spend (DuckDB's built-in
--             regr_slope/regr_intercept), summed over each of the next 3 months.
--
-- Honesty rules, same discipline as the waste plane — a forecast that quietly invents
-- confidence is worse than no forecast:
--
--   * The newest day in the lake is dropped from every calculation. Billing exports
--     land 24-48h late, so that day is nearly always partially delivered; counting it
--     as a complete day drags every projection down.
--   * `trend` is NULL until there are 60 complete days of history. On the default
--     35-day ingest lookback a regression line is fitted noise, so the view reports
--     no number rather than a bad one. `history_days` says why, and backfilling
--     (`flashlight ingest --start`, or FLASHLIGHT_INGEST_LOOKBACK_DAYS) fixes it.
--   * Negative projections are clamped to 0 — a downward slope extrapolates below zero
--     long before it means anything real.
--   * No seasonality, no confidence interval. Both need history this tool doesn't
--     assume it has; see docs/design if that changes.
CREATE OR REPLACE VIEW gold.spend_forecast_month AS
WITH daily AS (
    SELECT provider_name, charge_day, sum(cost) AS net_cost
    FROM silver.focus_normalized
    GROUP BY provider_name, charge_day
),
-- The last fully-delivered day per provider; everything downstream fits on days at or
-- before it.
bounds AS (
    SELECT provider_name, max(charge_day) - 1 AS last_complete_day
    FROM daily
    GROUP BY provider_name
),
complete AS (
    SELECT d.provider_name, d.charge_day, d.net_cost
    FROM daily d
    JOIN bounds b ON b.provider_name = d.provider_name
    WHERE d.charge_day <= b.last_complete_day
),
fit AS (
    SELECT
        provider_name,
        count(*)                                         AS history_days,
        max(date_trunc('month', charge_day))::DATE       AS last_month,
        regr_slope(net_cost, datediff('day', DATE '1970-01-01', charge_day))     AS slope,
        regr_intercept(net_cost, datediff('day', DATE '1970-01-01', charge_day)) AS intercept
    FROM complete
    GROUP BY provider_name
),
-- Actuals include the partial newest day (it's real money already billed); only the
-- per-day *rate* excludes it.
month_actual AS (
    SELECT provider_name, date_trunc('month', charge_day)::DATE AS charge_month,
           sum(net_cost) AS actual_to_date
    FROM daily
    GROUP BY 1, 2
),
month_complete AS (
    SELECT provider_name, date_trunc('month', charge_day)::DATE AS charge_month,
           sum(net_cost) AS complete_cost,
           count(*)      AS complete_days
    FROM complete
    GROUP BY 1, 2
),
run_rate AS (
    SELECT
        f.provider_name,
        f.last_month                                     AS charge_month,
        'run_rate'                                       AS forecast_kind,
        CASE WHEN mc.complete_days > 0
             THEN round(mc.complete_cost / mc.complete_days
                        * datediff('day', f.last_month, f.last_month + to_months(1)), 2)
             END                                         AS forecast_cost,
        ma.actual_to_date,
        coalesce(mc.complete_days, 0)                    AS history_days
    FROM fit f
    JOIN month_actual ma
      ON ma.provider_name = f.provider_name AND ma.charge_month = f.last_month
    LEFT JOIN month_complete mc
      ON mc.provider_name = f.provider_name AND mc.charge_month = f.last_month
),
-- Sum the fitted line across a future month analytically: for n days starting at epoch
-- index a, sum(intercept + slope*d) = n*intercept + slope*(n*a + n*(n-1)/2).
trend_shape AS (
    SELECT
        f.provider_name,
        f.history_days,
        f.slope,
        f.intercept,
        (f.last_month + to_months(g.n::INTEGER))::DATE   AS charge_month,
        datediff('day', (f.last_month + to_months(g.n::INTEGER))::DATE,
                 (f.last_month + to_months((g.n + 1)::INTEGER))::DATE) AS n_days,
        datediff('day', DATE '1970-01-01',
                 (f.last_month + to_months(g.n::INTEGER))::DATE)       AS first_index
    FROM fit f
    CROSS JOIN generate_series(1, 3) AS g(n)
),
trend AS (
    SELECT
        provider_name,
        charge_month,
        'trend'                                          AS forecast_kind,
        CASE WHEN history_days >= 60
             THEN greatest(0, round(n_days * intercept
                      + slope * (n_days * first_index + n_days * (n_days - 1) / 2.0), 2))
             END                                         AS forecast_cost,
        CAST(NULL AS DOUBLE)                             AS actual_to_date,
        history_days
    FROM trend_shape
)
SELECT * FROM run_rate
UNION ALL
SELECT * FROM trend;
