-- GOLD: AI/ML spend at the AI-product and endpoint grain. Provider-scoped.
--
-- SCOPE: every product the provider categorizes as AI, PLUS a Flashlight-owned list of AI
-- products the vendored Databricks query files under another category — AI/BI Genie most
-- notably, which bills as warehouse-shaped usage. See the WHERE clause below for why both
-- routes exist and why the extra enum spellings are safe to guess.
--
-- The dollars this view surfaces were already in BRONZE — the vendored Databricks FOCUS
-- query stamps `service_category = 'AI and Machine Learning'` on eight billing products
-- and routes each one's `usage_metadata.<x>_id` into `resource_id`
-- (databricks_focus_1_3.sql: MODEL_SERVING/AI_FUNCTIONS/VECTOR_SEARCH/AI_GATEWAY →
-- endpoint_id, FOUNDATION_MODEL_TRAINING → run_name, AI_RUNTIME → ai_runtime_workload_id,
-- AGENT_BRICKS → agent_bricks_id). They were reachable only by knowing which
-- `service_name` values happened to be AI, which is why this view exists: one place that
-- names the AI slice of the bill.
--
-- WHY A NEW MACRO rather than more WHENs on gold.compute_family (030_gold_metrics.sql):
-- compute_family maps MODEL_SERVING to 'endpoint' and is read by spend_by_sku_month and
-- resource_month. Folding VECTOR_SEARCH/AI_GATEWAY into that same 'endpoint' bucket would
-- silently change what every existing consumer of those two views means by 'endpoint',
-- and a family is about *how compute is bought*, not *what product it belongs to*. So AI
-- product identity gets its own macro and compute_family is left alone.
--
-- SCOPE: this view carries **cost only**. Token counts, model identity and the user who
-- issued a request are not in the FOCUS bill at all — they come from the `ai` GOLD group
-- (080_gold_ai_usage.sql) fed by the separate `system.serving` telemetry pull. A month with
-- rows here and none there means the bill was read and the token telemetry was not.
--
-- `service_subcategory` (the vendored query's 'AI Platforms' / 'Generative AI' split) is
-- deliberately NOT usable here — it is not persisted in lake/schema.py's BRONZE_SCHEMA, so
-- ai_product_family below is the only product-level grouping available downstream.

-- Databricks-only AI-product grouping, keyed on `service_name` — which for Databricks
-- carries `billing_origin_product` verbatim, a Databricks-maintained enum. Any other
-- provider's service_name (and any Databricks product that isn't AI) stays NULL rather
-- than being forced into a bucket — "not applicable", never miscategorized.
-- ponytail: add a WHEN here when Databricks ships a new AI billing product, or map another
-- provider's AI service_name values (e.g. AWS Bedrock) when a connector emits them.
--
-- The first eight are the products the vendored FOCUS query itself categorizes as 'AI and
-- Machine Learning'. The rest are AI products it categorizes ELSEWHERE (AI/BI Genie bills as
-- warehouse-shaped usage, so the vendored query files it under Databases/Analytics) — they
-- are AI spend by any reasonable reading, so gold.ai_spend_month unions them in explicitly.
-- See _AI_EXTRA_PRODUCTS below for why the enum spellings are safe to guess.
CREATE OR REPLACE MACRO gold.ai_product_family(service_name) AS
    CASE service_name
        WHEN 'MODEL_SERVING' THEN 'model_serving'
        WHEN 'VECTOR_SEARCH' THEN 'vector_search'
        WHEN 'AI_GATEWAY' THEN 'ai_gateway'
        WHEN 'AI_FUNCTIONS' THEN 'ai_functions'
        WHEN 'FOUNDATION_MODEL_TRAINING' THEN 'foundation_model_training'
        WHEN 'AGENT_BRICKS' THEN 'agent_bricks'
        WHEN 'AI_RUNTIME' THEN 'ai_runtime'
        WHEN 'AGENT_EVALUATION' THEN 'agent_evaluation'
        -- Not in the vendored query's AI category; see above.
        WHEN 'AI_BI_GENIE' THEN 'genie'
        WHEN 'GENIE' THEN 'genie'
        WHEN 'AI_BI_DASHBOARD' THEN 'ai_bi_dashboard'
        WHEN 'AI_BI_DASHBOARDS' THEN 'ai_bi_dashboard'
        WHEN 'LAKEHOUSE_MONITORING' THEN 'lakehouse_monitoring'
        WHEN 'DATA_QUALITY_MONITORING' THEN 'lakehouse_monitoring'
        WHEN 'MODEL_TRAINING' THEN 'foundation_model_training'
    END;


-- ── "What is AI costing me, and on which endpoint?" ──────────────────────────────
-- One row per (AI product, resource, SKU, month). `resource_id` is the serving/vector-
-- search endpoint id for the endpoint-shaped products, so this is the join key the `ai`
-- group's token views use to put a dollar figure next to a token count.
--
-- Carries BOTH net_cost and gross_cost, matching gold.spend_by_service_month: provider
-- pages answer "what did I owe?" with net, and the charges-only figure is what reconciles
-- against a tag-coverage denominator (credits are negative and typically untagged).
--
-- `project_tag` reads the same cost-allocation key this codebase already uses for project
-- attribution — the `'project'` key databricks_efficiency.sql:214 feeds into
-- EfficiencyRecord.owner_project — so the AI tab's project rollup and the efficiency
-- plane's agree instead of each picking their own convention. It matches on the *folded*
-- key (036_gold_tag_keys.sql's case/separator fold), so 'Project' and 'PROJECT' resolve
-- too. The fold is case/separator only, never a substring match: `cost-project` folds to
-- `cost_project`, a different dimension, and does NOT resolve — borrowing its value would
-- report a project the endpoint was never tagged with. NULL, never a placeholder string,
-- when the endpoint carries no such tag — an untagged endpoint is the finding (see the
-- endpoint_tagging policy rule), not a bucket named "untagged".
-- ponytail: make the key configurable if a second org spells it differently.
CREATE OR REPLACE VIEW gold.ai_spend_month AS
WITH ai_rows AS (
    SELECT
        provider_name,
        charge_month,
        service_name,
        resource_type,
        resource_id,
        resource_name,
        sku_id,
        cost,
        is_credit,
        consumed_quantity,
        consumed_unit,
        is_partial_period,
        -- Case- and separator-insensitive tag lookup, so 'Project'/'project'/'cost-project'
        -- style spellings resolve to the same dimension — the same fold
        -- 036_gold_tag_keys.sql applies to tag keys. A scalar subquery rather than a plain
        -- json_extract_string so the match is on the *folded* key, not the literal one.
        (
            SELECT json_extract_string(f.tags, '$."' || t.k || '"')
            FROM unnest(json_keys(f.tags)) AS t(k)
            WHERE replace(lower(trim(t.k)), '-', '_') = 'project'
            LIMIT 1
        )                                                AS project_tag,
        -- Raw tags JSON so the AI Costs tab can attribute by any cost-allocation key
        -- (team/env/project/…), not only the hardcoded project_tag above.
        f.tags                                           AS tags
    FROM silver.focus_provider_bill f
    -- TWO ways in, on purpose.
    --
    -- (1) service_category is the FOCUS-native, provider-authored fact, so any product a
    --     provider categorizes as AI is included the day it starts billing — including ones
    --     that don't exist yet, and including another provider's (AWS Bedrock stamps the same
    --     category). That is the durable half.
    --
    -- (2) But the vendored Databricks FOCUS query categorizes exactly EIGHT products as 'AI
    --     and Machine Learning' (databricks_focus_1_3.sql:405-415), and real AI products sit
    --     outside that list: AI/BI Genie bills as warehouse-shaped usage and is filed under
    --     Databases, and the monitoring products are ML-driven but filed elsewhere too.
    --     Anyone asking "what is AI costing me?" means Genie as well. Editing the vendored
    --     query to recategorize them is not an option (CLAUDE.md: don't hand-edit its logic —
    --     it's re-pulled upstream), so this view names them itself.
    --
    --     PREDICTIVE_OPTIMIZATION is deliberately NOT in this list — it's Databricks
    --     auto-tuning table layout/clustering, not an AI product a user runs; despite being
    --     ML-driven internally it isn't AI spend by any reasonable reading, so it stays out
    --     of this view (and out of gold.ai_product_family below).
    --
    -- WHY GUESSING THESE ENUM SPELLINGS IS SAFE, unlike guessing a column name: a wrong
    -- VALUE in an IN list simply matches nothing, while a wrong column name breaks the whole
    -- query. So the candidate spellings for a product whose exact enum value we haven't
    -- confirmed against a live account are all listed; at most one exists and the others are
    -- inert. Verify with:
    --     SELECT DISTINCT billing_origin_product FROM system.billing.usage ORDER BY 1;
    -- and add the real spelling here and to gold.ai_product_family above if it differs.
    WHERE service_category = 'AI and Machine Learning'
       OR service_name IN (
              'AI_BI_GENIE', 'GENIE',
              'AI_BI_DASHBOARD', 'AI_BI_DASHBOARDS',
              'LAKEHOUSE_MONITORING', 'DATA_QUALITY_MONITORING',
              'MODEL_TRAINING'
          )
)
SELECT
    provider_name,
    charge_month,
    gold.ai_product_family(service_name)                 AS ai_product_family,
    service_name,
    coalesce(resource_type, '(none)')                    AS resource_type,
    coalesce(resource_id, '(none)')                      AS resource_id,
    coalesce(resource_name, resource_id, '(unattributed)') AS resource_name,
    coalesce(sku_id, '(unknown)')                        AS sku_id,
    -- A real GROUP BY column, not max() — unlike resource_month, where the unit is an
    -- undeclared aggregate. Whether a SKU's quantity is denominated in DBUs or in tokens is
    -- the distinction this whole AI feature turns on, so it is a declared dimension a
    -- consumer can group by. In practice it is constant within a (product, resource, SKU)
    -- group, so grouping on it adds no rows.
    coalesce(consumed_unit, '(unknown)')                 AS consumed_unit,
    -- NULL, never a placeholder, when the endpoint carries no project tag — see header.
    max(project_tag)                                     AS project_tag,
    -- Tags are constant per (resource, SKU, month) in practice; max() is a stable pick
    -- when every input row carries the same JSON string.
    max(tags)                                            AS tags,
    sum(cost)                                            AS net_cost,
    sum(cost) FILTER (WHERE NOT is_credit)               AS gross_cost,
    sum(consumed_quantity)                               AS consumed_quantity,
    bool_or(is_partial_period)                           AS is_partial_period
FROM ai_rows
GROUP BY provider_name, charge_month, gold.ai_product_family(service_name), service_name,
         coalesce(resource_type, '(none)'), coalesce(resource_id, '(none)'),
         coalesce(resource_name, resource_id, '(unattributed)'),
         coalesce(sku_id, '(unknown)'), coalesce(consumed_unit, '(unknown)');
