# Snowflake Service Types — What Are You Paying For?

When you look at the **Cost Distribution by Service** chart in the Flashlight dashboard,
your spend is grouped into six categories. This document explains what each category
means, which Snowflake service types belong to it, and why you might see costs there.

---

## The Six Cost Categories

### 1. Managed Compute
**What it is:** Credit usage from virtual warehouses — the compute clusters your SQL
queries run on.

This is typically your largest cost. Every time a warehouse runs a query, loads data,
or stays running while idle, it burns credits.

**Includes these service types:**
| Service Type | Plain-English Meaning |
|---|---|
| `WAREHOUSE_METERING` | Your virtual warehouses running queries |
| `WAREHOUSE_METERING_READER` | Warehouses in Snowflake reader accounts (shared data consumers) |

> **Tip:** Idle warehouses with `AUTO_SUSPEND` set too high are the #1 source of
> wasted Managed Compute spend. Check the **Hidden Waste → Compute** tab.

---

### 2. Serverless Compute
**What it is:** Background work that Snowflake runs on your behalf — no warehouse
required. Snowflake spins up compute, does the job, and charges you only for what
it used.

**Includes these service types:**
| Service Type | Plain-English Meaning |
|---|---|
| `AUTOMATIC_CLUSTERING` / `AUTO_CLUSTERING` | Keeping your tables sorted for fast queries |
| `SNOWPIPE` / `PIPE` | Continuous data loading (micro-batch ingestion) |
| `SNOWPIPE_STREAMING` | Real-time row-level streaming ingestion |
| `SERVERLESS_TASK` | Scheduled or triggered SQL jobs |
| `SERVERLESS_ALERTS` | Condition-based monitoring jobs |
| `MATERIALIZED_VIEW` | Maintaining pre-computed query results |
| `SEARCH_OPTIMIZATION` | Maintaining point-lookup indexes |
| `QUERY_ACCELERATION` | Offloading parts of large queries to elastic compute |
| `REPLICATION` | Copying databases/objects across regions or accounts |
| `SNOWPARK_CONTAINER_SERVICES` | Running containerized apps inside Snowflake |
| `HYBRID_TABLE_REQUESTS` | Serving low-latency operational queries on hybrid tables |
| `OPENFLOW_COMPUTE_BYOC` | Openflow pipelines running on your own cloud compute |
| `OPENFLOW_COMPUTE_SNOWFLAKE` | Openflow pipelines running on Snowflake-managed compute |
| `POSTGRES_COMPUTE` | Snowflake Postgres compute resources |
| `POSTGRES_COMPUTE_HA` | Snowflake Postgres high-availability compute |

> **Tip:** Heavy `AUTOMATIC_CLUSTERING` costs often signal tables that are poorly
> clustered or clustered on the wrong column. Check the **Data Design** tab.

---

### 3. AI & ML
**What it is:** Credits used by Snowflake's AI and machine-learning features — mainly
the Cortex family of LLM-powered functions and services.

**Includes these service types:**
| Service Type | Plain-English Meaning |
|---|---|
| `AI_SERVICES` | General Cortex AI compute |
| `CORTEX_AI_FUNCTIONS` | LLM functions: `COMPLETE`, `SUMMARIZE`, `TRANSLATE`, etc. |
| `CORTEX_SEARCH` | Semantic / vector search service |
| `CORTEX_ANALYST` | Natural-language to SQL (Cortex Analyst) |
| `CORTEX_AGENTS` | Agentic AI workflows |
| `CORTEX_GUARDRAILS` | Safety / content filtering for AI outputs |
| `DOCUMENT_AI` | Extracting data from unstructured documents |
| `SNOWFLAKE_INTELLIGENCE` | Snowflake Intelligence product compute |

Also includes credits from **AI-named virtual warehouses** (e.g. `CORTEX_AI`,
`CORTEX_SEARCH`, `ML_TRAINING`, `CORTEX_AGENTS`) which run as Managed Compute
but are classified here because they exclusively serve AI workloads.

> **Tip:** AI & ML costs can spike quickly. The **AI & Cortex** tab shows a daily
> trend so you can spot unexpected growth early.

---

### 4. Storage
**What it is:** The monthly cost of storing your data in Snowflake. Charged per TB
at the on-demand rate (~$23/TB/month).

**Sources:**
- `ACCOUNT_USAGE.STORAGE_USAGE` — active table data, staging files, and Time Travel
  copies
- `FAILSAFE_RECOVERY` — compute cost of recovering data from Fail-safe (rare)
- `ARCHIVE_STORAGE_WRITE` / `ARCHIVE_STORAGE_RETRIEVAL_FILE_PROCESSING` — moving
  data to/from Snowflake's archive storage tier
- `STORAGE_LIFECYCLE_POLICY_EXECUTION` — running storage lifecycle rules that expire
  or archive rows

> **Tip:** Time Travel and Fail-safe retention can double your storage bill. Check
> the **Storage** tab for the breakdown. Shortening `DATA_RETENTION_TIME` on large
> tables is the fastest way to cut storage costs.

---

### 5. Data Transfer
**What it is:** Credits charged when data moves between clouds or regions — for
example, querying a table in AWS us-east-1 from a warehouse in AWS us-west-2, or
replicating to Azure.

**Includes:**
| Service Type | Plain-English Meaning |
|---|---|
| `DATA_TRANSFER` | All cross-cloud and cross-region egress |

> **Tip:** Data transfer costs are usually small, but can grow if you have frequent
> cross-region queries or large replication jobs. Check the **Data Movement** tab.

---

### 6. Other
**What it is:** Any service type in `metering_history` not covered by the five
categories above. Snowflake regularly adds new service types; this bucket ensures
nothing is silently dropped.

**Common entries you may see here:**
| Service Type | Plain-English Meaning |
|---|---|
| `TRUST_CENTER` | Running Trust Center security scans |
| `SENSITIVE_DATA_CLASSIFICATION` | Auto-classifying columns that may contain PII |
| `DATA_QUALITY_MONITORING` | Running data quality checks (DMFs) |
| `TELEMETRY_DATA_INGEST` | Writing logs/traces to the event table |
| `BACKUP` | Immutable backup compute |
| `COPY_FILES` | Moving files between stages with `COPY FILES` |

---

## How the Dashboard Uses These Categories

### Cost Distribution by Service (pie chart)
Shows the **current calendar month** cost split across all six categories.
The pie is the fastest way to answer: *"Where is my Snowflake money going right now?"*

### Cost by Service — Last 12 Months (bar chart)
The same six categories shown month by month as a **stacked bar chart**, using
identical data sources so the current month bar matches the pie exactly.
Use it to answer: *"Is my AI spend growing faster than my compute spend?"*

Both charts read from the same three Snowflake views:
- `ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY` — virtual warehouse credits
- `ACCOUNT_USAGE.METERING_HISTORY` — all serverless service credits
- `ACCOUNT_USAGE.STORAGE_USAGE` — storage bytes by day

---

## Reference

- [Snowflake METERING_HISTORY view](https://docs.snowflake.com/en/sql-reference/account-usage/metering_history) — full list of service types
- [Snowflake credit consumption](https://docs.snowflake.com/en/user-guide/credits) — how credits are charged per service
- [Snowflake storage costs](https://docs.snowflake.com/en/user-guide/cost-understanding-data-storage) — storage pricing explained
