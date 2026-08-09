# How to Generate Synthetic Data for the Snowflake Demo Dashboard

> Step-by-step guide to generating, customizing, and validating the synthetic dataset that powers the Flashlight Snowflake cost visibility dashboard.

---

## Prerequisites

- Python 3.12+ with the project virtual environment set up
- Dependencies: `numpy`, `pandas`, `pyarrow` (included in project `.venv`)

```bash
# From the project root
cd auralake-main
.venv/bin/python --version   # Should be 3.12+
```

---

## Quick Start

### Generate the full dataset

```bash
uv run fl sample
```

Expected output:
```
Generating synthetic Snowflake ACCOUNT_USAGE data for $3M/year demo account...
  Account: ACME_ANALYTICS
  Period: 2026-01-01 to 2026-07-24
  Monthly credits target: 84,000
  Annual cost target: $4,032,000

  warehouse_metering_history: 78336 rows, 381344 total credits
  warehouse_load_history: 78336 rows
  query_history: 1470000 rows
  query_attribution_history: 1426075 rows, 84000 credits
  storage_usage: 204 rows
  table_storage_metrics: 200 rows
  tag_references: 63 rows (13/16 WH tagged, 11 fully tagged)
  warehouse_events_history: 15858 rows
  metering_history (AI + managed): 3264 rows, 57490 AI credits, 68218 managed svc credits
  cortex_ai_functions_usage_history: 2040 rows
  cortex_search_daily_usage_history: 1836 rows
  metering_daily_history: 3672 rows
  automatic_clustering_history: 1020 rows
  pipe_usage_history: 1020 rows
  serverless_task_history: 1224 rows
  data_transfer_history: 816 rows
  hidden_waste_compute: 18 rows, $25,341
  hidden_waste_storage: 11 rows, $5,212/mo
  hidden_waste_ai: 11 rows, $27,368

Done! Files written to: /path/to/snowflake/synthetic_data
```

### Start the dashboard

```bash
.venv/bin/python -m flashlight dashboard serve
# Or using the CLI:
uv run flashlight dashboard serve
```

The dashboard reads Parquet files automatically from `snowflake/synthetic_data/` — no additional configuration needed.

---

## What Gets Generated

The generator creates **19 Parquet files** modeling a realistic Snowflake enterprise account:

| File | Rows (typical) | Models |
|------|---------------|--------|
| warehouse_metering_history.parquet | ~78K | Credit consumption per warehouse/hour |
| warehouse_load_history.parquet | ~78K | Warehouse utilization metrics |
| query_history.parquet | ~1.4M | Individual query records |
| query_attribution_history.parquet | ~1.4M | Per-query credit attribution |
| storage_usage.parquet | ~200 | Daily account storage |
| table_storage_metrics.parquet | 200 | Top tables by size |
| tag_references.parquet | ~63 | Cost attribution tags |
| warehouse_events_history.parquet | ~16K | Suspend/resume lifecycle |
| metering_history.parquet | ~3.3K | AI + managed service credits |
| cortex_ai_functions_usage_history.parquet | ~2K | Cortex AI function details |
| cortex_search_daily_usage_history.parquet | ~1.8K | Search service usage |
| metering_daily_history.parquet | ~3.7K | Daily credits by service |
| automatic_clustering_history.parquet | ~1K | Clustering operations |
| pipe_usage_history.parquet | ~1K | Snowpipe ingestion |
| serverless_task_history.parquet | ~1.2K | Task execution |
| data_transfer_history.parquet | ~800 | Cross-region transfers |
| hidden_waste_compute.parquet | ~18 | Compute waste findings |
| hidden_waste_storage.parquet | 11 | Storage waste findings |
| hidden_waste_ai.parquet | 11 | AI waste findings |

---

## Customizing the Dataset

### Change the spend target

Edit `MONTHLY_CREDITS` in `generate.py`:

```python
MONTHLY_CREDITS = 84000   # Current: ~$3M/year at $4/credit
MONTHLY_CREDITS = 42000   # For ~$1.5M/year
MONTHLY_CREDITS = 168000  # For ~$6M/year
```

All credit-based outputs scale proportionally.

### Change the time period

```python
START_DATE = date(2026, 1, 1)  # Change start date
DAYS = (date.today() - START_DATE).days  # Auto-calculates to today
```

### Add a new warehouse

1. Add to the `WAREHOUSES` list (ensure percentages sum to ~1.0):
```python
("NEW_WAREHOUSE", "Medium", 4, "bi", 0.03),
```

2. Add to relevant user profiles in `USER_PROFILES`:
```python
"NEW_USER": {"warehouses": ["NEW_WAREHOUSE", "ANALYTICS"], "pattern": "medium", "type": "compute"},
```

3. Regenerate: `uv run fl sample`

### Add a new user

Add to `USER_PROFILES`:
```python
"TABLEAU_SVC": {"warehouses": ["BI_REPORTS", "ANALYTICS"], "pattern": "good", "type": "compute"},
```

The reverse lookup `_WH_USERS` auto-updates — no other changes needed.

### Adjust storage cost ratio

Edit `generate_storage_usage()`:
```python
base_storage = 900.0 * (1024**4)  # 900 TB → change this for more/less storage cost
```

Storage cost = Total TB × $23/month. Current ~1,100 TB = ~$25K/month = ~13% of TCO.

### Add a new AI waste pattern

Add to `generate_hidden_waste_data()` in the AI section:
```python
rows.append({
    "waste_pattern": "NEW_PATTERN",
    "function_name": "AI_COMPLETE",
    "model_name": "description",
    "task_type": "category",
    "calls_30d": 500,
    "actual_cost_usd": 2000.0,   # Total cost (must be >= wasted)
    "wasted_credits": 300.0,
    "wasted_cost_usd": 1200.0,   # Recoverable portion
    "recommendation": "Action to take",
})
```

### Change growth rate

In `generate_warehouse_metering_history()`:
```python
growth_factor = 1.0 + (months_elapsed * 0.015)  # 1.5%/month
# Change to:
growth_factor = 1.0 + (months_elapsed * 0.03)   # 3%/month (aggressive growth)
```

---

## Validating the Generated Data

### Quick sanity checks

```bash
.venv/bin/python -c "
import pandas as pd

# Check user-warehouse isolation (should be <= 4)
qh = pd.read_parquet('snowflake/synthetic_data/query_history.parquet')
print('Max warehouses per user:', qh.groupby('user_name')['warehouse_name'].nunique().max())

# Check credit reconciliation
attr = pd.read_parquet('snowflake/synthetic_data/query_attribution_history.parquet')
print(f'Total attributed credits: {attr.credits_attributed_compute.sum():,.0f}')

# Check bad vs good user behavior
bad = ['MARKETING_USER', 'DEV_ALICE', 'DEV_CHARLIE', 'ADHOC_USER']
good = ['ETL_SERVICE', 'DBT_RUNNER', 'LOOKER_SVC', 'STREAMING_SVC']
print(f'Bad users avg spill: {qh[qh.user_name.isin(bad)].bytes_spilled_to_local_storage.mean():,.0f}')
print(f'Good users avg spill: {qh[qh.user_name.isin(good)].bytes_spilled_to_local_storage.mean():,.0f}')
print(f'Bad users cache%: {qh[qh.user_name.isin(bad)].percentage_scanned_from_cache.mean():.1f}%')
print(f'Good users cache%: {qh[qh.user_name.isin(good)].percentage_scanned_from_cache.mean():.1f}%')
"
```

### Validate TCO decomposition

```bash
.venv/bin/python -c "
import sys; sys.path.insert(0, 'src')
from flashlight.dashboard import snowflake_visibility_data as sf_data

kpi = sf_data.kpi_summary()
print(f'Monthly TCO: \${kpi[\"total_cost\"]:,.0f}')
print(f'  Compute: \${kpi[\"compute_cost\"]:,.0f}')
print(f'  Serverless: \${kpi[\"serverless_compute_cost\"]:,.0f}')
print(f'  Storage: \${kpi[\"storage_cost\"]:,.0f}')

breakdown = sf_data.cost_breakdown()
print(f'\\nCost breakdown (pie chart):')
for _, row in breakdown.iterrows():
    print(f'  {row[\"category\"]}: \${row[\"cost\"]:,.0f}')
"
```

### Validate waste attribution

```bash
.venv/bin/python -c "
import sys; sys.path.insert(0, 'src')
from flashlight.dashboard import snowflake_visibility_data as sf_data

waste = sf_data.top_users_hidden_waste(5)
print('Top 5 Users Hidden Waste:')
print(waste.to_string())
print(f'\\nTotal attributed waste: \${waste.attributed_waste.sum():,.0f}')
"
```

---

## How the Dashboard Consumes the Data

The data layer (`src/flashlight/dashboard/snowflake_visibility_data.py`) uses **DuckDB** to query the Parquet files:

1. On first call, `_con()` opens an in-memory DuckDB connection
2. All `*.parquet` files from the data directory are registered as tables
3. Table names = file names without `.parquet` extension
4. Queries use standard SQL against these tables

```python
# Example: How kpi_summary() works internally
con.execute("""
    SELECT SUM(credits_used) * 4.0 AS compute_cost
    FROM warehouse_metering_history
    WHERE start_time >= DATE_TRUNC('month', CURRENT_DATE)
""")
```

No configuration file points to the data — the data layer auto-discovers Parquet files in:
```
snowflake/synthetic_data/*.parquet
```

---

## Troubleshooting

### "ModuleNotFoundError: No module named 'numpy'"

Use the project virtual environment:
```bash
uv run fl sample
```

### Dashboard shows stale data

Regenerate and restart:
```bash
uv run fl sample
# Then restart the dashboard (it caches the DuckDB connection)
```

### Numbers don't match between LeaderBoard and Visibility

Both use the same `kpi_summary()` function — they're aligned by construction. If they diverge, check that the same Parquet files are being read (no stale copies in other directories).

### TCO doesn't add up

Verify the cost_breakdown categories sum to kpi_summary total. Common causes:
- Changed `MONTHLY_CREDITS` but didn't regenerate all data
- Added a new service type without updating `metering_daily_history` percentages
- Storage base changed without adjusting the $23/TB calculation

### Forecast is flat

The growth factor (`1.5%/month`) only applies to `warehouse_metering_history`. If the data period is too short (< 3 months), the growth won't be visible in the forecast chart.

---

## Architecture Diagram

```
generate.py
    │
    ├── generate_warehouse_metering_history()  ──→  warehouse_metering_history.parquet
    ├── generate_warehouse_load_history()       ──→  warehouse_load_history.parquet
    ├── generate_query_history()                ──→  query_history.parquet
    │       └── uses USER_PROFILES + _WH_USERS for role-based assignment
    ├── generate_query_attribution_history()    ──→  query_attribution_history.parquet
    │       └── distributes MONTHLY_CREDITS proportional to elapsed time
    ├── generate_storage_usage()               ──→  storage_usage.parquet
    ├── generate_table_storage_metrics()       ──→  table_storage_metrics.parquet
    ├── generate_tag_references()              ──→  tag_references.parquet
    │       └── sorted by spend: top 90% fully tagged
    ├── generate_warehouse_events_history()    ──→  warehouse_events_history.parquet
    ├── generate_metering_history()            ──→  metering_history.parquet
    │       └── AI services (10%) + Managed services (12%)
    ├── generate_cortex_ai_functions_usage()   ──→  cortex_ai_functions_usage_history.parquet
    ├── generate_cortex_search_daily_usage()   ──→  cortex_search_daily_usage_history.parquet
    ├── generate_metering_daily_history()      ──→  metering_daily_history.parquet
    ├── generate_serverless_services_data()    ──→  automatic_clustering_history.parquet
    │                                          ──→  pipe_usage_history.parquet
    │                                          ──→  serverless_task_history.parquet
    │                                          ──→  data_transfer_history.parquet
    └── generate_hidden_waste_data()           ──→  hidden_waste_compute.parquet
                                               ──→  hidden_waste_storage.parquet
                                               ──→  hidden_waste_ai.parquet
```

---

## Extending for New Dashboard Features

When adding a new dashboard view that needs data:

1. **Check if existing Parquet has the data** — most views can be derived from existing datasets via SQL in the data layer
2. **If new data is needed**, add a generator function in `generate.py`:
   ```python
   def generate_new_feature() -> pd.DataFrame:
       rows = []
       for day_offset in range(DAYS):
           # ... build rows
       df = pd.DataFrame(rows)
       pq.write_table(pa.Table.from_pandas(df), OUTPUT_DIR / "new_feature.parquet")
       return df
   ```
3. **Add the call to `main()`** at the bottom of generate.py
4. **The data layer auto-discovers it** — DuckDB registers all `*.parquet` files as tables
5. **Add a query function** in `snowflake_visibility_data.py`:
   ```python
   def new_feature_data():
       return _query("SELECT * FROM new_feature WHERE ...")
   ```
6. **Regenerate**: `uv run fl sample`

---

## Reference

- Full data specification: [`docs/snowflake-synthetic-data-reference.md`](snowflake-synthetic-data-reference.md)
- Dashboard architecture: [`.cortex/skills/snowflake-dashboard.md`](../.cortex/skills/snowflake-dashboard.md)
- Generator source: [`snowflake/synthetic_data/generate.py`](../snowflake/synthetic_data/generate.py)
