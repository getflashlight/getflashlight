-- Databricks system.serving -> AiUsageRecord aggregation (the AI token/unit-economics plane).
--
-- Emits ONE row per (endpoint × served_entity × requester × request-project × month),
-- aggregated AT SOURCE — never one row per request. Substituted by the connector:
-- :start_date, :end_date, :account_prices.
--
-- *** VALIDATED against a live production warehouse 2026-08-05. ***
-- The published docs' column list does NOT match the live schema. Differences baked in:
--
--   endpoint_usage
--     • NO endpoint_name — join identity is served_entity_id only; endpoint_id/name
--       come from served_entities.
--     • NO execution_duration_ms — total_duration_ms is emitted as 0.
--     • Has: requester, status_code, request_time, input/output_token_count,
--       usage_context (MAP), served_entity_id.
--
--   served_entities
--     • NO top-level workload_size / workload_type / scale_to_zero_enabled /
--       min_provisioned_throughput / max_provisioned_throughput.
--     • Config lives in typed structs:
--         foundation_model_config.min/max_provisioned_throughput
--         custom_model_config.compute_type (and min/max_concurrency)
--         feature_spec_config.compute_type
--         external_model_config.provider
--     • scale_to_zero_enabled is NOT exposed — always NULL here, so the
--       endpoint_scale_to_zero_disabled waste rule cannot fire (unmeasured, not off).
--     • workload_type is coalesced from custom/feature compute_type (e.g. 'CPU');
--       workload_size stays NULL (no Small/Medium enum on the live schema).
--
-- The connector still probes system.information_schema.tables and degrades in three
-- rungs (full / usage_only / none) — see _resolve_serving_tables.
--
-- `usage_context` is a MAP<STRING,STRING> and is resolved to a scalar HERE, with element_at,
-- on purpose: _execute stages results as JSON_ARRAY and how the Statement Execution API
-- serializes a MAP cell is not established.
--
-- TWO DOLLAR FIGURES, ON PURPOSE, AND THEY ARE NOT THE SAME THING.
-- The AiUsageRecord itself carries NO cost: the endpoint is a FOCUS ResourceId, so its spend
-- is already canonical in the FOCUS plane and the cost↔token join happens once, in GOLD
-- (080_gold_ai_usage.sql). But the connector ALSO derives endpoint EfficiencyRecords from
-- this same result, and those need a `billed_cost` for the `idle`/`failed` waste rules to
-- price against — so `endpoint_billed_cost` below is computed from list/account prices, the
-- identical way every other EfficiencyRecord in databricks_efficiency.sql gets its
-- billed_cost. Consistent with that plane, not a second source of truth for the AI *views*:
-- ai_usage.endpoint_month.net_cost always comes from the bill.
WITH prices AS (
  SELECT sku_name, cloud, CAST(pricing.default AS DOUBLE) AS unit_price,
         price_start_time,
         COALESCE(price_end_time, DATE_ADD(current_date(), 1)) AS price_end_time
  FROM IDENTIFIER(:account_prices)
  WHERE currency_code = 'USD'
),
endpoint_cost AS (
  SELECT
    u.usage_metadata.endpoint_id                             AS endpoint_id,
    date_trunc('MONTH', u.usage_date)                        AS charge_month,
    SUM(u.usage_quantity)                                    AS dbu_quantity,
    SUM(u.usage_quantity * COALESCE(p.unit_price, 0))        AS billed_cost,
    -- "Has this endpoint been tagged at all", for the endpoint_tagging policy rule. NOTE the
    -- derivation differs from cluster/warehouse tag_count, which reads size(tags) off the
    -- resource's own config row in system.compute.clusters/.warehouses: serving has no such
    -- config table here, so this is the billing-propagated tag set and can include
    -- account-level default tags. MAX over the month so a single tagged usage row counts.
    MAX(size(u.custom_tags))                                 AS tag_count
  FROM system.billing.usage u
  LEFT JOIN prices p
    ON u.sku_name = p.sku_name AND u.cloud = p.cloud
   AND u.usage_end_time >= p.price_start_time
   AND u.usage_end_time <  p.price_end_time
  WHERE u.usage_date BETWEEN :start_date AND :end_date
    AND u.billing_origin_product IN
        ('MODEL_SERVING', 'AI_GATEWAY', 'AI_FUNCTIONS', 'VECTOR_SEARCH')
    AND u.usage_metadata.endpoint_id IS NOT NULL
  GROUP BY u.usage_metadata.endpoint_id, date_trunc('MONTH', u.usage_date)
),
entities AS (
  -- served_entities is slowly-changing; keep the latest row per served_entity_id.
  -- Config fields are projected out of the live structs into the flat names AiUsageRecord
  -- / the serving_mode CASE already speak — so the rest of this file and the Python mapper
  -- stay on one vocabulary.
  SELECT
    served_entity_id,
    endpoint_id,
    endpoint_name,
    entity_type,
    entity_name,
    entity_version,
    CAST(NULL AS BOOLEAN)                                    AS scale_to_zero_enabled,
    CAST(NULL AS STRING)                                     AS workload_size,
    COALESCE(
      custom_model_config.compute_type,
      feature_spec_config.compute_type
    )                                                        AS workload_type,
    CAST(foundation_model_config.min_provisioned_throughput AS DOUBLE)
                                                             AS min_provisioned_throughput,
    CAST(foundation_model_config.max_provisioned_throughput AS DOUBLE)
                                                             AS max_provisioned_throughput
  FROM system.serving.served_entities
  QUALIFY ROW_NUMBER() OVER (PARTITION BY served_entity_id ORDER BY change_time DESC) = 1
),
req AS (
  SELECT
    eu.served_entity_id,
    eu.requester,
    element_at(eu.usage_context, 'project')                  AS usage_context_project,
    date_trunc('MONTH', eu.request_time)                     AS charge_month,
    COUNT(*)                                                 AS request_count,
    SUM(COALESCE(eu.input_token_count, 0))                   AS input_tokens,
    SUM(COALESCE(eu.output_token_count, 0))                  AS output_tokens,
    SUM(CASE WHEN eu.status_code >= 400 THEN 1 ELSE 0 END)   AS error_request_count,
    SUM(CASE WHEN eu.status_code >= 400
             THEN COALESCE(eu.input_token_count, 0) ELSE 0 END)  AS error_input_tokens,
    SUM(CASE WHEN eu.status_code >= 400
             THEN COALESCE(eu.output_token_count, 0) ELSE 0 END) AS error_output_tokens,
    -- Live endpoint_usage has no execution_duration_ms; keep the column so the mapper and
    -- EfficiencyRecord cause_detail stay stable, always zero.
    CAST(0 AS BIGINT)                                        AS total_duration_ms
  FROM system.serving.endpoint_usage eu
  WHERE eu.request_time >= :start_date
    AND eu.request_time <  DATE_ADD(:end_date, 1)
  GROUP BY
    eu.served_entity_id,
    eu.requester,
    element_at(eu.usage_context, 'project'),
    date_trunc('MONTH', eu.request_time)
)
SELECT
  -- endpoint_id is the FOCUS ResourceId join key. Falling back to served_entity_id keeps a
  -- usage_only (no served_entities) row from vanishing entirely — the GOLD join then finds
  -- no cost match and the row shows as a reconciliation gap rather than being dropped.
  COALESCE(e.endpoint_id, r.served_entity_id)                AS endpoint_id,
  e.endpoint_name                                            AS endpoint_name,
  r.served_entity_id,
  e.entity_name                                              AS model_name,
  e.entity_version                                           AS model_version,
  e.entity_type                                              AS model_kind,
  -- The serving-mode ladder. Order matters: a provisioned-throughput foundation model is
  -- also a FOUNDATION_MODEL, so the throughput test must come first. CUSTOM_MODEL /
  -- FEATURE_SPEC are provisioned compute by construction on the live schema (their
  -- compute_type lives in the struct; there is no separate workload_size gate).
  CASE
    WHEN e.min_provisioned_throughput > 0                    THEN 'provisioned_throughput'
    WHEN e.entity_type = 'EXTERNAL_MODEL'                    THEN 'external'
    WHEN e.entity_type = 'FOUNDATION_MODEL'                  THEN 'pay_per_token'
    WHEN e.entity_type IN ('CUSTOM_MODEL', 'FEATURE_SPEC')   THEN 'provisioned_compute'
    ELSE 'unknown'
  END                                                        AS serving_mode,
  r.requester,
  r.usage_context_project,
  e.scale_to_zero_enabled,
  e.workload_size,
  e.workload_type,
  e.min_provisioned_throughput,
  e.max_provisioned_throughput,
  CAST(r.charge_month AS DATE)                               AS charge_month,
  r.request_count,
  r.error_request_count,
  r.input_tokens,
  r.output_tokens,
  r.error_input_tokens,
  r.error_output_tokens,
  r.total_duration_ms,
  -- Endpoint-month scalars, fanned across this endpoint's several req rows. The connector
  -- takes MAX (not SUM) when it aggregates back to endpoint grain for the EfficiencyRecords —
  -- endpoint_cost is already per (endpoint, month), so summing would multiply it by the
  -- number of requesters.
  ec.billed_cost                                             AS endpoint_billed_cost,
  ec.dbu_quantity                                            AS endpoint_dbu_quantity,
  ec.tag_count                                               AS endpoint_tag_count
FROM req r
LEFT JOIN entities e ON e.served_entity_id = r.served_entity_id
LEFT JOIN endpoint_cost ec
       ON ec.endpoint_id = COALESCE(e.endpoint_id, r.served_entity_id)
      AND ec.charge_month = r.charge_month
