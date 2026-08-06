-- GOLD: forward-looking spend. Everything else in this layer reports what already
-- happened; this projects what the current month lands at, and roughly where the next
-- few months go.
--
-- Two deliberately simple methods, both plain SQL — no statistical dependency, nothing
-- an operator can't re-derive by hand:
--
--   run_rate  the current month's completed-day average, extended to the whole month.
--             This is the number a FinOps practitioner actually acts on mid-month.
--   trend     a flat hold of the trailing 3 *complete* months' mean net, projected for
--             each of the next 3 months. Deliberately not a daily OLS line: extrapolating
--             a daily slope invents collapses (or spikes) that neither recent bills nor
--             the mid-month run-rate support.
--
-- Honesty rules, same discipline as the waste plane — a forecast that quietly invents
-- confidence is worse than no forecast:
--
--   * The newest day in the lake is dropped from every calculation. Billing exports
--     land 24-48h late, so that day is nearly always partially delivered; counting it
--     as a complete day drags every projection down.
--   * The month of `last_complete_day` never enters the trend fit — it is still
--     accruing (or, on the last day of a month, reserved as the run_rate anchor). Only
--     months strictly before it count as complete.
--   * `trend` is NULL until there are 3 complete months. On a short ingest lookback
--     that is the usual case, so the view reports no number rather than a bad one.
--     `history_days` is the day count inside the trailing-3 window (0 when empty);
--     backfilling (`flashlight ingest --start`, or FLASHLIGHT_INGEST_LOOKBACK_DAYS)
--     fixes it.
--   * Negative projections are clamped to 0.
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
anchor AS (
    SELECT
        provider_name,
        last_complete_day,
        date_trunc('month', last_complete_day)::DATE AS last_month
    FROM bounds
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
-- Months strictly before the anchor month — never the still-accruing one.
full_months AS (
    SELECT mc.provider_name, mc.charge_month, mc.complete_cost, mc.complete_days
    FROM month_complete mc
    JOIN anchor a ON a.provider_name = mc.provider_name
    WHERE mc.charge_month < a.last_month
),
ranked_months AS (
    SELECT
        provider_name,
        charge_month,
        complete_cost,
        complete_days,
        row_number() OVER (
            PARTITION BY provider_name ORDER BY charge_month DESC
        ) AS rn
    FROM full_months
),
trailing_3 AS (
    SELECT
        provider_name,
        count(*)            AS n_months,
        avg(complete_cost)  AS mean_cost,
        sum(complete_days)  AS history_days
    FROM ranked_months
    WHERE rn <= 3
    GROUP BY provider_name
),
run_rate AS (
    SELECT
        a.provider_name,
        a.last_month                                     AS charge_month,
        'run_rate'                                       AS forecast_kind,
        CASE WHEN mc.complete_days > 0
             THEN round(mc.complete_cost / mc.complete_days
                        * datediff('day', a.last_month, a.last_month + to_months(1)), 2)
             END                                         AS forecast_cost,
        ma.actual_to_date,
        coalesce(mc.complete_days, 0)                    AS history_days
    FROM anchor a
    JOIN month_actual ma
      ON ma.provider_name = a.provider_name AND ma.charge_month = a.last_month
    LEFT JOIN month_complete mc
      ON mc.provider_name = a.provider_name AND mc.charge_month = a.last_month
),
trend AS (
    SELECT
        a.provider_name,
        (a.last_month + to_months(g.n::INTEGER))::DATE   AS charge_month,
        'trend'                                          AS forecast_kind,
        CASE WHEN coalesce(t.n_months, 0) >= 3
             THEN greatest(0, round(t.mean_cost, 2))
             END                                         AS forecast_cost,
        CAST(NULL AS DOUBLE)                             AS actual_to_date,
        coalesce(t.history_days, 0)                      AS history_days
    FROM anchor a
    CROSS JOIN generate_series(1, 3) AS g(n)
    LEFT JOIN trailing_3 t ON t.provider_name = a.provider_name
)
SELECT * FROM run_rate
UNION ALL
SELECT * FROM trend;
