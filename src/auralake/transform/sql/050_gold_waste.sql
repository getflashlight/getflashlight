-- GOLD: the waste contract. Classifies each standardized EfficiencyRecord into a
-- waste_category + recoverable_cost. ONE consumer view; the dashboard and MCP read it.
--
-- Inputs: metrics.efficiency_record (one row per entity x month, aggregated AT SOURCE).
-- An entity can emit MULTIPLE category rows (additive, like FOCUS line items) — a job
-- can be both underutilized AND have failed runs. charge_month arrives as the partition
-- string 'YYYY-MM'; normalize to a date here so it matches the rest of GOLD.
--
-- HONESTY (carried from the FOCUS plane):
--   * underutilized requires a non-NULL utilization_pct → NEVER emitted for shared
--     compute (interactive/sql_warehouse), where per-entity CPU% does not exist.
--   * placement and photon_no_gain are 'candidate' confidence (real confirmation needs
--     job-context / an A/B run); idle and failed are 'high'.
--   * we report failed-run and idle-window cost — never an auto-termination "saving".

-- ── Recoverable-fraction knobs (tune as rates move) ────────────────────────────────
-- ponytail: hard-coded heuristics; promote to a list_prices-derived join if the rate
-- delta needs to track contract pricing. placement ≈ all-purpose($0.55)→jobs($0.15);
-- photon ≈ 2.9x DBU multiplier → (1 − 1/2.9) premium share.

CREATE OR REPLACE VIEW gold.waste_record AS
WITH e AS (
    SELECT
        provider_name,
        strptime(charge_month, '%Y-%m')::date              AS charge_month,
        entity_type,
        entity_id,
        entity_name,
        owner_user,
        owner_project,
        billed_cost,
        utilization_pct,
        activity_count,
        TRY_CAST(json_extract_string(cause_detail, '$.failed_cost') AS DOUBLE)
                                                           AS failed_cost,
        TRY_CAST(json_extract_string(cause_detail, '$.pct_runs_underutilized') AS DOUBLE)
                                                           AS pct_runs_underutilized,
        coalesce(TRY_CAST(json_extract_string(cause_detail, '$.photon') AS BOOLEAN), false)
                                                           AS photon
    FROM metrics.efficiency_record
)
-- underutilized (measurable utilization only)
SELECT provider_name, charge_month, entity_type, entity_id, entity_name,
       owner_user, owner_project, billed_cost,
       'underutilized'                                     AS waste_category,
       'WASTE'                                             AS lens,
       round(billed_cost * (1 - utilization_pct / 100.0), 2) AS recoverable_cost,
       CASE WHEN coalesce(pct_runs_underutilized, 0) >= 0.8 THEN 'high'
            ELSE 'candidate' END                           AS confidence
FROM e
WHERE utilization_pct IS NOT NULL AND utilization_pct <= 20
UNION ALL
-- idle (billed but zero activity)
SELECT provider_name, charge_month, entity_type, entity_id, entity_name,
       owner_user, owner_project, billed_cost,
       'idle', 'WASTE', round(billed_cost, 2), 'high'
FROM e
WHERE coalesce(activity_count, 0) = 0 AND billed_cost > 0
UNION ALL
-- placement: real work on interactive/SQL that could run on jobs compute → OPPORTUNITY
SELECT provider_name, charge_month, entity_type, entity_id, entity_name,
       owner_user, owner_project, billed_cost,
       'placement', 'OPPORTUNITY', round(billed_cost * 0.70, 2), 'candidate'
FROM e
WHERE entity_type IN ('interactive', 'sql_warehouse') AND coalesce(activity_count, 0) > 0
UNION ALL
-- failed / retried run spend
SELECT provider_name, charge_month, entity_type, entity_id, entity_name,
       owner_user, owner_project, billed_cost,
       'failed', 'WASTE', round(failed_cost, 2), 'high'
FROM e
WHERE coalesce(failed_cost, 0) > 0
UNION ALL
-- photon on a no-gain (low-util) workload (candidate; real A/B needs with/without)
SELECT provider_name, charge_month, entity_type, entity_id, entity_name,
       owner_user, owner_project, billed_cost,
       'photon_no_gain', 'WASTE', round(billed_cost * (1 - 1 / 2.9), 2), 'candidate'
FROM e
WHERE photon AND utilization_pct IS NOT NULL AND utilization_pct <= 20;


-- ── KPI rollup: recoverable $ per month × category × lens × confidence ──────────────
CREATE OR REPLACE VIEW gold.waste_summary_month AS
SELECT
    charge_month,
    waste_category,
    lens,
    confidence,
    sum(recoverable_cost)                                  AS recoverable_cost,
    sum(billed_cost)                                       AS billed_cost,
    count(*)                                               AS entity_count
FROM gold.waste_record
GROUP BY charge_month, waste_category, lens, confidence;
