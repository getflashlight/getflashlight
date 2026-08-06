-- GOLD: the utilization *visibility* contract. ONE consumer view; the dashboard and MCP read it.
--
-- Why this exists separately from gold.waste_record: waste_record only ever contains rows
-- that MATCHED a rule, so it can answer "what is wasteful?" but not "how well is my infra
-- used?". An entity measured at 45% utilization is invisible there — no rule fires — and so
-- is an entity whose telemetry never arrived. This view carries every measured entity-month
-- and, crucially, says which of the three states each row is in.
--
-- Grain: one row per (entity, month), the same grain as metrics.efficiency_record. It is
-- deliberately NOT fanned out one-row-per-signal: that would repeat billed_cost across
-- rows and make it non-additive, the double-count class this codebase guards hardest.
--
-- Honesty rules encoded here (mirroring waste_rules.py's):
--   * NULL telemetry never becomes a verdict. There is no `coalesce(..., false)` on any
--     cause_detail extraction below — a missing key stays NULL and reads as `unmeasured`.
--   * `measurement_status` separates "we cannot obtain this" from "we did not get it".
--     Shared compute (SQL warehouses), per-user shares, query shapes, tables and serving
--     endpoints have no per-entity utilization *in principle* (see efficiency/model.py's
--     EntityType docs), so
--     they are `not_applicable`. Only job/interactive/notebook can be `unmeasured`, which
--     makes that value a real signal: the pull ran and did not deliver CPU telemetry.
--   * `is_saturated_reading` exists because ~60% of real readings are exactly 100.0 —
--     a node_timeline ceiling artifact. Reporting those as "perfectly right-sized" would be
--     a lie, so the ceiling is named as a property OF THE READING, not a health verdict.
--   * `is_flagged_underutilized` is semi-joined from the baked rule pool, never recomputed
--     from a threshold literal. Static .sql files get no .format() (see transform/runner.py),
--     so a literal here would silently diverge from policies.yml the moment a user tunes it.
--     This also makes the genuinely new fact expressible: measured, low, and NO rule fired.
--
-- Comparability: primary_signal_value is only comparable within one (primary_signal_name,
-- primary_signal_unit) pair, and cost_per_native_unit only within one native_unit (the real
-- lake mixes DBU, MB and bytes). No unit conversion happens here on purpose — silent unit
-- math inside a view is unauditable. Consumers must group before they compare.
CREATE OR REPLACE VIEW gold.utilization_entity_month AS
WITH e AS (
    SELECT
        provider_name,
        strptime(charge_month, '%Y-%m')::date               AS charge_month,
        x_source_connector,
        entity_type,
        entity_id,
        entity_name,
        owner_user,
        owner_project,
        billed_cost,
        utilization_pct,
        activity_count,
        native_quantity,
        -- Case/whitespace only. 'DBU' and 'dbu' are one unit; MB and bytes are not.
        lower(trim(native_unit))                            AS native_unit,
        -- Per-entity-type signals. See WasteRule's docstring in efficiency/waste_rules.py
        -- for the authoritative inventory of which entity_type populates which key.
        TRY_CAST(json_extract_string(cause_detail, '$.max_cpu_pct') AS DOUBLE)
                                                            AS max_cpu_pct,
        TRY_CAST(json_extract_string(cause_detail, '$.max_mem_pct') AS DOUBLE)
                                                            AS max_mem_pct,
        TRY_CAST(json_extract_string(cause_detail, '$.cache_hit_pct') AS DOUBLE)
                                                            AS cache_hit_pct,
        TRY_CAST(json_extract_string(cause_detail, '$.duration_share_pct') AS DOUBLE)
                                                            AS duration_share_pct,
        TRY_CAST(json_extract_string(cause_detail, '$.pct_runs_spilling') AS DOUBLE)
                                                            AS pct_runs_spilling,
        TRY_CAST(json_extract_string(cause_detail, '$.stats_off_pct') AS DOUBLE)
                                                            AS stats_off_pct,
        TRY_CAST(json_extract_string(cause_detail, '$.unsorted_pct') AS DOUBLE)
                                                            AS unsorted_pct,
        TRY_CAST(json_extract_string(cause_detail, '$.days_since_last_access') AS DOUBLE)
                                                            AS days_since_last_access,
        TRY_CAST(json_extract_string(cause_detail, '$.avg_run_seconds') AS DOUBLE)
                                                            AS avg_run_seconds,
        TRY_CAST(json_extract_string(cause_detail, '$.query_count') AS DOUBLE)
                                                            AS query_count
    FROM metrics.efficiency_record
),
-- Pre-deduplicated on purpose: several rules can emit the same category for one entity-month,
-- and combining DISTINCT with ORDER BY inside string_agg is a DuckDB syntax hazard not worth
-- risking in a file whose failure takes down the whole transform.
cats AS (
    SELECT DISTINCT provider_name, charge_month, entity_type, entity_id, waste_category
    FROM gold.waste_record
),
flagged AS (
    SELECT
        provider_name,
        charge_month,
        entity_type,
        entity_id,
        string_agg(waste_category, ' · ' ORDER BY waste_category)  AS waste_categories,
        bool_or(waste_category = 'underutilized')                  AS is_flagged_underutilized
    FROM cats
    GROUP BY provider_name, charge_month, entity_type, entity_id
),
-- The primary signal's NAME is resolved once, here, so value/unit/direction below can all be
-- derived from it and cannot drift out of sync with each other.
picked AS (
    SELECT
        e.*,
        CASE
            WHEN e.entity_type IN ('job', 'interactive') THEN
                CASE WHEN e.max_cpu_pct IS NOT NULL THEN 'max_cpu_pct'
                     WHEN e.max_mem_pct IS NOT NULL THEN 'max_mem_pct' END
            WHEN e.entity_type = 'sql_warehouse' THEN
                CASE WHEN e.cache_hit_pct IS NOT NULL THEN 'cache_hit_pct' END
            WHEN e.entity_type = 'sql_warehouse_user' THEN
                CASE WHEN e.duration_share_pct IS NOT NULL THEN 'duration_share_pct' END
            WHEN e.entity_type = 'query_pattern' THEN
                CASE WHEN e.pct_runs_spilling IS NOT NULL THEN 'pct_runs_spilling' END
            WHEN e.entity_type = 'table' THEN
                CASE WHEN e.days_since_last_access IS NOT NULL THEN 'days_since_last_access'
                     WHEN e.stats_off_pct IS NOT NULL THEN 'stats_off_pct' END
        END                                                        AS primary_signal_name
    FROM e
)
SELECT
    p.provider_name,
    p.charge_month,
    p.x_source_connector,
    p.entity_type,
    p.entity_id,
    p.entity_name,
    p.owner_user,
    p.owner_project,
    p.billed_cost,
    p.utilization_pct,
    p.activity_count,
    p.native_quantity,
    p.native_unit,
    -- > 0, not <> 0: a corrective negative quantity would otherwise emit a negative rate.
    CASE WHEN p.native_quantity > 0
         THEN round(p.billed_cost / p.native_quantity, 6) END      AS cost_per_native_unit,
    -- 'endpoint' is not_applicable, not unmeasured: a serving endpoint has no CPU% at all
    -- (its waste is idle provisioned capacity, not underutilization — see EntityType.ENDPOINT).
    -- Leaving it in the ELSE would report every endpoint as "could be measured but no
    -- telemetry arrived", inflating the coverage caption's unmeasured count with rows that
    -- can never carry a reading.
    CASE WHEN p.utilization_pct IS NOT NULL THEN 'measured'
         WHEN p.entity_type IN ('sql_warehouse', 'sql_warehouse_user', 'query_pattern',
                                'table', 'endpoint')
              THEN 'not_applicable'
         ELSE 'unmeasured' END                                     AS measurement_status,
    -- Reifies the distinction the idle rule turns on: zero activity is waste, NULL is silence.
    CASE WHEN p.activity_count IS NOT NULL THEN 'measured'
         ELSE 'unmeasured' END                                     AS activity_status,
    CASE WHEN p.utilization_pct IS NULL THEN NULL
         ELSE p.utilization_pct >= 99.5 END                        AS is_saturated_reading,
    p.primary_signal_name,
    -- Simple CASE with no ELSE: a NULL primary_signal_name matches no branch and falls
    -- through to NULL, which is what an entity with no available signal should report.
    CASE p.primary_signal_name
        WHEN 'max_cpu_pct'            THEN p.max_cpu_pct
        WHEN 'max_mem_pct'            THEN p.max_mem_pct
        WHEN 'cache_hit_pct'          THEN p.cache_hit_pct
        WHEN 'duration_share_pct'     THEN p.duration_share_pct
        WHEN 'pct_runs_spilling'      THEN p.pct_runs_spilling
        WHEN 'stats_off_pct'          THEN p.stats_off_pct
        WHEN 'days_since_last_access' THEN p.days_since_last_access
    END                                                            AS primary_signal_value,
    CASE p.primary_signal_name
        WHEN 'days_since_last_access' THEN 'days'
        WHEN 'max_cpu_pct'            THEN 'pct'
        WHEN 'max_mem_pct'            THEN 'pct'
        WHEN 'cache_hit_pct'          THEN 'pct'
        WHEN 'duration_share_pct'     THEN 'pct'
        WHEN 'pct_runs_spilling'      THEN 'pct'
        WHEN 'stats_off_pct'          THEN 'pct'
    END                                                            AS primary_signal_unit,
    -- duration_share_pct is 'neutral': it is an attribution share (this user ran 40% of the
    -- warehouse's query time), not a health reading. High is not better or worse, just bigger.
    CASE p.primary_signal_name
        WHEN 'max_cpu_pct'            THEN 'higher_is_better'
        WHEN 'max_mem_pct'            THEN 'higher_is_better'
        WHEN 'cache_hit_pct'          THEN 'higher_is_better'
        WHEN 'duration_share_pct'     THEN 'neutral'
        WHEN 'pct_runs_spilling'      THEN 'lower_is_better'
        WHEN 'stats_off_pct'          THEN 'lower_is_better'
        WHEN 'days_since_last_access' THEN 'lower_is_better'
    END                                                            AS primary_signal_direction,
    -- Display-only, in neither dimensions nor measures (the `detail` precedent on
    -- waste_record). concat_ws skips NULLs, which is exactly the sparse behavior wanted, and
    -- IS DISTINCT FROM keeps the primary out without going NULL when there is no primary.
    nullif(concat_ws(' · ',
        CASE WHEN p.max_cpu_pct IS NOT NULL AND p.primary_signal_name IS DISTINCT FROM 'max_cpu_pct'
             THEN 'cpu ' || round(p.max_cpu_pct, 1)::VARCHAR || '%' END,
        CASE WHEN p.max_mem_pct IS NOT NULL AND p.primary_signal_name IS DISTINCT FROM 'max_mem_pct'
             THEN 'mem ' || round(p.max_mem_pct, 1)::VARCHAR || '%' END,
        CASE WHEN p.cache_hit_pct IS NOT NULL AND p.primary_signal_name IS DISTINCT FROM 'cache_hit_pct'
             THEN 'cache hit ' || round(p.cache_hit_pct, 1)::VARCHAR || '%' END,
        CASE WHEN p.duration_share_pct IS NOT NULL AND p.primary_signal_name IS DISTINCT FROM 'duration_share_pct'
             THEN 'duration share ' || round(p.duration_share_pct, 1)::VARCHAR || '%' END,
        CASE WHEN p.pct_runs_spilling IS NOT NULL AND p.primary_signal_name IS DISTINCT FROM 'pct_runs_spilling'
             THEN 'spilling ' || round(p.pct_runs_spilling, 1)::VARCHAR || '%' END,
        CASE WHEN p.stats_off_pct IS NOT NULL AND p.primary_signal_name IS DISTINCT FROM 'stats_off_pct'
             THEN 'stats off ' || round(p.stats_off_pct, 1)::VARCHAR || '%' END,
        CASE WHEN p.unsorted_pct IS NOT NULL
             THEN 'unsorted ' || round(p.unsorted_pct, 1)::VARCHAR || '%' END,
        CASE WHEN p.days_since_last_access IS NOT NULL AND p.primary_signal_name IS DISTINCT FROM 'days_since_last_access'
             THEN 'idle ' || round(p.days_since_last_access, 0)::VARCHAR || 'd' END,
        CASE WHEN p.avg_run_seconds IS NOT NULL
             THEN 'avg run ' || round(p.avg_run_seconds, 0)::VARCHAR || 's' END,
        CASE WHEN p.query_count IS NOT NULL
             THEN round(p.query_count, 0)::VARCHAR || ' queries' END
    ), '')                                                         AS secondary_signals,
    -- The one honest NULL-to-false coalesce in this file: waste_record contains EVERY rule
    -- match, so absence from it is positive evidence that no rule fired — unlike a missing
    -- telemetry key, which is evidence of nothing.
    coalesce(f.is_flagged_underutilized, false)                    AS is_flagged_underutilized,
    f.waste_categories
FROM picked p
LEFT JOIN flagged f
       ON f.provider_name = p.provider_name
      AND f.charge_month  = p.charge_month
      AND f.entity_type   = p.entity_type
      AND f.entity_id     = p.entity_id;
