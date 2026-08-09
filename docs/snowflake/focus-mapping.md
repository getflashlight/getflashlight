# Snowflake → FOCUS 1.4 Mapping Guide

> How to transform Snowflake `ACCOUNT_USAGE` cost and usage records into a
> FOCUS 1.4-aligned dataset — modeled after the
> [Databricks FOCUS 1.3 reference](https://github.com/databricks-solutions/cloud-infra-costs/tree/main/focus).

**Status**: Snowflake FOCUS export expected GA Q4 2026. Until then, this mapping
is implemented by Flashlight's Snowflake connector.

---

## References

| Resource | URL |
|----------|-----|
| FOCUS Spec | https://focus.finops.org |
| FOCUS Column Library (v1.4) | https://focus.finops.org/focus-columns/?version=v1-4&dataset=cost-and-usage |
| FOCUS 1.3 Data Model (Google Sheet) | https://docs.google.com/spreadsheets/d/1duuzCD4jovfKjfsfVBlWgxPCOOOqsWAXlSA2XLw-iuw |
| Databricks → FOCUS 1.3 SQL | `src/flashlight/ingest/connectors/sql/databricks_focus_1_3.sql` |
| Snowflake Cost Docs | https://docs.snowflake.com/en/user-guide/cost-understanding-overall |

---

## Source Views (Snowflake ACCOUNT_USAGE)

| View | Grain | What It Contains |
|------|-------|-----------------|
| `WAREHOUSE_METERING_HISTORY` | Hourly | Virtual warehouse credit consumption |
| `METERING_HISTORY` | Hourly | Serverless + cloud services credits by `service_type` |
| `STORAGE_USAGE` | Daily | Account-level storage bytes |
| `TABLE_STORAGE_METRICS` | Snapshot | Per-table storage breakdown |
| `DATA_TRANSFER_HISTORY` | Hourly | Cross-region/cross-cloud egress bytes |
| `QUERY_ATTRIBUTION_HISTORY` | Per-query | Attributed compute credits per query |
| `CORTEX_AI_FUNCTIONS_USAGE_HISTORY` | Daily | Per-function/model Cortex credit consumption |
| `USAGE_IN_CURRENCY_DAILY` (ORG_USAGE) | Daily | Dollar-denominated spend (contract rates) |

---

## FOCUS Column Mapping (Cost & Usage Dataset)

Following FOCUS 1.4 column numbering. For each column: Mandatory (M), Conditional (C),
or Recommended (R).

### Identity & Provenance

| # | FOCUS Column | Req | Snowflake Mapping | Notes |
|---|---|---|---|---|
| 1 | `AvailabilityZone` | R | `NULL` | Snowflake is regional, no AZ exposed |
| 2 | `BillingAccountId` | M | `CURRENT_ACCOUNT()` or `account_locator` | Account locator string |
| 3 | `BillingAccountName` | M | `CURRENT_ORGANIZATION_NAME() \|\| '.' \|\| CURRENT_ACCOUNT_NAME()` | Org.Account |
| 4 | `BillingAccountType` | C | `NULL` | Not applicable |

### Billing Period

| # | FOCUS Column | Req | Snowflake Mapping | Notes |
|---|---|---|---|---|
| 5 | `BillingCurrency` | M | `'USD'` | From contract; always single currency |
| 6 | `BillingPeriodEnd` | M | `DATE_TRUNC('MONTH', START_TIME) + INTERVAL '1 MONTH'` | Exclusive end |
| 7 | `BillingPeriodStart` | M | `DATE_TRUNC('MONTH', START_TIME)` | First of month |

### Charge Classification

| # | FOCUS Column | Req | Snowflake Mapping | Notes |
|---|---|---|---|---|
| 8 | `ChargeCategory` | M | `'Usage'` | All metering rows are usage |
| 9 | `ChargeClass` | M | `NULL` | No correction model in ACCOUNT_USAGE |
| 10 | `ChargeDescription` | M | See below | Service-specific description |
| 11 | `ChargeFrequency` | R | `'Usage-Based'` | Per-hour or per-day |

### Charge Period

| # | FOCUS Column | Req | Snowflake Mapping | Notes |
|---|---|---|---|---|
| 12 | `ChargePeriodEnd` | M | `END_TIME` (hourly) or `USAGE_DATE + 1` (daily) | |
| 13 | `ChargePeriodStart` | M | `START_TIME` (hourly) or `USAGE_DATE` (daily) | |

### Commitment Discount (Snowflake Capacity Commitments)

| # | FOCUS Column | Req | Snowflake Mapping | Notes |
|---|---|---|---|---|
| 14 | `CommitmentDiscountCategory` | C | `'Spend'` if on capacity contract | |
| 15 | `CommitmentDiscountId` | C | Contract ID (from `USAGE_IN_CURRENCY_DAILY`) | |
| 16 | `CommitmentDiscountName` | C | `NULL` | Not exposed |
| 17 | `CommitmentDiscountQuantity` | C | `NULL` | |
| 18 | `CommitmentDiscountStatus` | C | `'Used'` | All consumed credits are "used" |
| 19 | `CommitmentDiscountType` | C | `'Prepaid'` | Snowflake capacity = prepaid credits |
| 20 | `CommitmentDiscountUnit` | C | `'Credits'` | |

### Cost Metrics

| # | FOCUS Column | Req | Snowflake Mapping | Notes |
|---|---|---|---|---|
| 21 | `ConsumedQuantity` | C | `CREDITS_USED` or `BYTES_TRANSFERRED` | |
| 22 | `ConsumedUnit` | C | `'Credits'` or `'Bytes'` | |
| 23 | `ContractedCost` | M | `CREDITS_USED × contracted_$/credit` | From contract |
| 24 | `ContractedUnitPrice` | C | Contracted $/credit | |
| 25 | `EffectiveCost` | M | `= ContractedCost` | No amortization to split |
| 26 | `ListCost` | M | `CREDITS_USED × on_demand_$/credit` | Edition list price |
| 27 | `ListUnitPrice` | C | On-demand $/credit by edition | |
| 28 | `BilledCost` | M | `CREDITS_USED × actual_$/credit` | What appears on invoice |

### Provider & Invoice

| # | FOCUS Column | Req | Snowflake Mapping | Notes |
|---|---|---|---|---|
| 29 | `HostProviderName` | M | `CASE cloud_region WHEN 'AWS%' THEN 'Amazon Web Services' WHEN 'AZURE%' THEN 'Microsoft Azure' WHEN 'GCP%' THEN 'Google Cloud Platform' END` | Derived from region |
| 30 | `InvoiceId` | R | `NULL` | Not available in ACCOUNT_USAGE |
| 31 | `InvoiceIssuerName` | M | `'Snowflake'` | |
| 32 | `ProviderName` | M | `'Snowflake'` | DEPRECATED in 1.3; kept for compat |
| 33 | `ServiceProviderName` | M | `'Snowflake'` | New in FOCUS 1.3 |

### Pricing

| # | FOCUS Column | Req | Snowflake Mapping | Notes |
|---|---|---|---|---|
| 34 | `PricingCategory` | C | `'Standard'` or `'Committed'` | Based on contract |
| 35 | `PricingCurrency` | C | `'USD'` | |
| 36 | `PricingQuantity` | M | `= ConsumedQuantity` | |
| 37 | `PricingUnit` | M | `= ConsumedUnit` | |

### Region

| # | FOCUS Column | Req | Snowflake Mapping | Notes |
|---|---|---|---|---|
| 38 | `RegionId` | C | `CURRENT_REGION()` | e.g. `AWS_US_WEST_2` |
| 39 | `RegionName` | C | `CURRENT_REGION()` | Same; no separate display name |

### Resource

| # | FOCUS Column | Req | Snowflake Mapping | Notes |
|---|---|---|---|---|
| 40 | `ResourceId` | C | See resource mapping table below | |
| 41 | `ResourceName` | C | `= ResourceId` | Same for Snowflake |
| 42 | `ResourceType` | C | `'Warehouse'`, `'Pipe'`, `'Table'`, `'Service'` | |

### Service

| # | FOCUS Column | Req | Snowflake Mapping | Notes |
|---|---|---|---|---|
| 43 | `ServiceCategory` | M | See service category mapping below | |
| 44 | `ServiceName` | M | See service name mapping below | |
| 45 | `SkuId` | C | Warehouse size or service_type | |
| 46 | `SkuPriceId` | C | `NULL` | Not exposed |

### Tags

| # | FOCUS Column | Req | Snowflake Mapping | Notes |
|---|---|---|---|---|
| 47 | `Tags` | R | JOIN `TAG_REFERENCES` on warehouse_name | `{owner, cost_center, department, ...}` |

---

## Service Category Mapping

| Snowflake Source | `service_type` Values | FOCUS ServiceCategory |
|---|---|---|
| `WAREHOUSE_METERING_HISTORY` | — | `Compute` |
| `METERING_HISTORY` (compute services) | AUTO_CLUSTERING, SNOWPIPE, SERVERLESS_TASK, MATERIALIZED_VIEW, SEARCH_OPTIMIZATION, QUERY_ACCELERATION, REPLICATION | `Compute` |
| `METERING_HISTORY` (AI) | CORTEX_AI_FUNCTIONS, CORTEX_SEARCH, AI_SERVICES, CORTEX_ANALYST, DOCUMENT_AI, CORTEX_AGENTS, CORTEX_GUARDRAILS | `AI and Machine Learning` |
| `STORAGE_USAGE` | — | `Storage` |
| `DATA_TRANSFER_HISTORY` | — | `Networking` |
| `METERING_HISTORY` (cloud svc) | CLOUD_SERVICES | `Management and Governance` |

---

## Resource ID Mapping

| Source View | ResourceId Logic |
|---|---|
| `WAREHOUSE_METERING_HISTORY` | `WAREHOUSE_NAME` |
| `METERING_HISTORY` | `NAME` (service instance name) |
| `STORAGE_USAGE` | `'ACCOUNT_STORAGE'` (account-level) |
| `TABLE_STORAGE_METRICS` | `TABLE_CATALOG.TABLE_SCHEMA.TABLE_NAME` |
| `DATA_TRANSFER_HISTORY` | `SOURCE_CLOUD:SOURCE_REGION → TARGET_CLOUD:TARGET_REGION` |
| `CORTEX_AI_FUNCTIONS_USAGE_HISTORY` | `FUNCTION_NAME:MODEL_NAME` |

---

## Charge Description Mapping

| Source | ChargeDescription |
|---|---|
| Warehouse | `'Virtual warehouse compute: ' \|\| WAREHOUSE_NAME \|\| ' (' \|\| WAREHOUSE_SIZE \|\| ')'` |
| Serverless | `'Serverless: ' \|\| SERVICE_TYPE \|\| ' — ' \|\| NAME` |
| Storage | `'Account storage: ' \|\| ROUND(bytes/1024^4, 2) \|\| ' TB'` |
| Data Transfer | `'Egress: ' \|\| TRANSFER_TYPE \|\| ' ' \|\| SOURCE_REGION \|\| '→' \|\| TARGET_REGION` |
| AI/Cortex | `'Cortex: ' \|\| FUNCTION_NAME \|\| ' (' \|\| MODEL_NAME \|\| ')'` |

---

## DDL: Snowflake FOCUS Cost Table

```sql
CREATE OR REPLACE TABLE FOCUS_COST_AND_USAGE (
    -- Identity
    AvailabilityZone            VARCHAR,
    BillingAccountId            VARCHAR         NOT NULL,
    BillingAccountName          VARCHAR         NOT NULL,
    BillingAccountType          VARCHAR,

    -- Billing Period
    BillingCurrency             VARCHAR         NOT NULL DEFAULT 'USD',
    BillingPeriodEnd            DATE            NOT NULL,
    BillingPeriodStart          DATE            NOT NULL,

    -- Charge
    ChargeCategory              VARCHAR         NOT NULL,  -- Usage|Purchase|Credit|Adjustment
    ChargeClass                 VARCHAR,                   -- NULL or Correction
    ChargeDescription           VARCHAR         NOT NULL,
    ChargeFrequency             VARCHAR,                   -- Usage-Based
    ChargePeriodEnd             TIMESTAMP_NTZ   NOT NULL,
    ChargePeriodStart           TIMESTAMP_NTZ   NOT NULL,

    -- Commitment Discount
    CommitmentDiscountCategory  VARCHAR,
    CommitmentDiscountId        VARCHAR,
    CommitmentDiscountName      VARCHAR,
    CommitmentDiscountQuantity  DECIMAL(30,15),
    CommitmentDiscountStatus    VARCHAR,
    CommitmentDiscountType      VARCHAR,
    CommitmentDiscountUnit      VARCHAR,

    -- Cost
    ConsumedQuantity            DECIMAL(30,15),
    ConsumedUnit                VARCHAR,
    ContractedCost              DECIMAL(30,15)  NOT NULL,
    ContractedUnitPrice         DECIMAL(30,15),
    EffectiveCost               DECIMAL(30,15)  NOT NULL,
    ListCost                    DECIMAL(30,15)  NOT NULL,
    ListUnitPrice               DECIMAL(30,15),
    BilledCost                  DECIMAL(30,15)  NOT NULL,

    -- Provider
    HostProviderName            VARCHAR         NOT NULL,
    InvoiceId                   VARCHAR,
    InvoiceIssuerName           VARCHAR         NOT NULL DEFAULT 'Snowflake',
    ProviderName                VARCHAR         NOT NULL DEFAULT 'Snowflake',
    ServiceProviderName         VARCHAR         NOT NULL DEFAULT 'Snowflake',

    -- Pricing
    PricingCategory             VARCHAR,
    PricingCurrency             VARCHAR,
    PricingQuantity             DECIMAL(30,15),
    PricingUnit                 VARCHAR,

    -- Region
    RegionId                    VARCHAR,
    RegionName                  VARCHAR,

    -- Resource
    ResourceId                  VARCHAR,
    ResourceName                VARCHAR,
    ResourceType                VARCHAR,

    -- Service
    ServiceCategory             VARCHAR         NOT NULL,
    ServiceName                 VARCHAR         NOT NULL,
    SkuId                       VARCHAR,
    SkuPriceId                  VARCHAR,

    -- Tags
    Tags                        VARIANT,

    -- ═══ Snowflake Extensions (x_ prefix per FOCUS convention) ═══
    x_source_view               VARCHAR,        -- Which ACCOUNT_USAGE view sourced this
    x_service_type              VARCHAR,        -- Raw Snowflake service_type enum
    x_compute_class             VARCHAR,        -- WAREHOUSE | SERVERLESS | CLOUD_SERVICES
    x_warehouse_size            VARCHAR,        -- XS, S, M, L, XL, 2XL, ...
    x_credit_price              DECIMAL(10,4),  -- $/credit used for cost calculation
    x_record_hash               VARCHAR         -- Deduplication key
);
```

---

## Key Differences: Databricks vs Snowflake FOCUS Mapping

| Aspect | Databricks | Snowflake |
|--------|-----------|-----------|
| Source | Single `system.billing.usage` table | Multiple `ACCOUNT_USAGE` views |
| Pricing | `list_prices` + `account_prices` JOIN | No pricing table; must use contract rate |
| Currency | Billed in USD/DBU natively | Billed in credits; convert via $/credit |
| ListCost | `usage_quantity × list_unit_price` | `credits × on_demand_$/credit` |
| ContractedCost | `usage_quantity × account_unit_price` | `credits × contracted_$/credit` |
| ResourceId | Complex CASE by `billing_origin_product` | Simpler: warehouse_name or service NAME |
| Tags | `custom_tags` map column on usage row | Separate `TAG_REFERENCES` view; requires JOIN |
| Corrections | `record_type` RETRACTION/RESTATEMENT | Not available in ACCOUNT_USAGE |
| Data Transfer | Not in Databricks billing | Egress only; separate `DATA_TRANSFER_HISTORY` |
| AI/ML | `MODEL_SERVING`, `AI_FUNCTIONS` SKUs | Separate `CORTEX_AI_FUNCTIONS_USAGE_HISTORY` |
| FOCUS Export | Not available | Expected GA Q4 2026 |

---

## Credit-to-Dollar Conversion

| Edition | List $/credit | Typical Contract |
|---------|--------------|-----------------|
| Standard | $2.00 | $1.50–$2.00 |
| Enterprise | $3.00 | $2.00–$3.00 |
| Business Critical | $4.00 | $3.00–$3.50 |

Flashlight default: `$4.00/credit` (conservative). Configure in `connections.yml`:

```yaml
snowflake:
  account: FZWFQIJ-NA36844
  credit_price: 3.00  # your actual contract rate
```

---

## Mapping SQL (Warehouse Compute Example)

```sql
SELECT
    -- Identity
    NULL                                        AS AvailabilityZone,
    CURRENT_ACCOUNT()                           AS BillingAccountId,
    CURRENT_ACCOUNT_NAME()                      AS BillingAccountName,
    -- Billing Period
    'USD'                                       AS BillingCurrency,
    DATE_TRUNC('MONTH', START_TIME)
      + INTERVAL '1 MONTH'                      AS BillingPeriodEnd,
    DATE_TRUNC('MONTH', START_TIME)             AS BillingPeriodStart,
    -- Charge
    'Usage'                                     AS ChargeCategory,
    NULL                                        AS ChargeClass,
    'Virtual warehouse: ' || WAREHOUSE_NAME
      || ' (' || WAREHOUSE_SIZE || ')'          AS ChargeDescription,
    'Usage-Based'                               AS ChargeFrequency,
    END_TIME                                    AS ChargePeriodEnd,
    START_TIME                                  AS ChargePeriodStart,
    -- Cost
    CREDITS_USED                                AS ConsumedQuantity,
    'Credits'                                   AS ConsumedUnit,
    CREDITS_USED * :contracted_price            AS ContractedCost,
    :contracted_price                           AS ContractedUnitPrice,
    CREDITS_USED * :contracted_price            AS EffectiveCost,
    CREDITS_USED * :list_price                  AS ListCost,
    :list_price                                 AS ListUnitPrice,
    CREDITS_USED * :contracted_price            AS BilledCost,
    -- Provider
    CASE
      WHEN CURRENT_REGION() LIKE 'AWS%' THEN 'Amazon Web Services'
      WHEN CURRENT_REGION() LIKE 'AZURE%' THEN 'Microsoft Azure'
      WHEN CURRENT_REGION() LIKE 'GCP%' THEN 'Google Cloud Platform'
    END                                         AS HostProviderName,
    'Snowflake'                                 AS InvoiceIssuerName,
    'Snowflake'                                 AS ProviderName,
    'Snowflake'                                 AS ServiceProviderName,
    -- Region
    CURRENT_REGION()                            AS RegionId,
    -- Resource
    WAREHOUSE_NAME                              AS ResourceId,
    WAREHOUSE_NAME                              AS ResourceName,
    'Warehouse'                                 AS ResourceType,
    -- Service
    'Compute'                                   AS ServiceCategory,
    'Virtual Warehouse'                         AS ServiceName,
    WAREHOUSE_SIZE                              AS SkuId,
    -- Extensions
    'WAREHOUSE_METERING_HISTORY'                AS x_source_view,
    NULL                                        AS x_service_type,
    'WAREHOUSE'                                 AS x_compute_class,
    WAREHOUSE_SIZE                              AS x_warehouse_size,
    :contracted_price                           AS x_credit_price
FROM SNOWFLAKE.ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY
WHERE START_TIME >= :start_date;
```

---

## What's Missing Until Snowflake Ships Native FOCUS (Q4 2026)

| Gap | Impact | Current Workaround |
|-----|--------|-------------------|
| No `USAGE_IN_CURRENCY` at hourly grain | Can't get exact $ per hour | Multiply credits × contract rate |
| No pricing tables like Databricks `list_prices` | Must hardcode or configure $/credit | `credit_price` in config |
| No `record_type` for corrections | Can't track RETRACTION/RESTATEMENT | Treat all rows as ORIGINAL |
| Tags not on usage rows | Must JOIN `TAG_REFERENCES` separately | Left join on warehouse/resource |
| No `InvoiceId` in ACCOUNT_USAGE | Can't link to specific invoice | Leave NULL |
| Storage billed monthly, not hourly | Daily proration is an approximation | Average daily bytes × daily rate |
| Data Transfer has no dollar amount | Only bytes; must apply rate table | `bytes × $/TB` by region pair |
