# Aura Lake

Multi-platform lakehouse cost optimization CLI. Analyzes compute, storage, job, and query costs across Databricks (v1), with a provider abstraction ready for Snowflake and AWS Lake Formation.

## Features

- **Cost analysis** — DBU billing, AWS infrastructure costs (EC2/S3/EBS/data transfer), total cost of ownership, RI/Savings Plan recommendations
- **Cluster optimization** — right-sizing, idle detection, autotermination enforcement, spot instance adoption
- **Job optimization** — stale/failing job detection, job consolidation via bin-packing onto shared clusters
- **Delta Lake maintenance** — small file detection, OPTIMIZE, VACUUM, Z-ORDER recommendations
- **Query analysis** — expensive query detection, Spark plan anti-pattern detection (full scans, bad joins, skew, excessive shuffle)
- **Governance** — cluster policy auditing, tag enforcement, budget alerts
- **Workload portability** — Databricks feature lock-in detection with open-source alternative suggestions
- **Discount-aware pricing** — configurable negotiated rates (global DBU discount, SKU overrides, AWS EDP); recommendations reflect actual costs
- **Savings tracking** — projected vs actual savings verified automatically from collected billing data after recommendations are applied
- **Config-driven rules** — 21 analysis rules toggled and tuned per-connection via API without redeploy
- **Progressive automation** — recommend → dry-run → apply → auto, with safety rails and audit trail
- **DAB-aware PR workflow** — modifies Databricks Asset Bundle YAML configs and creates GitHub PRs with savings estimates
- **Collector agent** — continuously captures Spark query plans and metrics to PostgreSQL

## Requirements

- Python >= 3.11
- PostgreSQL (for persistent state, recommendations, audit trail)
- [uv](https://docs.astral.sh/uv/) (recommended package manager)

## Installation

```bash
git clone <repo-url> && cd auralake
uv sync
```

For development dependencies:

```bash
uv sync --extra dev
```

## Quick start (Docker)

1. **Copy `.env.example` and generate an encryption key:**

```bash
cp .env.example .env

# Generate AURALAKE_ENCRYPTION_KEY (pick one method):
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
# or, if you don't have cryptography installed:
openssl rand -base64 32
```

Paste the output into `AURALAKE_ENCRYPTION_KEY=` in `.env`. The server will refuse to start without it — this key encrypts provider credentials (Databricks tokens, AWS keys) at rest in PostgreSQL.

2. **Start the stack:**

```bash
docker compose up -d
```

This starts PostgreSQL, runs Alembic migrations, and launches the backend API on port 8000.

3. **Get the auto-generated API key:**

The server automatically creates the first API key on startup when none exist. Grab it from the logs:

```bash
docker compose logs backend | grep auto_bootstrap
```

To generate a new key at any time, exec into the backend container:

```bash
docker compose exec backend auralake-generate-key
docker compose exec backend auralake-generate-key --name "CI pipeline"
```

Alternatively, run the interactive setup wizard:

```bash
uv run --project packages/cli auralake auth setup
```

4. **Store your API key for CLI use:**

```bash
uv run --project packages/cli auralake auth login \
  --server http://localhost:8000 \
  --key al_<your-key-here>
```

This writes `~/.auralake/credentials.json` (mode 0600) so you don't need to export `AURALAKE_API_KEY` every session.

5. **Add provider connections:**

```bash
# Add a Databricks connection
curl -X POST http://localhost:8000/api/v1/connections \
  -H "Authorization: Bearer al_<your-key>" \
  -H "Content-Type: application/json" \
  -d '{
    "provider": "databricks",
    "name": "prod-workspace",
    "is_default": true,
    "config": {
      "host": "https://mycompany.cloud.databricks.com",
      "sql_warehouse_id": "abc123def456"
    },
    "credentials": {
      "token": "dapi..."
    }
  }'

# Add an AWS connection for infrastructure cost analysis
curl -X POST http://localhost:8000/api/v1/connections \
  -H "Authorization: Bearer al_<your-key>" \
  -H "Content-Type: application/json" \
  -d '{
    "provider": "aws",
    "name": "cost-explorer",
    "config": {
      "region": "us-east-1",
      "cluster_tag_key": "ClusterId"
    },
    "credentials": {
      "access_key_id": "AKIA...",
      "secret_access_key": "..."
    }
  }'
```

Valid providers are: `databricks`, `snowflake`, `lake_formation`, `github`, `aws`, `config` (rule overrides and threshold tuning). Connection names must start with a letter or digit and contain only letters, digits, hyphens, and underscores.

6. **Run your first analysis:**

```bash
auralake cost report
auralake cost breakdown
auralake clusters analyze
```

## Quick start (local dev, no Docker)

1. **Copy and edit the config:**

```bash
cp auralake.yaml.example auralake.yaml
# Edit with your Databricks workspace, AWS, and database details
```

2. **Initialize the database:**

```bash
auralake db init
```

3. **Run a cost report:**

```bash
auralake cost report
auralake cost tco              # Total cost of ownership (DBU + AWS)
auralake cost infra            # AWS infrastructure costs
```

4. **Analyze clusters:**

```bash
auralake clusters analyze      # Right-sizing + idle + spot recommendations
auralake clusters list         # List all clusters with utilization
```

5. **Optimize jobs:**

```bash
auralake jobs analyze          # Stale, failing, and consolidation opportunities
auralake jobs consolidate --pr # Create a GitHub PR with DAB changes
```

6. **Start the collector agent:**

```bash
auralake agent start           # Collect query plans continuously
auralake query plans           # View captured plan anti-patterns
```

## Data Collection

The backend runs a **collector agent** that fetches infrastructure data from Databricks into PostgreSQL. Collection is organized into 9 workers that run in parallel waves.

### Workers

| Worker | Mode | Data Source | Description |
|--------|------|-------------|-------------|
| `compute` | Full sync | REST API + SQL | Clusters, SQL warehouses, DLT pipelines, serving/vector search endpoints |
| `jobs` | Full sync | REST API | Job definitions and metadata |
| `job_runs` | Incremental | REST API | Historical job run records (depends on `jobs`) |
| `billing` | Incremental | `system.billing.usage` | DBU billing with full attribution |
| `query_history` | Incremental | `system.query.history` | SQL query execution history |
| `query_plans` | Incremental | EXPLAIN API | Parsed Spark plans + anti-patterns (depends on `query_history`) |
| `policies` | Full sync | REST API | Cluster policy definitions |
| `infra_costs` | Incremental | AWS Cost Explorer | AWS infrastructure costs (optional) |
| `catalog_tables` | Full sync | Unity Catalog + SQL | Table metadata, stats, and Delta details |

**Full sync** workers re-fetch all data on every run (small datasets like policies, cluster configs).
**Incremental** workers track a cursor/watermark in `core.worker_cursors` and only fetch data newer than the last run.

### Triggering a full collection

A full collection runs all 9 workers in parallel (with dependency ordering):

```bash
# Via CLI
auralake agent collect <connection-id>

# Via API
curl -X POST http://localhost:8000/api/v1/agent/collect/<connection-id> \
  -H "Authorization: Bearer al_<key>"
```

### Retrying a single worker

If a worker fails or you want to re-run just one, use the retry endpoint. This runs only the specified worker with its existing cursor:

```bash
# Via CLI
auralake agent retry <connection-id> <worker-name>

# Via API — retry only the query_history worker
curl -X POST http://localhost:8000/api/v1/agent/retry/<connection-id>/query_history \
  -H "Authorization: Bearer al_<key>"
```

Valid worker names: `compute`, `jobs`, `job_runs`, `billing`, `query_history`, `query_plans`, `policies`, `infra_costs`, `catalog_tables`.

### Checking status

```bash
# All active collections
auralake agent status

# Specific connection — shows per-worker status, count, and duration
auralake agent status <connection-id>

# Collection history
auralake agent history

# Via API
curl http://localhost:8000/api/v1/agent/status/<connection-id> \
  -H "Authorization: Bearer al_<key>"
```

### Cancelling a collection

```bash
auralake agent cancel <connection-id>

curl -X POST http://localhost:8000/api/v1/agent/cancel/<connection-id> \
  -H "Authorization: Bearer al_<key>"
```

### Incremental vs full behavior

- **Incremental workers** (`billing`, `query_history`, `query_plans`, `job_runs`, `infra_costs`) store a cursor in `core.worker_cursors`. On the first run they use a default lookback window (90 days for billing/jobs, 7 days for queries). Subsequent runs only fetch data since the last cursor.
- **Full sync workers** (`compute`, `jobs`, `policies`, `catalog_tables`) always re-fetch all data and upsert by primary key.
- To force a full re-collection of an incremental worker, delete its cursor row from `core.worker_cursors` and retry the worker.

### Pipeline execution order

```
Wave 1 (parallel):  compute, jobs, billing, query_history, policies, infra_costs, catalog_tables
Wave 2 (dependent):  job_runs (after jobs), query_plans (after query_history)
Post-collection:     Analysis (runs all analyzers)
```

Workers that query system tables via SQL (`compute`, `billing`, `query_history`, `query_plans`, `catalog_tables`) share a semaphore (max 2 concurrent) to avoid overloading the SQL warehouse.

## Processing & Analysis

After each collection completes, the backend automatically runs the **analysis pipeline**. This is the processing layer that turns raw collected data into cost-saving recommendations.

```
Collection (FullCollector) → Processing (Analyzers + PricingService) → Recommendations → Dashboard/API
                                                                          ↓
                                                                   SavingsTracker (verifies actual $ from collected data)
```

`AnalysisScheduler.run_all()` executes 8 analyzers in sequence, then runs savings verification:

| # | Analyzer | Rules |
|---|----------|-------|
| 1 | `CostAnalyzer` | `cost_high_sku` |
| 2 | `ClusterAnalyzer` | `cluster_rightsize`, `cluster_idle`, `cluster_no_autotermination`, `cluster_spot_eligible` |
| 3 | `SpotAnalyzer` | `spot_eligible` |
| 4 | `IdleResourceAnalyzer` | `idle_cluster` |
| 5 | `DeltaAnalyzer` | `delta_small_files`, `delta_stale_optimize`, `delta_stale_vacuum`, `delta_over_optimized`, `delta_migrate_to_liquid_clustering`, `delta_enable_clustering` |
| 6 | `S3TagAnalyzer` | `orphan_s3_objects`, `untagged_s3_objects` |
| 7 | `JobAnalyzer` | `job_stale`, `job_failing`, `job_consolidation` |
| 8 | `QueryAnalyzer` | `query_expensive`, `query_anti_pattern` |

Each analyzer checks its rules config (enabled/disabled + thresholds), applies discount-aware pricing when configured, and persists recommendations to `core.recommendations`. After all analyzers finish, `SavingsTracker` verifies actual savings on previously applied recommendations using already-collected billing data.

### Discount-Aware Pricing

Configure negotiated pricing on a Databricks connection to ensure recommendations reflect your actual costs instead of list prices:

```bash
curl -X POST http://localhost:8000/api/v1/connections \
  -H "Authorization: Bearer al_<your-key>" \
  -H "Content-Type: application/json" \
  -d '{
    "provider": "databricks",
    "name": "production",
    "config": {
      "host": "https://mycompany.cloud.databricks.com",
      "sql_warehouse_id": "abc123def456",
      "discounts": {
        "databricks": { "global_dbu_discount_pct": 0.25 },
        "aws": { "edp_discount_pct": 0.10 }
      }
    },
    "credentials": { "token": "dapi..." }
  }'
```

Price resolution order: **SKU override > global discount > list price**.

- `databricks.global_dbu_discount_pct` — percentage off list DBU price (e.g. `0.25` = 25% off)
- `databricks.sku_overrides` — map of SKU name → negotiated $/DBU (takes priority over global discount)
- `aws.edp_discount_pct` — AWS Enterprise Discount Program percentage (e.g. `0.10` = 10% off)

When any discount is configured, recommendations include `pricing_basis: "negotiated"` instead of `"list"`.

### Config-Driven Rules

All 21 analysis rules default to enabled. Toggle or tune rules per-connection via a `config` provider connection — no redeploy required:

```bash
curl -X POST http://localhost:8000/api/v1/connections \
  -H "Authorization: Bearer al_<your-key>" \
  -H "Content-Type: application/json" \
  -d '{
    "provider": "config",
    "name": "rules",
    "config": {
      "rules": {
        "cluster_rightsize": { "thresholds": { "cpu_utilization_low": 15 } },
        "spot_eligible": { "enabled": false }
      }
    }
  }'
```

Each rule supports:
- `enabled` (bool, default `true`) — disable a rule entirely
- `thresholds` (dict) — analyzer-specific tuning knobs (e.g. utilization %, staleness days)

**Rule reference:**

| Analyzer | Rule ID | Description |
|----------|---------|-------------|
| Cost | `cost_high_sku` | Flag expensive Databricks SKUs |
| Cluster | `cluster_rightsize` | Right-size under-utilized clusters |
| Cluster | `cluster_idle` | Detect idle clusters |
| Cluster | `cluster_no_autotermination` | Flag missing autotermination |
| Cluster | `cluster_spot_eligible` | Recommend spot instances for clusters |
| Spot | `spot_eligible` | Broader spot instance adoption |
| Idle | `idle_cluster` | Detect long-idle compute resources |
| Delta | `delta_small_files` | Detect small file problems |
| Delta | `delta_stale_optimize` | Tables needing OPTIMIZE |
| Delta | `delta_stale_vacuum` | Tables needing VACUUM |
| Delta | `delta_over_optimized` | Excessively optimized tables |
| Delta | `delta_migrate_to_liquid_clustering` | Migrate to liquid clustering |
| Delta | `delta_enable_clustering` | Enable clustering on unclustered tables |
| S3 | `orphan_s3_objects` | Detect orphan S3 objects |
| S3 | `untagged_s3_objects` | Detect untagged S3 objects |
| Job | `job_stale` | Detect stale/unused jobs |
| Job | `job_failing` | Detect persistently failing jobs |
| Job | `job_consolidation` | Consolidate jobs onto shared clusters |
| Query | `query_expensive` | Flag expensive queries |
| Query | `query_anti_pattern` | Detect Spark plan anti-patterns |
| Infra | `infra_high_transfer` | Flag high data transfer costs |

### Savings Tracking

Auralake automatically verifies whether applied recommendations deliver the projected savings. No separate job is required — verification runs as part of the normal analysis pipeline using already-collected `billing_records` and `infra_cost_snapshots`.

**Three-column model on each recommendation:**

| Column | Description |
|--------|-------------|
| `estimated_monthly_savings_usd` | Projected savings at recommendation time |
| `baseline_monthly_cost_usd` | Actual monthly cost from billing data *before* the recommendation was applied |
| `actual_monthly_savings_usd` | Difference between pre- and post-application cost from billing data |

Verification triggers automatically for recommendations that have been in `applied` status for 14+ days. `SavingsTracker` compares 30-day billing windows before and after the `applied_at` date, normalizes to monthly rates, and writes the results back to the recommendation record along with `savings_verified_at`.

### Adding a New Rule

Three steps to add a new analysis rule:

1. **Add the rule field to `RulesConfig`** in `packages/backend/src/auralake_shared/models/config.py`:
   ```python
   class RulesConfig(BaseModel):
       # ... existing rules ...
       my_new_rule: RuleConfig = Field(default_factory=RuleConfig)
   ```

2. **Write analysis logic** in the relevant analyzer, gated with `self.rule_enabled()`:
   ```python
   if self.rule_enabled("my_new_rule"):
       # analysis logic that yields Recommendation objects
   ```

3. **(Optional) Add billing column mapping** in `savings_tracker.py` for cost verification:
   ```python
   _BILLING_COLUMN_MAP = {
       # ... existing mappings ...
       "my_new_rule": "cluster_id",  # or job_id, warehouse_id, sku
   }
   ```

The rule auto-activates on deploy (defaults to `enabled: True`). Users can disable or tune it at runtime via the config API without a redeploy.

## CLI commands

```
auralake
├── auth        setup, login, create-key, list-keys, revoke-key
├── cost        report, breakdown, trend, forecast, tco, infra
├── clusters    analyze, list, resize, show
├── resources   scan, cleanup, report
├── spot        analyze, recommend, apply
├── delta       scan, optimize, vacuum, zorder
├── jobs        analyze, consolidate, stale, recommend
├── query       analyze, expensive, plans
├── policies    audit, create, recommend, apply
├── budgets     list, create, update, alerts
├── tags        scan, report, enforce
├── routing     analyze, compare
├── agent       start, stop, status
└── db          init, migrate, status
```

### Global flags

| Flag | Description |
|------|-------------|
| `--provider` | Override provider (`databricks`, `snowflake`, `lake_formation`) |
| `--config` | Path to config file (default: `auralake.yaml`) |
| `--workspace` | Target workspace name |
| `--output` | Output format: `table`, `json`, `csv` |
| `--dry-run` | Show what would happen without making changes |
| `--apply` | Apply changes with interactive confirmation |
| `--auto` | Apply automatically within safety rails |
| `--pr` | Create a GitHub PR with DAB config changes |
| `--verbose` | Enable debug logging |

## Configuration

### Config file resolution (local dev mode)

1. `--config` flag
2. `AURALAKE_CONFIG` environment variable
3. `auralake.yaml` in the current directory
4. `~/.auralake/config.yaml`

### API credential resolution (server mode)

The CLI resolves the server URL and API key in this order:

1. `--server` / `--key` CLI flags (highest priority)
2. `AURALAKE_SERVER_URL` / `AURALAKE_API_KEY` environment variables
3. `~/.auralake/credentials.json` (written by `auralake auth login`)

### Environment variables

| Variable | Description |
|----------|-------------|
| `AURALAKE_DATABASE_URL` | PostgreSQL connection string |
| `AURALAKE_ENCRYPTION_KEY` | Fernet key for encrypting credentials at rest (required for server) |
| `AURALAKE_PROVIDER` | Default provider |
| `AURALAKE_SERVER_URL` | Backend API URL for CLI |
| `AURALAKE_API_KEY` | API key for CLI-to-server auth |
| `GITHUB_TOKEN` | GitHub PAT for PR creation |

See [`auralake.yaml.example`](auralake.yaml.example) for the full config schema.

## Architecture

```
CLI (Typer)
 └── ExecutionContext
      ├── Provider (Databricks / Snowflake / Lake Formation)
      │    ├── CostClient        — platform billing data
      │    ├── InfraCostClient   — AWS Cost Explorer / EC2 / S3 / EBS
      │    ├── ComputeClient     — cluster management
      │    ├── JobClient         — job/workflow management
      │    ├── QueryClient       — query history + EXPLAIN
      │    ├── StorageClient     — Delta table operations
      │    └── ConfigFormat      — DAB YAML parsing (ruamel.yaml)
      ├── Processing Layer
      │    ├── PricingService    — discount-aware DBU + AWS pricing
      │    ├── Analyzers (8)     — config-driven rules → Recommendations
      │    └── SavingsTracker    — verifies actual $ from billing data
      ├── Actions                — mutating operations with risk levels
      ├── AutomationEngine       — recommend / dry-run / apply / auto / PR
      ├── Git Integration        — branch, commit, push, PR via PyGithub
      └── Database (SQLModel)    — PostgreSQL via Alembic migrations
```

### Provider abstraction

Each lakehouse platform implements a common set of interfaces. v1 ships with a full Databricks implementation; Snowflake and Lake Formation are registered as stubs.

| Concept | Databricks | Snowflake | Lake Formation |
|---------|-----------|-----------|----------------|
| Compute right-sizing | Cluster resize | Warehouse scaling | Glue worker count |
| Idle termination | Cluster terminate | Warehouse suspend | Glue job stop |
| Spot instances | Spot workers | N/A | Glue flex execution |
| Storage optimization | Delta OPTIMIZE/VACUUM | Reclustering | S3 lifecycle |
| Config-as-code | DABs (databricks.yml) | Terraform | Terraform/CDK |

## Database Schema

Tables are organized into two PostgreSQL schemas:

### `core` — Application state

| Table | Purpose | Populated By |
|-------|---------|-------------|
| `provider_connections` | Encrypted provider credentials | `POST /connections` API |
| `api_keys` | SHA-256 hashed API keys for auth | `auralake-generate-key` / auto-bootstrap |
| `collection_runs` | Tracks each collection execution | `POST /agent/collect/{id}` |
| `worker_cursors` | Per-worker watermarks for incremental collection | Collection pipeline |
| `analysis_runs` | Analysis execution history | `AnalysisScheduler` (post-collection) |
| `recommendations` | Cost-saving recommendations with pricing basis, projected and verified savings | Analyzers + SavingsTracker |
| `consolidation_groups` | Job consolidation groups | Job analyzer |
| `audit_log` | Actions taken on recommendations | Automation engine |

**`recommendations` columns of note:**

| Column | Type | Description |
|--------|------|-------------|
| `pricing_basis` | `str` | `"list"` or `"negotiated"` — indicates whether discounts were applied |
| `baseline_monthly_cost_usd` | `float?` | Pre-application monthly cost from billing data (set by SavingsTracker) |
| `actual_monthly_savings_usd` | `float?` | Verified savings: baseline minus post-application cost |
| `savings_verified_at` | `datetime?` | When SavingsTracker last verified this recommendation |
| `applied_at` | `datetime?` | When the recommendation was applied (used as the before/after boundary) |

### `inventory` — Collected infrastructure data

| Table | Purpose | Populated By |
|-------|---------|-------------|
| `billing_records` | Databricks DBU billing with full attribution (cluster, job, warehouse, endpoint, pipeline, notebook) | `billing` worker |
| `job_profiles` | Current job metadata (full sync) | `jobs` worker |
| `job_runs` | Historical job run records (incremental) | `job_runs` worker |
| `query_history` | Query history from `system.query.history` | `query_history` worker |
| `query_plans` | Parsed Spark EXPLAIN plans + anti-patterns | `query_plans` worker |
| `unity_catalog_tables` | Unity Catalog table metadata + stats | `catalog_tables` worker |
| `cluster_policies` | Cluster policy definitions (full sync) | `policies` worker |
| `infra_cost_snapshots` | AWS infrastructure costs (optional) | `infra_costs` worker |
| `compute_resources` | Full compute config (clusters, SQL warehouses, DLT pipelines, serving endpoints, vector search endpoints) | `compute` worker |
| `infra_resource_mappings` | Databricks clusters → AWS EC2 mapping | `compute` worker |
| `s3_inventory_objects` | S3 inventory for orphan detection | Inventory collector |

## Development

```bash
uv sync --extra dev

# Lint
uv run ruff check src/ tests/

# Type check
uv run mypy src/auralake/

# Test
uv run pytest

# Run migrations
uv run auralake db migrate
```

## License

Proprietary.
