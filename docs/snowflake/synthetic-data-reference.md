# Snowflake Synthetic Data Reference

> Complete reference for the synthetic data that powers the Flashlight Snowflake demo dashboard.

---

## Purpose

The synthetic data generator (`snowflake/synthetic_data/generate.py`) produces realistic Snowflake `ACCOUNT_USAGE` telemetry for a mixed-workload enterprise account. This data drives the Flashlight Snowflake dashboard without requiring access to a live Snowflake account.

**Target Profile**: Enterprise account "ACME_ANALYTICS" with ≤$50K/month and ≤$600K/year Snowflake spend across ETL, BI, ML/AI, streaming, and development workloads (all cost services retained from the larger reference profile, scaled down).

---

## Snowflake Dictionary Views Modeled

Each generated Parquet file corresponds to a real Snowflake `ACCOUNT_USAGE` or system view:

| # | Snowflake Source View | Generated Parquet File | Description |
|---|----------------------|------------------------|-------------|
| 1 | `SNOWFLAKE.ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY` | warehouse_metering_history.parquet | Credit consumption per warehouse per hour |
| 2 | `SNOWFLAKE.ACCOUNT_USAGE.WAREHOUSE_LOAD_HISTORY` | warehouse_load_history.parquet | Warehouse utilization (running, queued, blocked) |
| 3 | `SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY` | query_history.parquet | Individual query execution records |
| 4 | `SNOWFLAKE.ACCOUNT_USAGE.QUERY_ATTRIBUTION_HISTORY` | query_attribution_history.parquet | Per-query credit attribution |
| 5 | `SNOWFLAKE.ACCOUNT_USAGE.STORAGE_USAGE` | storage_usage.parquet | Account-level daily storage bytes |
| 6 | `SNOWFLAKE.ACCOUNT_USAGE.TABLE_STORAGE_METRICS` | table_storage_metrics.parquet | Per-table storage (active, time travel, failsafe) |
| 7 | `SNOWFLAKE.ACCOUNT_USAGE.TAG_REFERENCES` | tag_references.parquet | Object-level tag assignments for governance |
| 8 | `SNOWFLAKE.ACCOUNT_USAGE.WAREHOUSE_EVENTS_HISTORY` | warehouse_events_history.parquet | Warehouse lifecycle events (suspend/resume) |
| 9 | `SNOWFLAKE.ACCOUNT_USAGE.METERING_HISTORY` | metering_history.parquet | All serverless and managed service metering |
| 10 | `SNOWFLAKE.CORTEX.USAGE_HISTORY` | cortex_ai_functions_usage_history.parquet | Cortex AI function calls, models, tokens |
| 11 | `SNOWFLAKE.ACCOUNT_USAGE.CORTEX_SEARCH_DAILY_USAGE_HISTORY` | cortex_search_daily_usage_history.parquet | Cortex Search service daily usage |
| 12 | `SNOWFLAKE.ACCOUNT_USAGE.METERING_DAILY_HISTORY` | metering_daily_history.parquet | Daily credit aggregation by service type |
| 13 | `SNOWFLAKE.ACCOUNT_USAGE.AUTOMATIC_CLUSTERING_HISTORY` | automatic_clustering_history.parquet | Auto-clustering operations per table |
| 14 | `SNOWFLAKE.ACCOUNT_USAGE.PIPE_USAGE_HISTORY` | pipe_usage_history.parquet | Snowpipe ingestion credits and volumes |
| 15 | `SNOWFLAKE.ACCOUNT_USAGE.SERVERLESS_TASK_HISTORY` | serverless_task_history.parquet | Serverless task execution credits |
| 16 | `SNOWFLAKE.ACCOUNT_USAGE.DATA_TRANSFER_HISTORY` | data_transfer_history.parquet | Cross-region/cross-cloud data transfer |
| 17 | *(Custom: Hidden Waste Analysis)* | hidden_waste_compute.parquet | Compute waste findings (idle, oversized) |
| 18 | *(Custom: Hidden Waste Analysis)* | hidden_waste_storage.parquet | Storage waste findings (stale, clones, TT excess) |
| 19 | *(Custom: Hidden Waste Analysis)* | hidden_waste_ai.parquet | AI/Cortex waste findings (6 patterns) |

### Views NOT Modeled

| Snowflake View | Reason |
|---------------|--------|
| `WAREHOUSES` | Not accessible in demo account; warehouse metadata inferred from metering |
| `RESOURCE_MONITORS` | Referenced in governance SQL but not needed for dashboard |
| `LOGIN_HISTORY` | Outside cost visibility scope |
| `ACCESS_HISTORY` | Outside cost visibility scope |
| `COPY_HISTORY` | Covered indirectly by pipe_usage_history |

---

## Key Factors

### Spend Distribution

| Category | % of TCO | ~Monthly $ | Primary Data Source |
|----------|----------|-----------|---------------------|
| Managed Compute (Warehouses) | 55% | $26K | warehouse_metering_history |
| AI & ML Services | 10% | $5K | metering_history (AI portion) |
| Serverless/Managed Services | 12% | $6K | metering_history (managed portion) |
| Storage | 13% | $6K | storage_usage × $23/TB/month |
| Cloud Services Overhead | 6% | $3K | credits_used_cloud_services |
| Data Transfer | 1% | $0.5K | data_transfer_history |
| Other (QAS, hybrid, etc.) | 3% | $1.5K | metering_daily_history |

### TCO Formula

```
TCO = Managed Compute + AI & ML + Serverless Compute + Storage + Data Transfer
```

All dashboard views decompose TCO into these 5 non-overlapping categories. The sum must always reconcile.

---

## Diversification

### Workload Types (6 categories across 16 warehouses)

| Workload | Warehouses | % of Compute | Peak Pattern |
|----------|-----------|-------------|--------------|
| ETL/Batch | ETL_PROD, DBT_PROD, AIRFLOW | 34% | Night (02:00-08:00), flat on weekends |
| BI/Reporting | BI_REPORTS, LOOKER, FINANCE, MARKETING | 23% | Business hours (09:00-17:00), weekday only |
| Analytics | ANALYTICS | 8% | Business hours, weekday only |
| AI/ML | ML_TRAINING, CORTEX_AI, DATA_SCIENCE, CORTEX_SEARCH, CORTEX_AGENTS | 26% | Extended (06:00-22:00), 7 days/week |
| Streaming | STREAMING | 4% | Flat 24/7 |
| Dev/Adhoc | DEV, ADHOC | 5% | Sporadic (10:00-16:00), weekday only |

### Warehouse Size Tiers

| Size | Credits/Hour | Count | Examples |
|------|-------------|-------|---------|
| 2X-Large | 32 | 1 | ML_TRAINING |
| X-Large | 16 | 1 | ETL_PROD |
| Large | 8 | 4 | DBT_PROD, ANALYTICS, DATA_SCIENCE, CORTEX_AI |
| Medium | 4 | 6 | BI_REPORTS, LOOKER, STREAMING, MARKETING, AIRFLOW, CORTEX_SEARCH, CORTEX_AGENTS |
| Small | 2 | 4 | FINANCE, DEV, ADHOC |

### Growth Pattern

- 1.5% month-over-month linear growth applied to warehouse metering
- Storage grows at 0.2%/day (organic data accumulation)
- Creates realistic upward forecast trend

---

## Users (15 profiles)

### Service Accounts (7 users — automated, predictable)

| User | Warehouses | Pattern | Purpose |
|------|-----------|---------|---------|
| ETL_SERVICE | ETL_PROD, DBT_PROD | good | Production ETL pipelines |
| DBT_RUNNER | DBT_PROD, ETL_PROD | good | dbt model builds |
| LOOKER_SVC | LOOKER, BI_REPORTS | good | Looker dashboard queries |
| ML_PIPELINE | ML_TRAINING, CORTEX_AI | good | Scheduled ML training |
| CORTEX_SVC | CORTEX_AI, CORTEX_SEARCH, CORTEX_AGENTS | medium | Cortex inference serving |
| AIRFLOW_SVC | ETL_PROD, DBT_PROD, AIRFLOW | good | Airflow DAG orchestration |
| STREAMING_SVC | STREAMING | good | Kafka/Snowpipe streaming |

### Human Users (8 users — interactive, variable)

| User | Warehouses | Pattern | Purpose |
|------|-----------|---------|---------|
| ANALYST_JANE | ANALYTICS, BI_REPORTS, FINANCE | medium | Senior analyst, cross-team |
| ANALYST_BOB | ANALYTICS, BI_REPORTS | good | Efficient analyst |
| DS_TEAM | DATA_SCIENCE, ML_TRAINING | medium | Data science exploration |
| FINANCE_RPT | FINANCE, BI_REPORTS | medium | Finance reporting |
| MARKETING_USER | MARKETING, ADHOC | bad | Large scans, no optimization |
| DEV_ALICE | DEV, ADHOC, DATA_SCIENCE | bad | Dev experiments on prod data |
| DEV_CHARLIE | DEV, ADHOC, CORTEX_AI | bad | AI experimentation, wasteful |
| ADHOC_USER | ADHOC, DEV, ANALYTICS | bad | Unoptimized ad-hoc queries |

### Usage Pattern Effects

| Pattern | Users | Elapsed Time | Cache Hit | Spill | Remote Spill |
|---------|-------|-------------|-----------|-------|--------------|
| good | 6 | 0.5-0.8x baseline | 130% of normal | Zero | Zero |
| medium | 4 | Baseline | Normal | Normal | Normal |
| bad | 4 | 1.5-3.0x baseline | 40% of normal | 250% increase | 15% probability |

---

## Warehouses (16 total)

| Warehouse | Size | Type | % Spend | Key Behaviors |
|-----------|------|------|---------|---------------|
| ETL_PROD | X-Large | etl | 20% | Highest spend, night peaks, queue pressure |
| DBT_PROD | Large | etl | 12% | Night peaks, high throughput |
| BI_REPORTS | Medium | bi | 10% | Business hours, queue pressure at peak |
| LOOKER | Medium | bi | 7% | Business hours, steady BI load |
| ANALYTICS | Large | analytics | 8% | Business hours, large scans |
| DATA_SCIENCE | Large | data_science | 6% | Extended hours, exploration |
| ML_TRAINING | 2X-Large | ml | 8% | Largest WH, extended hours, queue pressure |
| CORTEX_AI | Large | ai | 7% | AI inference, extended hours |
| CORTEX_SEARCH | Medium | ai | 3% | Search serving |
| CORTEX_AGENTS | Medium | ai | 2% | Agent orchestration |
| STREAMING | Medium | streaming | 4% | Flat 24/7, steady-state |
| FINANCE | Small | bi | 3% | Business hours, small workload |
| MARKETING | Medium | bi | 3% | Business hours, inefficient users |
| AIRFLOW | Medium | etl | 2% | Night orchestration |
| DEV | Small | dev | 3% | Thrashing (8-20 events/day), weekday only |
| ADHOC | Small | dev | 2% | Thrashing, wasteful queries |

---

## Services

### AI & ML Services (8 services, 10% of credit budget)

| Service Type | % of AI Budget | Description |
|-------------|---------------|-------------|
| CORTEX_AI_FUNCTIONS | 35% | LLM inference (Complete, Summarize, Translate, etc.) |
| CORTEX_SEARCH | 20% | Vector search serving + embedding |
| AI_SERVICES | 15% | ML training workloads |
| CORTEX_ANALYST | 10% | Natural language analytics |
| DOCUMENT_AI | 8% | Document extraction |
| SNOWFLAKE_INTELLIGENCE | 5% | Built-in intelligence features |
| CORTEX_AGENTS | 4% | Agent orchestration |
| CORTEX_GUARDRAILS | 3% | Content safety |

### Managed/Serverless Services (8 services, 12% of credit budget)

| Service Type | % of Managed Budget | Description |
|-------------|-------------------|-------------|
| AUTOMATIC_CLUSTERING | 22% | Table micro-partition optimization |
| SNOWPIPE | 18% | Continuous ingestion |
| SERVERLESS_TASK | 16% | Scheduled task execution |
| REPLICATION | 14% | Cross-region database replication |
| DATA_TRANSFER | 12% | Egress to other clouds |
| SEARCH_OPTIMIZATION | 8% | Point lookup acceleration |
| MATERIALIZED_VIEW | 5% | Pre-computed view maintenance |
| QUERY_ACCELERATION | 5% | Large scan offload |

### Cortex AI Functions (10 function/model pairs)

| Function | Model | % Share |
|----------|-------|---------|
| COMPLETE | llama3.1-70b | 25% |
| COMPLETE | mistral-large2 | 20% |
| COMPLETE | claude-3.5-sonnet | 15% |
| SUMMARIZE | llama3.1-70b | 10% |
| TRANSLATE | snowflake-arctic | 8% |
| SENTIMENT | snowflake-arctic | 7% |
| CLASSIFY_TEXT | llama3.1-8b | 5% |
| EXTRACT_ANSWER | mistral-large2 | 5% |
| EMBED_TEXT_768 | e5-base-v2 | 3% |
| EMBED_TEXT_1024 | voyage-multilingual-2 | 2% |

### Cortex Search Services (3 services)

| Service | Database | % Share | Types |
|---------|----------|---------|-------|
| PRODUCT_SEARCH_SVC | ANALYTICS | 40% | Serving (50%), Embedding (35%), Batch (15%) |
| DOC_SEARCH_SVC | ML_FEATURES | 35% | Serving (50%), Embedding (35%), Batch (15%) |
| SUPPORT_SEARCH_SVC | REPORTING | 25% | Serving (50%), Embedding (35%), Batch (15%) |

### Snowpipe (5 pipes, 3% of monthly credits)

| Pipe | % Share |
|------|---------|
| RAW_EVENTS_PIPE | 35% |
| CLICKSTREAM_PIPE | 25% |
| IOT_TELEMETRY_PIPE | 20% |
| `<internal_or_auto_refresh>` | 10% |
| API_LOGS_PIPE | 10% |

### Serverless Tasks (6 tasks, 3% of monthly credits)

| Task | Database.Schema | % Share |
|------|----------------|---------|
| REFRESH_DASHBOARDS | ANALYTICS.ORCHESTRATION | 25% |
| LOAD_EXTERNAL_DATA | RAW.INGESTION | 20% |
| FEATURE_PIPELINE | ML_FEATURES.ML | 20% |
| AGGREGATE_METRICS | ANALYTICS.CORE | 15% |
| ANOMALY_DETECTOR | REPORTING.ALERTS | 10% |
| CLEANUP_STAGING | RAW.MAINTENANCE | 10% |

### Data Transfer (4 routes)

| Type | Route | % Share |
|------|-------|---------|
| COPY | AWS us-east-1 → AWS eu-west-1 | 40% |
| REPLICATION | AWS us-east-1 → AWS us-west-2 | 30% |
| UNLOAD | AWS us-east-1 → Azure eastus2 | 20% |
| STAGE | AWS us-east-1 → GCP us-central1 | 10% |

---

## Storage Model

| Component | Base Size | Growth | ~Monthly Cost |
|-----------|----------|--------|--------------|
| Active storage | 900 TB | +0.2%/day | $20,700 |
| Stage storage | 120 TB | +0.2%/day | $2,760 |
| Failsafe | 60 TB | Flat | $1,380 |
| Hybrid tables | 20 TB | Flat | $460 |
| Archive (cool) | 40 TB | +0.2%/day | $920 |
| Archive (cold) | 20 TB | +0.2%/day | $460 |
| **Total** | **~1,160 TB** | | **~$26,680/mo** |

---

## Tag Governance

### Required Tags (5)

| Tag | Example Values |
|-----|---------------|
| department | Engineering, Data, Finance, Marketing, Operations, IT |
| environment | prod, staging, dev, sandbox |
| application | Analytics Platform, ML Pipeline, ERP Integration, Customer 360 |
| owner | etl_team, bi_team, ml_team, etc. |
| cost_center | CC_ETL, CC_BI, CC_AI, CC_DEV, etc. |

### Coverage Model

- **Top 90% of spend** (by warehouse cost): Fully tagged (all 5 tags)
- **Next 5%**: Partially tagged (1-3 tags randomly)
- **Bottom 5%**: Completely untagged (governance gap)
- **Quality issues** (~10%): Empty values (4%), placeholders "TBD/TODO" (3%), case mismatches (3%)
- **Database tagging**: Only 45% of databases tagged, 1-3 tags each

---

## Hidden Waste Patterns

### Compute Waste ($25K/30 days)

| Pattern | Scope | Metrics |
|---------|-------|---------|
| IDLE_RUNNING | All 16 warehouses | Dev: 200-500 idle hrs; BI: 50-150 hrs; Others: 10-60 hrs |
| OVERSIZED | Dev/BI only (50% chance) | 50-300 wasted credits per finding |

### Storage Waste ($5.2K/month)

| Pattern | Count | Size | Access Gap |
|---------|-------|------|-----------|
| STALE_TABLE | 6 tables | 5-40 TB each | 90-365 days |
| TIME_TRAVEL_EXCESS | 2 tables | 10-30 TB each | Active but over-retained (90d→7d) |
| ABANDONED_CLONE | 3 clones | 8-25 TB each | 30-180 days |

### AI Waste ($27K/30 days, 6 patterns)

| Pattern | Count | Key Detail |
|---------|-------|-----------|
| OVERSIZED_MODEL | 3 | claude/mistral/llama-70b for simple tasks |
| DUPLICATE_CALLS | 3 | No incremental processing, retries on success |
| VERBOSE_PROMPTS | 1 | 200 tokens filler × 15K calls/month |
| IDLE_SEARCH_SERVICE | 2 | 0 queries in 30 days, still indexing |
| AGENT_LOOP | 1 | No token budget or time limit |
| DEV_IN_PROD | 1 | Dev role using expensive prod models |

---

## Roles & Access Control

| Role | Assigned Users | Warehouse Scope |
|------|---------------|----------------|
| ETL_ROLE | ETL_SERVICE, DBT_RUNNER, STREAMING_SVC | ETL, batch, streaming warehouses |
| ANALYST_ROLE | ANALYST_JANE, ANALYST_BOB, LOOKER_SVC, FINANCE_RPT | BI, analytics warehouses |
| DATA_SCIENCE_ROLE | DS_TEAM, ANALYST_JANE | Data science, analytics |
| ML_ROLE | ML_PIPELINE, DS_TEAM | ML training, AI warehouses |
| AI_ROLE | CORTEX_SVC | All Cortex warehouses |
| SYSADMIN | AIRFLOW_SVC | Cross-team ETL access |
| PUBLIC | DEV_ALICE, DEV_CHARLIE, ADHOC_USER | Dev + limited prod (drives waste) |

---

## Validation Rules

| # | Rule | How Verified |
|---|------|-------------|
| 1 | TCO categories sum to total | cost_breakdown() pie = kpi_summary() total |
| 2 | No user on >4 warehouses | `qh.groupby('user_name')['warehouse_name'].nunique().max() <= 4` |
| 3 | Attribution credits = MONTHLY_CREDITS | `query_attribution_history.credits_attributed_compute.sum() == MONTHLY_CREDITS` |
| 4 | 90% tag coverage by spend | Tag sort by warehouse pct descending, cumulative ≤ 0.90 = full tags |
| 5 | actual_cost >= wasted_cost | Every waste row in all 3 waste tables |
| 6 | Bad users have worse metrics | avg spill (bad) >> avg spill (good); cache% (bad) < cache% (good) |
| 7 | Storage ~13% of TCO | ~$5K storage / ~$48K total monthly |
| 8 | Growth visible in forecast | Later months > earlier months in metering_history |
| 9 | Seed reproducibility | SEED=42, same output every run |
| 10 | Weekend reduction | Queries × 0.6 on weekends vs × 1.2 weekdays |
