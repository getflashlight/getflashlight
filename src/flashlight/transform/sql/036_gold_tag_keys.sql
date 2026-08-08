-- GOLD: cost-allocation spend per *normalized* tag key. ONE consumer view.
--
-- Real tagging is inconsistent in a specific, boring way: the same dimension arrives under
-- several spellings that differ only by case or separator — `epic`/`Epic`, `app-long`/
-- `app_long`, `user`/`User`, `compute-type`/`compute_type`. Ranked side by side in
-- gold.spend_by_tag_month they read as distinct dimensions, so the same team's spend looks
-- half its real size and the ranking is wrong.
--
-- WHY THIS IS A NEW VIEW rather than a fold applied inside spend_by_tag_month:
--   1. Folding in place is a breaking change for agents — `list_dimension_values` on
--      tag_key currently returns both spellings, and that difference IS a finding ("your
--      tagging is inconsistent"). Collapsing it upstream makes that unanswerable.
--   2. spend_by_sku_tag_month explodes the same Tags JSON and is not folded, so a fold in
--      one and not the other would put two views that should reconcile out of sync.
-- So: raw keys stay exactly where they were, and this view adds the folded ranking plus
-- tag_key_variants/variant_count, which make the collision itself visible and countable.
--
-- CREDITS ARE EXCLUDED (`NOT is_credit`), deliberately differing from spend_by_tag_month,
-- which does not filter them. This view is meant to be read against
-- spend_tag_coverage_month.tagged_pct, and that view measures charges only (credits are
-- negative and typically untagged, which otherwise pushes a tagged share above 100%) — so
-- matching its denominator is what makes the two comparable.
--
-- DO NOT SUM net_cost ACROSS KEYS. A resource carrying two tags contributes its full cost
-- to both keys, so the column total exceeds the month's real tagged spend. That is inherent
-- to a per-key breakdown, not a bug, and it is why this view publishes no percentage: the
-- honest denominator is spend_tag_coverage_month.tagged_cost, which counts each resource
-- once.
CREATE OR REPLACE VIEW gold.spend_by_tag_key_month AS
WITH exploded AS (
    SELECT
        f.provider_name,
        f.charge_month,
        t.tag_key,
        -- Case and separator only. Two keys are "the same dimension" when they differ by
        -- nothing a human would consider meaningful.
        replace(lower(trim(t.tag_key)), '-', '_')            AS tag_key_normalized,
        json_extract_string(f.tags, '$."' || t.tag_key || '"') AS tag_value,
        f.cost
    FROM silver.focus_provider_bill f
    CROSS JOIN unnest(json_keys(f.tags)) AS t(tag_key)
    WHERE NOT f.is_credit
),
-- Collapsed to one row per raw spelling first, so variant_count counts SPELLINGS and not
-- the billing rows that happen to use each one.
variants AS (
    SELECT DISTINCT provider_name, charge_month, tag_key_normalized, tag_key
    FROM exploded
),
variant_agg AS (
    SELECT
        provider_name,
        charge_month,
        tag_key_normalized,
        string_agg(tag_key, ' · ' ORDER BY tag_key)          AS tag_key_variants,
        count(*)                                             AS variant_count
    FROM variants
    GROUP BY provider_name, charge_month, tag_key_normalized
),
cost_agg AS (
    SELECT
        provider_name,
        charge_month,
        tag_key_normalized,
        sum(cost)                                            AS net_cost,
        count(DISTINCT tag_value)                            AS tag_value_count
    FROM exploded
    GROUP BY provider_name, charge_month, tag_key_normalized
)
SELECT
    c.provider_name,
    c.charge_month,
    c.tag_key_normalized,
    v.tag_key_variants,
    v.variant_count,
    c.net_cost,
    c.tag_value_count
FROM cost_agg c
JOIN variant_agg v
  ON v.provider_name      = c.provider_name
 AND v.charge_month       = c.charge_month
 AND v.tag_key_normalized = c.tag_key_normalized;
