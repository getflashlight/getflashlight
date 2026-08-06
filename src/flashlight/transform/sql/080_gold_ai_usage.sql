-- GOLD: the AI unit-economics contract. Four consumer views; the AI Costs tab + MCP read
-- these and nothing else.
--
-- ── WHERE THE COST↔TOKEN JOIN LIVES ──────────────────────────────────────────────
-- This is a Databricks-internal join, NOT the cross-provider cost join CLAUDE.md forbids.
-- That one summed Databricks DBU spend with the AWS infrastructure underneath it into a
-- single "total"; this joins ONE bill's own endpoint spend to that SAME bill's own request
-- telemetry, at the grain the bill already publishes (resource_id IS the endpoint_id — see
-- databricks_focus_1_3.sql's ResourceId mapping). `provider_name` is part of the join key on
-- both sides, not a WHERE clause, so it is structurally impossible for one provider's token
-- count to pick up another provider's dollars — and a future Bedrock/Azure-OpenAI connector
-- lands rows here with no edit to this file.
--
-- FOCUS stays canonical for dollars: metrics.ai_usage carries no cost column at all, and
-- net_cost below is read from silver.focus_normalized, never recomputed from list_prices.
-- That is what makes these views reconcile against <group>.ai_spend_month by construction.
--
-- ── THE HONESTY MECHANISM: cost_allocation_basis ─────────────────────────────────
-- Model serving bills two entirely different ways, and the difference decides whether a
-- $/token figure is a measurement or a fabrication:
--
--   pay_per_token           tokens ARE the meter. Splitting the endpoint's charge by a
--                           requester's token share is a proportional split of a per-token
--                           charge → 'measured_tokens', allocated_cost populated.
--   provisioned_throughput  billed per provisioned HOUR. An idle provisioned endpoint bills
--   provisioned_compute     real money with ZERO tokens, so a token-share split hands the
--                           idle hours to whoever happened to send traffic → 'unallocated',
--                           allocated_cost NULL, the whole charge named in unallocated_cost.
--   external                Databricks bills the gateway hop; the model vendor bills the
--                           tokens on a bill this lake never sees → 'external_passthrough'.
--                           Tokens are real, Databricks dollars are NOT the token cost.
--   unknown                 mode undetermined, or served_entities disagrees with the FOCUS
--                           SKU. Costs us a $/token claim rather than making a wrong one.
--
-- Token counts are honest for every basis. Only the dollars are conditional. NULL in a cost
-- column here means "not allocatable by token", NEVER "$0" — do not coalesce it to zero.


-- Month-typed telemetry spine. charge_month is a 'YYYY-MM' partition string on disk.
CREATE OR REPLACE VIEW gold._ai_usage_typed AS
SELECT
    provider_name,
    strptime(charge_month, '%Y-%m')::date                AS charge_month,
    endpoint_id,
    endpoint_name,
    served_entity_id,
    model_name,
    model_version,
    model_kind,
    serving_mode,
    requester,
    usage_context_project,
    scale_to_zero_enabled,
    workload_size,
    workload_type,
    min_provisioned_throughput,
    request_count,
    error_request_count,
    input_tokens,
    output_tokens,
    input_tokens + output_tokens                         AS total_tokens,
    error_input_tokens + error_output_tokens             AS error_tokens
FROM metrics.ai_usage;


-- The endpoint's own spend and project tag, from the FOCUS plane.
--
-- Scoped by service_category alone, deliberately NARROWER than gold.ai_spend_month (which
-- also unions in Genie and the monitoring products). This CTE exists only to put a dollar
-- figure next to *request telemetry*, and only endpoint-shaped products have any: Genie has
-- no serving endpoint in system.serving and no token rows, so including it here would add
-- cost with no telemetry to join it to and inflate the "endpoint with cost but no tokens"
-- coverage bucket with products that could never have had tokens. Genie's spend is reported
-- on the AI Costs tab from ai_spend_month; it is simply not part of the token join.
CREATE OR REPLACE VIEW gold._ai_endpoint_cost AS
SELECT
    provider_name,
    resource_id                                          AS endpoint_id,
    charge_month,
    sum(cost)                                            AS net_cost,
    -- The billing SHAPE, cross-checked against served_entities below. The vendored FOCUS
    -- query proves MODEL_SERVING billed on an ALL_PURPOSE SKU is provisioned throughput
    -- backed by a classic cluster (databricks_focus_1_3.sql:237-242) — a Databricks-authored
    -- fact, so it is what we grade the serving_mode guess against.
    CASE
        WHEN bool_or(sku_id ILIKE '%ALL_PURPOSE%') THEN 'hourly_compute'
        WHEN bool_or(sku_id ILIKE '%MODEL_SERVING%')
          OR bool_or(sku_id ILIKE '%INFERENCE%')   THEN 'model_serving_sku'
        ELSE 'other'
    END                                                  AS billing_shape,
    max(
        (
            SELECT json_extract_string(f.tags, '$."' || t.k || '"')
            FROM unnest(json_keys(f.tags)) AS t(k)
            WHERE replace(lower(trim(t.k)), '-', '_') = 'project'
            LIMIT 1
        )
    )                                                    AS endpoint_tag_project
FROM silver.focus_normalized f
WHERE service_category = 'AI and Machine Learning'
  AND resource_id IS NOT NULL
GROUP BY provider_name, resource_id, charge_month;


-- Endpoint-month spine: cost beside tokens, with the basis decided once.
--
-- FULL OUTER JOIN on purpose. An endpoint with cost but no token rows MUST still appear —
-- that is either an endpoint whose telemetry was never measured or a genuinely silent
-- provisioned endpoint, and dropping it would make "unmeasured" indistinguishable from
-- "efficient". A token row with no matching FOCUS cost is a reconciliation gap and must be
-- visible rather than silently discarded.
CREATE OR REPLACE VIEW gold._ai_endpoint_base AS
WITH rolled AS (
    SELECT
        provider_name,
        charge_month,
        endpoint_id,
        max(endpoint_name)                               AS endpoint_name,
        max(serving_mode)                                AS serving_mode,
        -- bool_and: an endpoint is only "scale to zero enabled" if every served entity on
        -- it is. A mixed endpoint is the conservative answer, not a coin flip.
        bool_and(scale_to_zero_enabled)                  AS scale_to_zero_enabled,
        max(workload_type)                               AS workload_type,
        sum(input_tokens)                                AS input_tokens,
        sum(output_tokens)                               AS output_tokens,
        sum(total_tokens)                                AS total_tokens,
        sum(error_tokens)                                AS error_tokens,
        sum(request_count)                               AS request_count,
        sum(error_request_count)                         AS error_request_count
    FROM gold._ai_usage_typed
    GROUP BY provider_name, charge_month, endpoint_id
)
SELECT
    coalesce(u.provider_name, c.provider_name)           AS provider_name,
    coalesce(u.charge_month, c.charge_month)             AS charge_month,
    coalesce(u.endpoint_id, c.endpoint_id)               AS endpoint_id,
    coalesce(u.endpoint_name, c.endpoint_id)             AS endpoint_name,
    coalesce(u.serving_mode, 'unknown')                  AS serving_mode,
    u.scale_to_zero_enabled,
    u.workload_type,
    c.billing_shape,
    c.endpoint_tag_project,
    c.net_cost,
    u.input_tokens,
    u.output_tokens,
    u.total_tokens,
    u.error_tokens,
    u.request_count,
    u.error_request_count,
    -- 'measured' only when a token row actually arrived. An endpoint present in the bill and
    -- absent from the telemetry is 'no_token_telemetry' — the coverage caption's denominator.
    CASE WHEN u.endpoint_id IS NULL THEN 'no_token_telemetry' ELSE 'measured' END
                                                         AS token_coverage_status,
    CASE
        -- served_entities vs the FOCUS SKU, ONE genuine disagreement: pay-per-token billed on
        -- an hourly ALL_PURPOSE SKU. Pay-per-token is metered per token by definition, so an
        -- hourly SKU under it means served_entities and the bill describe different things —
        -- 'unknown' forfeits the rate rather than asserting a wrong one.
        --
        -- Provisioned throughput pairs with EITHER shape legitimately and is NOT cross-checked:
        -- normally a MODEL_SERVING/INFERENCE SKU, but the vendored FOCUS query documents
        -- provisioned throughput running on a classic cluster and billing as
        -- ENTERPRISE_ALL_PURPOSE_COMPUTE (databricks_focus_1_3.sql:237-242). Treating the
        -- MODEL_SERVING pairing as a conflict would push every ordinary provisioned endpoint
        -- to 'unknown' and hide it from the named unallocated bucket this design exists to
        -- surface.
        WHEN u.serving_mode = 'pay_per_token' AND c.billing_shape = 'hourly_compute'
            THEN 'unknown'
        WHEN u.serving_mode = 'pay_per_token'            THEN 'measured_tokens'
        WHEN u.serving_mode = 'external'                 THEN 'external_passthrough'
        WHEN u.serving_mode IN ('provisioned_throughput', 'provisioned_compute')
            THEN 'unallocated'
        ELSE 'unknown'
    END                                                  AS cost_allocation_basis
FROM rolled u
FULL OUTER JOIN gold._ai_endpoint_cost c
  ON  c.provider_name = u.provider_name
 AND  c.endpoint_id   = u.endpoint_id
 AND  c.charge_month  = u.charge_month;


-- Per-record allocated cost — the one place a dollar is split by token share, and only
-- where tokens are the meter. NULLIF guards a pay-per-token endpoint billed with zero
-- measured tokens (division by zero → NULL, which is the right answer anyway).
CREATE OR REPLACE VIEW gold._ai_usage_allocated AS
SELECT
    u.*,
    b.cost_allocation_basis,
    b.endpoint_tag_project,
    b.net_cost                                           AS endpoint_net_cost,
    b.total_tokens                                       AS endpoint_total_tokens,
    CASE
        WHEN b.cost_allocation_basis = 'measured_tokens'
        THEN b.net_cost * u.total_tokens / nullif(b.total_tokens, 0)
    END                                                  AS allocated_cost
FROM gold._ai_usage_typed u
JOIN gold._ai_endpoint_base b
  ON  b.provider_name = u.provider_name
 AND  b.charge_month  = u.charge_month
 AND  b.endpoint_id   = u.endpoint_id;


-- ── "What does each AI endpoint cost, and what did it serve?" ─────────────────────
CREATE OR REPLACE VIEW gold.endpoint_month AS
SELECT
    provider_name,
    charge_month,
    endpoint_id,
    endpoint_name,
    serving_mode,
    coalesce(workload_type, '(none)')                    AS workload_type,
    scale_to_zero_enabled,
    cost_allocation_basis,
    token_coverage_status,
    net_cost,
    -- The named complement of allocated_cost. Never overlaps it, so the two must never be
    -- summed into a "total AI cost by project" — that would double-count.
    CASE WHEN cost_allocation_basis <> 'measured_tokens' THEN net_cost END
                                                         AS unallocated_cost,
    input_tokens,
    output_tokens,
    total_tokens,
    error_tokens,
    request_count,
    error_request_count,
    CASE WHEN request_count > 0
         THEN round(100.0 * error_request_count / request_count, 2) END
                                                         AS error_rate_pct,
    CASE WHEN cost_allocation_basis = 'measured_tokens' AND total_tokens > 0
         THEN round(1e6 * net_cost / total_tokens, 4) END AS cost_per_million_tokens
FROM gold._ai_endpoint_base;


-- ── "Which model is the money going to, and at what rate?" ────────────────────────
CREATE OR REPLACE VIEW gold.model_month AS
SELECT
    provider_name,
    charge_month,
    endpoint_id,
    endpoint_name,
    coalesce(model_name, '(unknown)')                    AS model_name,
    coalesce(model_version, '(unknown)')                 AS model_version,
    coalesce(model_kind, '(unknown)')                    AS model_kind,
    serving_mode,
    cost_allocation_basis,
    sum(input_tokens)                                    AS input_tokens,
    sum(output_tokens)                                   AS output_tokens,
    sum(total_tokens)                                    AS total_tokens,
    sum(request_count)                                   AS request_count,
    sum(error_request_count)                             AS error_request_count,
    sum(allocated_cost)                                  AS allocated_cost,
    CASE WHEN cost_allocation_basis = 'measured_tokens' AND sum(total_tokens) > 0
         THEN round(1e6 * sum(allocated_cost) / sum(total_tokens), 4) END
                                                         AS cost_per_million_tokens
FROM gold._ai_usage_allocated
GROUP BY provider_name, charge_month, endpoint_id, endpoint_name,
         coalesce(model_name, '(unknown)'), coalesce(model_version, '(unknown)'),
         coalesce(model_kind, '(unknown)'), serving_mode, cost_allocation_basis;


-- ── "How many tokens is each project using, and what does it cost?" ───────────────
-- project_key is NEVER NULL. Unattributed spend is the largest bucket on real data and is
-- the finding itself — the same invariant efficiency.waste_by_owner_month.owner_key carries.
-- Request-level usage_context wins over the endpoint tag when present: it is the finer,
-- more specific fact. project_source makes which one answered visible so a reader can tell
-- 26%-from-tags from 26%-from-instrumentation.
CREATE OR REPLACE VIEW gold.project_month AS
SELECT
    provider_name,
    charge_month,
    coalesce(usage_context_project, endpoint_tag_project, '(unattributed)') AS project_key,
    CASE
        WHEN usage_context_project IS NOT NULL THEN 'usage_context'
        WHEN endpoint_tag_project  IS NOT NULL THEN 'endpoint_tag'
        ELSE 'none'
    END                                                  AS project_source,
    serving_mode,
    cost_allocation_basis,
    sum(input_tokens)                                    AS input_tokens,
    sum(output_tokens)                                   AS output_tokens,
    sum(total_tokens)                                    AS total_tokens,
    sum(request_count)                                   AS request_count,
    sum(error_request_count)                             AS error_request_count,
    sum(allocated_cost)                                  AS allocated_cost,
    count(DISTINCT endpoint_id)                          AS endpoint_count,
    count(DISTINCT model_name)                           AS model_count
FROM gold._ai_usage_allocated
GROUP BY provider_name, charge_month,
         coalesce(usage_context_project, endpoint_tag_project, '(unattributed)'),
         CASE
             WHEN usage_context_project IS NOT NULL THEN 'usage_context'
             WHEN endpoint_tag_project  IS NOT NULL THEN 'endpoint_tag'
             ELSE 'none'
         END,
         serving_mode, cost_allocation_basis;


-- ── "Which user or service principal is using how many tokens?" ───────────────────
-- Same never-NULL key discipline. A bare UUID identity is a service principal, not a
-- person — the fold 056_gold_owner_leaderboard.sql applies to owner_user, reused here so
-- the two leaderboards classify an identity the same way.
CREATE OR REPLACE VIEW gold.requester_month AS
WITH folded AS (
    SELECT
        provider_name,
        charge_month,
        nullif(trim(requester), '')                      AS requester_raw,
        lower(nullif(trim(requester), ''))               AS requester_folded,
        serving_mode,
        cost_allocation_basis,
        endpoint_id,
        model_name,
        input_tokens,
        output_tokens,
        total_tokens,
        request_count,
        error_request_count,
        allocated_cost
    FROM gold._ai_usage_allocated
),
keyed AS (
    SELECT
        provider_name,
        charge_month,
        coalesce(requester_folded, '(unattributed)')     AS requester_key,
        CASE
            WHEN requester_folded IS NULL THEN 'unattributed'
            WHEN regexp_full_match(
                     requester_folded,
                     '[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}'
                 ) THEN 'service_principal'
            ELSE 'user'
        END                                              AS requester_kind,
        serving_mode,
        cost_allocation_basis,
        endpoint_id,
        model_name,
        requester_raw,
        input_tokens,
        output_tokens,
        total_tokens,
        request_count,
        error_request_count,
        allocated_cost
    FROM folded
),
agg AS (
    SELECT
        provider_name,
        charge_month,
        requester_key,
        requester_kind,
        serving_mode,
        cost_allocation_basis,
        -- The spelling from the heaviest row, so the display name is the one attached to the
        -- most usage rather than whichever sorted first.
        max_by(requester_raw, total_tokens)              AS requester_raw,
        sum(input_tokens)                                AS input_tokens,
        sum(output_tokens)                               AS output_tokens,
        sum(total_tokens)                                AS total_tokens,
        sum(request_count)                               AS request_count,
        sum(error_request_count)                         AS error_request_count,
        sum(allocated_cost)                              AS allocated_cost,
        count(DISTINCT endpoint_id)                      AS endpoint_count,
        count(DISTINCT model_name)                       AS model_count
    FROM keyed
    GROUP BY provider_name, charge_month, requester_key, requester_kind, serving_mode,
             cost_allocation_basis
)
SELECT
    provider_name,
    charge_month,
    requester_key,
    -- Display only. requester_key keeps the full identity so an agent's filter stays exact.
    CASE
        WHEN requester_kind = 'service_principal'
            THEN 'Service principal ' || left(requester_key, 8)
        WHEN requester_kind = 'unattributed' THEN '(no requester recorded)'
        ELSE coalesce(requester_raw, requester_key)
    END                                                  AS requester_display,
    requester_kind,
    serving_mode,
    cost_allocation_basis,
    input_tokens,
    output_tokens,
    total_tokens,
    request_count,
    error_request_count,
    allocated_cost,
    endpoint_count,
    model_count
FROM agg;
