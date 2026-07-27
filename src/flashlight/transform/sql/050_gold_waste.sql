-- GOLD: the waste contract. ONE consumer view; the dashboard and MCP read it.
--
-- gold.waste_record itself is generated at transform time, not defined here — see
-- flashlight.efficiency.waste_rules.build_waste_record_sql(). Classification is a
-- deterministic, config-driven pool of WasteRule entries (plain DuckDB SQL per rule,
-- no LLM/skill judgment) so a new rule is picked up on the next `flashlight transform`
-- with no SQL edit, and the dashboard/MCP always see the identical result. See that
-- module for the waste-honesty invariants (NULL never means "flag it", etc.) each
-- rule must preserve.

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


-- ── Entity coverage: which entities were actually measured this month? ──────────────
-- gold.waste_record only ever contains rows that MATCHED a rule's where_sql, so it
-- can't tell "rule evaluated, found nothing" apart from "this entity_type's telemetry
-- never arrived this window" (e.g. a connector's sub-pull came back empty, or hasn't
-- run at all). This is the one raw metrics.efficiency_record identity surface exposed
-- as GOLD so a consumer can make that distinction without reaching past GOLD into
-- metrics/silver directly (see the Redshift rule-coverage table, the first consumer).
CREATE OR REPLACE VIEW gold.efficiency_entity_month AS
SELECT DISTINCT
    provider_name,
    x_source_connector,
    entity_type,
    entity_id,
    -- Cast to DATE, same as gold.waste_record's own charge_month — metrics.efficiency_
    -- record stores it as a '%Y-%m' string, and the two views need to compare cleanly.
    strptime(charge_month, '%Y-%m')::date AS charge_month
FROM metrics.efficiency_record;


-- ── Resolution tracking: did a flagged (entity, category) go away, and did cost drop? ──
-- Pure re-detection — no new state, no user input. A (entity_id, waste_category) span is
-- collapsed to first/last seen across ALL history in gold.waste_record; is_resolved means
-- it did not appear in the most recent month we have data for. realized_savings compares
-- billed_cost the month it was last flagged vs. the month right after (0 if the entity's
-- data disappears entirely, e.g. terminated — a full recovery, not a data gap, since we
-- know a later month of data exists for that provider).
-- ponytail: min/max-over-history collapses a fix-then-relapse into one continuous span
-- (a relapsed entity just reads as "still flagged", which is correct for is_resolved, but
-- hides the temporary improvement in between) — upgrade to gap-and-island contiguous-run
-- detection if relapse patterns need to be visible, not just current status.
CREATE OR REPLACE VIEW gold.waste_resolution_month AS
WITH latest AS (
    SELECT provider_name, max(strptime(charge_month, '%Y-%m')::date) AS latest_month
    FROM metrics.efficiency_record
    GROUP BY provider_name
),
spans AS (
    SELECT
        provider_name, entity_type, entity_id,
        max_by(entity_name, charge_month)      AS entity_name,
        max_by(owner_user, charge_month)       AS owner_user,
        max_by(owner_project, charge_month)    AS owner_project,
        waste_category, lens,
        min(charge_month)                      AS first_seen_month,
        max(charge_month)                      AS last_seen_month,
        max_by(recoverable_cost, charge_month) AS recoverable_cost_at_last_seen,
        max_by(billed_cost, charge_month)      AS billed_cost_at_last_seen
    FROM gold.waste_record
    GROUP BY provider_name, entity_type, entity_id, waste_category, lens
),
next_month_cost AS (
    SELECT
        provider_name, entity_type, CAST(entity_id AS STRING) AS entity_id,
        strptime(charge_month, '%Y-%m')::date AS charge_month,
        sum(billed_cost) AS billed_cost
    FROM metrics.efficiency_record
    GROUP BY provider_name, entity_type, entity_id, charge_month
)
SELECT
    s.provider_name, s.entity_type, s.entity_id, s.entity_name,
    s.owner_user, s.owner_project, s.waste_category, s.lens,
    s.first_seen_month, s.last_seen_month,
    (s.last_seen_month < l.latest_month)                        AS is_resolved,
    CASE WHEN s.last_seen_month < l.latest_month
         THEN CAST(s.last_seen_month + INTERVAL 1 MONTH AS DATE) END AS resolved_month,
    s.recoverable_cost_at_last_seen,
    s.billed_cost_at_last_seen,
    nm.billed_cost                                              AS billed_cost_after,
    CASE WHEN s.last_seen_month < l.latest_month
         THEN round(s.billed_cost_at_last_seen - coalesce(nm.billed_cost, 0), 2)
    END                                                          AS realized_savings
FROM spans s
JOIN latest l ON l.provider_name = s.provider_name
LEFT JOIN next_month_cost nm
    ON nm.provider_name = s.provider_name AND nm.entity_type = s.entity_type
   AND nm.entity_id = s.entity_id
   AND nm.charge_month = CAST(s.last_seen_month + INTERVAL 1 MONTH AS DATE);
