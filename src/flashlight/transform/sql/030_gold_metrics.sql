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
FROM silver.focus_provider_bill
GROUP BY provider_name, charge_month;


-- ── "Where is the money going?" — by service / product ──────────────────────────
-- Carries the same list_cost/savings pair as `monthly_bill` above, deliberately: this is
-- `monthly_bill` at one finer grain, over the same silver.focus_provider_bill with the same
-- sum(cost)/sum(list_cost), so the two reconcile by construction. That's what lets a
-- consumer scoped to a subset of services (the /aws page is scoped to Redshift's own
-- service names) build a list/savings/realized-discount headline that agrees with the
-- provider-wide one, instead of bolting a service dimension onto `monthly_bill` — which
-- would make the headline view carry a grain nothing else needs.
CREATE OR REPLACE VIEW gold.spend_by_service_month AS
SELECT
    provider_name,
    service_category,
    service_name,
    charge_month,
    sum(cost)                                            AS net_cost,
    sum(cost) FILTER (WHERE NOT is_credit)               AS gross_cost,
    sum(cost) FILTER (WHERE is_credit)                   AS credit_cost,
    sum(list_cost)                                       AS list_cost,
    sum(list_cost) - sum(cost)                           AS savings,
    bool_or(is_partial_period)                           AS is_partial_period,
    bool_or(x_effective_is_list)                         AS effective_is_list,
    max(billing_currency)                                AS currency
FROM silver.focus_provider_bill
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
FROM silver.focus_provider_bill
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
FROM silver.focus_provider_bill
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
    -- Keep the charge-side amount beside net_cost so a resource rollup can
    -- reconcile exactly to spend_by_service_month.gross_cost.  In particular,
    -- a person/warehouse allocation should not appear to lose a credit merely
    -- because the service-level total is presented as charges.
    sum(cost) FILTER (WHERE NOT is_credit)               AS gross_cost,
    sum(consumed_quantity)                               AS consumed_quantity,
    max(consumed_unit)                                   AS consumed_unit,
    bool_or(is_partial_period)                           AS is_partial_period
FROM silver.focus_provider_bill
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
FROM silver.focus_provider_bill f
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
FROM silver.focus_provider_bill
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
    sum(f.cost) FILTER (WHERE NOT f.is_credit)           AS gross_cost,
    bool_or(f.is_partial_period)                         AS is_partial_period
FROM silver.focus_provider_bill f
CROSS JOIN unnest(json_keys(f.tags)) AS t(tag_key)
GROUP BY tag_key, tag_value, f.provider_name, f.charge_month;


-- ── "How much of my spend can I actually attribute?" — tag coverage ──────────────
-- The honest denominator for the two tag views above. Both explode the Tags JSON with
-- a CROSS JOIN, so untagged rows vanish from them entirely — a tag breakdown alone can
-- look like full coverage when most of the bill carries no tags at all. This view keeps
-- the untagged remainder visible (attribution honesty: never silently dropped), and is
-- what a "tag coverage" KPI reconciles against.
--
-- Tagged = the Tags JSON has at least one key. `tags` is '{}' both when the source
-- reported no tags and when it reported something unparseable (see lake/schema.py and
-- focus/sql_mapping.py) — either way this is spend we cannot attribute, which is the
-- question being asked.
--
-- Coverage is measured over CHARGES ONLY (`NOT is_credit`), never net. Credits are
-- negative rows and are typically untagged, so a net-based split reports a negative
-- untagged_cost and a tagged share above 100% — observed on the FOCUS sample, which
-- has untagged credits. "How much of what I was charged can I attribute?" is the
-- question, and a credit isn't something you attribute to a team. net_cost is kept
-- alongside for reconciliation against monthly_bill.
CREATE OR REPLACE VIEW gold.spend_tag_coverage_month AS
SELECT
    provider_name,
    charge_month,
    sum(cost)                                            AS net_cost,
    sum(cost) FILTER (WHERE NOT is_credit)               AS gross_cost,
    sum(cost) FILTER (WHERE NOT is_credit AND json_array_length(json_keys(tags)) > 0)
                                                         AS tagged_cost,
    sum(cost) FILTER (WHERE NOT is_credit AND json_array_length(json_keys(tags)) = 0)
                                                         AS untagged_cost,
    CASE WHEN sum(cost) FILTER (WHERE NOT is_credit) > 0
         THEN round(100 * sum(cost) FILTER (
                  WHERE NOT is_credit AND json_array_length(json_keys(tags)) > 0
              ) / sum(cost) FILTER (WHERE NOT is_credit), 1)
         ELSE NULL END                                   AS tagged_pct,
    bool_or(is_partial_period)                           AS is_partial_period
FROM silver.focus_provider_bill
GROUP BY provider_name, charge_month;


-- ── "Which services carry untagged spend?" — tag coverage at service grain ──────
-- spend_tag_coverage_month is provider × month; this is the same charge-only tagged/
-- untagged split one grain finer, so Attribution can rank *where* tagging is missing
-- (Jobs Compute vs SQL vs S3) rather than only how much. Fully-tagged services stay
-- in the view (tagged_pct = 100) so a consumer can see clean coverage; the dashboard
-- filters to untagged_cost > 0. Same Tags/'{}' honesty as the provider-level view.
CREATE OR REPLACE VIEW gold.spend_untagged_by_service_month AS
SELECT
    provider_name,
    coalesce(service_name, '(no service)')               AS service_name,
    charge_month,
    sum(cost) FILTER (WHERE NOT is_credit)               AS gross_cost,
    -- coalesce: DuckDB SUM over an empty FILTER is NULL (fully tagged → untagged NULL,
    -- fully untagged → tagged NULL), which would break "untagged_cost > 0" filters and
    -- tagged_pct arithmetic.
    coalesce(sum(cost) FILTER (
        WHERE NOT is_credit AND json_array_length(json_keys(tags)) > 0
    ), 0)                                                AS tagged_cost,
    coalesce(sum(cost) FILTER (
        WHERE NOT is_credit AND json_array_length(json_keys(tags)) = 0
    ), 0)                                                AS untagged_cost,
    CASE WHEN sum(cost) FILTER (WHERE NOT is_credit) > 0
         THEN round(100 * coalesce(sum(cost) FILTER (
                  WHERE NOT is_credit AND json_array_length(json_keys(tags)) > 0
              ), 0) / sum(cost) FILTER (WHERE NOT is_credit), 1)
         ELSE NULL END                                   AS tagged_pct,
    bool_or(is_partial_period)                           AS is_partial_period
FROM silver.focus_provider_bill
GROUP BY provider_name, coalesce(service_name, '(no service)'), charge_month;


-- ── "Which resources are untagged?" — the work queue under a service gap ────────
-- Attribution's next step after spend_untagged_by_service_month: ranked untagged
-- *resources* that reconcile to a service's untagged_cost. Only untagged charge rows
-- (empty Tags, NOT is_credit) — fully-tagged resources are absent by construction.
-- Identity coalesced like resource_month so account/shared lines without a resource_id
-- stay visible as '(none)' / '(unattributed)' rather than vanishing.
CREATE OR REPLACE VIEW gold.spend_untagged_by_resource_month AS
SELECT
    provider_name,
    coalesce(service_name, '(no service)')               AS service_name,
    coalesce(sku_id, '(unknown)')                        AS sku_id,
    coalesce(resource_type, '(none)')                    AS resource_type,
    coalesce(resource_id, '(none)')                      AS resource_id,
    coalesce(resource_name, resource_id, '(unattributed)') AS resource_name,
    coalesce(sub_account_id, '(none)')                   AS sub_account_id,
    coalesce(region_id, '(none)')                        AS region_id,
    charge_month,
    sum(cost)                                            AS untagged_cost,
    bool_or(is_partial_period)                           AS is_partial_period
FROM silver.focus_provider_bill
WHERE NOT is_credit
  AND json_array_length(json_keys(tags)) = 0
GROUP BY provider_name,
         coalesce(service_name, '(no service)'),
         coalesce(sku_id, '(unknown)'),
         coalesce(resource_type, '(none)'),
         coalesce(resource_id, '(none)'),
         coalesce(resource_name, resource_id, '(unattributed)'),
         coalesce(sub_account_id, '(none)'),
         coalesce(region_id, '(none)'),
         charge_month;


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
FROM silver.focus_provider_bill
GROUP BY provider_name, charge_month;


-- ── "Which credits/discounts hit this bill, and when?" — credit detail ───────────
-- The one view where credits are the subject rather than a `sum(cost)` term. Every
-- other view nets them into `cost` (FOCUS-correct: EffectiveCost is post-credit), which
-- is right for a bill but hides a one-off: a single goodwill credit can swing a
-- provider's month by more than its usage moved, and at month grain that reads as "spend
-- collapsed". Kept at charge-description grain because that's the credit's identity (AWS
-- puts the credit name + credit id there), so a headline that excludes credits (the home
-- page trends charges only) can point at exactly what it excluded.
--
-- Credit AND Adjustment: both are non-usage lines that move the invoice (FOCUS
-- ChargeCategory), and a user asking "what discount did I get?" means either. Tax and
-- Purchase stay out — they're charges, not reductions.
CREATE OR REPLACE VIEW gold.credits_month AS
SELECT
    provider_name,
    charge_month,
    charge_category,
    coalesce(service_name, '(account-level)')            AS service_name,
    coalesce(charge_description, '(no description)')     AS charge_description,
    sum(cost)                                            AS net_cost,
    count(*)                                             AS line_count,
    bool_or(is_partial_period)                           AS is_partial_period
FROM silver.focus_provider_bill
WHERE charge_category IN ('Credit', 'Adjustment')
GROUP BY provider_name, charge_month, charge_category,
         coalesce(service_name, '(account-level)'),
         coalesce(charge_description, '(no description)');


-- ── Daily spend trend per provider — drives the time-series panels ───────────────
-- Carries `service_name` so a consumer scoped to a subset of services can still get a
-- DAILY series. Without it, the only service-dimensioned view is monthly, and a
-- service-scoped page has no daily trend at all (that was the /aws page's gap). This is
-- the one view here where the extra dimension genuinely multiplies rows — days ×
-- providers × services — so every consumer wanting a provider-wide daily series must
-- now aggregate: `sum(net_cost) ... GROUP BY charge_day`, never one row per day.
CREATE OR REPLACE VIEW gold.spend_trend_daily AS
SELECT
    charge_day,
    provider_name,
    service_name,
    sum(cost)                                            AS net_cost,
    sum(cost) FILTER (WHERE NOT is_credit)               AS gross_cost,
    bool_or(is_partial_period)                           AS is_partial_period
FROM silver.focus_provider_bill
GROUP BY charge_day, provider_name, service_name;


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
    FROM silver.focus_provider_bill
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
