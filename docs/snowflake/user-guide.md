# Snowflake User Guide — Flashlight Cost Governance

> **For operations teams managing Snowflake spend.**
> This guide covers setting up cost policies, managing tag governance,
> and using the Flashlight dashboard to control Snowflake costs.

---

## 1. Getting Started

### Prerequisites
- Flashlight installed (`pip install getflashlight`)
- Snowflake account with `ACCOUNTADMIN` (or a role with access to `ORGANIZATION_USAGE` and `ACCOUNT_USAGE`)
- Connection configured in `connections.yml`

### Initial Setup

```bash
flashlight init                    # Creates config directory + default policies
flashlight sample                  # (optional) Load demo data for testing
flashlight dashboard serve         # Start dashboard at http://127.0.0.1:8501
```

After `flashlight init`, two config files are created under `FLASHLIGHT_HOME/config/`:
- `connections.yml` — Snowflake connection credentials
- `policies.yml` — Cost governance policies (your rules)

---

## 2. Configuring the Snowflake Connection

Edit `<FLASHLIGHT_HOME>/config/connections.yml`:

```yaml
connectors:
  - type: snowflake
    enabled: true
    account: "your-account-identifier"    # e.g. xy12345.us-east-1
    user_env: SNOWFLAKE_USER              # env var holding your username
    password_env: SNOWFLAKE_PASSWORD      # env var holding your password
    role: ACCOUNTADMIN                    # or a custom role with required grants
    database: SNOWFLAKE                   # the shared SNOWFLAKE database
    warehouse: COMPUTE_WH                # optional, for query execution
```

### Authentication Options

| Method | Config |
|--------|--------|
| Password | `user_env` + `password_env` (environment variables) |
| Key-pair | `private_key_path: ~/.ssh/snowflake_rsa_key.pem` |
| SSO/External | `authenticator: externalbrowser` |

### Required Grants

```sql
-- Minimum grants for the Flashlight role
GRANT IMPORTED PRIVILEGES ON DATABASE SNOWFLAKE TO ROLE flashlight_role;
-- This gives access to:
--   SNOWFLAKE.ORGANIZATION_USAGE.USAGE_IN_CURRENCY_DAILY (billing)
--   SNOWFLAKE.ACCOUNT_USAGE.* (operational telemetry)
```

---

## 3. Ingesting Snowflake Cost Data

```bash
# Pull billing data and rebuild GOLD views
flashlight ingest

# Or just rebuild GOLD from existing data (no re-pull)
flashlight transform
```

The Snowflake connector reads from `ORGANIZATION_USAGE.USAGE_IN_CURRENCY_DAILY` — this is already in your billing currency (no credit-to-dollar conversion needed).

### What Gets Ingested

| Source | Data | Grain |
|--------|------|-------|
| `USAGE_IN_CURRENCY_DAILY` | All Snowflake costs | (account, service_type, usage_type, day) |

Service types automatically mapped: `WAREHOUSE_METERING`, `CLOUD_SERVICES`, `QUERY_ACCELERATION`, `SERVERLESS_COMPUTE`, `STORAGE`, `DATA_TRANSFER`, `REPLICATION`, `AI_SERVICES`.

---

## 4. Cost Governance Policies

### Overview

Flashlight ships with **11 default policies** that detect common Snowflake cost issues. Operations teams can:
- **Customize thresholds** to fit their environment
- **Enable/disable** policies as needed
- **Add new policies** for custom rules
- **Monitor violations** via dashboard or CLI

### Viewing Policies

```bash
# List all policies
flashlight policies list

# Output:
#   ✓ [warning ] idle-warehouse-15min: Shutdown idle warehouses after 15 minutes
#   ✓ [critical] oversized-warehouse-approval: 4XL+ warehouse requires Director approval
#   ✓ [blocker ] required-tags-missing: All warehouses must have required tags
#   ... (11 total)
```

### Evaluating Policies

```bash
# Check current data against all enabled policies
flashlight policies evaluate

# Output shows violations:
#   [warning ] ETL_PROD
#              Policy: Shutdown idle warehouses after 15 minutes
#              Detail: avg_running=0.042 with 3814 credits consumed
#              Action: Set AUTO_SUSPEND to 15 minutes
```

### Default Policies Reference

#### Compute Policies

| Policy | What It Detects | Default Threshold | Action |
|--------|----------------|-------------------|--------|
| **idle-warehouse-15min** | Warehouses with very low utilization but significant credit burn | `avg_running < 0.05`, credits ≥ 10 | Shutdown |
| **auto-suspend-not-set** | Warehouses without AUTO_SUSPEND configured | suspend = 0 or NULL | Alert |
| **concurrency-scaling-runaway** | Concurrency scaling credits exceeding daily budget | > 50 credits/day | Alert |

#### Sizing Policies

| Policy | What It Detects | Default Threshold | Action |
|--------|----------------|-------------------|--------|
| **oversized-warehouse-approval** | Warehouses sized 4XL or larger | size ≥ 4X-Large | Requires Director approval |
| **multi-cluster-max-exceeded** | Multi-cluster warehouses with max > 3 | max_clusters > 3 | Requires Manager approval |

#### AI / Cortex Policies

| Policy | What It Detects | Default Threshold | Action |
|--------|----------------|-------------------|--------|
| **ai-daily-credit-cap** | AI service credits exceeding daily budget | > 100 credits/day | Budget cap alert |
| **cortex-agent-loop-limit** | Agent tasks stuck in loops | > 50 iterations | Alert |

#### Governance Policies

| Policy | What It Detects | Default Threshold | Action |
|--------|----------------|-------------------|--------|
| **required-tags-missing** | Warehouses without the 5 required tags | tag_count < 5 | Blocker |
| **prod-environment-tag** | Production warehouses missing environment tag | environment = NULL | Tag required |

#### Storage Policies

| Policy | What It Detects | Default Threshold | Action |
|--------|----------------|-------------------|--------|
| **time-travel-retention-nonprod** | Non-prod with time travel > 1 day | retention > 1 day | Downsize |
| **stale-table-90-days** | Tables not accessed in 90+ days | days > 90 | Alert |

---

## 5. Customizing Policies

### Editing via YAML

Open `<FLASHLIGHT_HOME>/config/policies.yml` and adjust thresholds:

```yaml
policies:
  - id: idle-warehouse-15min
    name: "Shutdown idle warehouses after 15 minutes"
    enabled: true
    category: compute
    condition_sql: "avg_running < {max_idle_ratio} AND credits >= {min_credits}"
    threshold:
      max_idle_ratio: 0.10      # Changed from 0.05 → less aggressive
      idle_minutes: 30           # Changed from 15 → longer grace period
      min_credits: 25            # Changed from 10 → only flag big spenders
    action: shutdown
    severity: warning

  - id: oversized-warehouse-approval
    name: "4XL+ warehouse requires Director approval"
    enabled: true
    category: sizing
    condition_sql: "warehouse_size IN ('4X-Large', '5X-Large', '6X-Large')"
    threshold:
      max_self_serve_size: "3X-Large"
    action: require_approval
    severity: critical
    approval_role: "Director"    # Who needs to approve
```

### Editing via Dashboard

1. Navigate to **http://localhost:8501/policies**
2. Find the policy you want to change
3. Toggle the **switch** to enable/disable
4. Click **"Edit thresholds"** to expand
5. Change values and click **Save**

Changes are written back to `policies.yml` immediately.

### Creating a Custom Policy

**Via Dashboard:**
1. Go to `/policies` → **"Add Policy"** tab
2. Fill in:
   - Name, description
   - Category (compute, sizing, governance, ai, storage)
   - Action (alert, shutdown, require_approval, downsize, tag_required, budget_cap)
   - Severity (info, warning, critical, blocker)
   - Condition SQL (the detection logic)
   - Thresholds (JSON: `{"my_param": 100}`)
3. Click **Create Policy**

**Via YAML:**
```yaml
# Add to policies.yml
  - id: dev-warehouse-weekend-shutdown
    name: "Dev warehouses must be suspended on weekends"
    description: "Development warehouses running on weekends waste credits"
    enabled: true
    category: compute
    condition_sql: "dayofweek(start_time) IN (0, 6) AND credits > {min_credits}"
    threshold:
      min_credits: 5
    action: shutdown
    severity: warning
    created_by: "ops-team"
    tags: ["dev", "scheduling"]
```

---

## 6. Tag Governance

### Why Tags Matter

Without proper tags, the dashboard shows "unattributed" spend — you can't answer:
- "What does Team X cost us?"
- "How much is production vs. dev?"
- "Which application is the cost driver?"

### Required Tag Taxonomy

| Tag | Purpose | Example Values |
|-----|---------|----------------|
| `department` | Chargeback: who pays? | Engineering, Data, Finance |
| `environment` | Separate prod from dev | prod, staging, dev, sandbox |
| `application` | Cost per workload | Analytics, ML Pipeline, BI |
| `owner` | Who to ask about cost | data-eng-team, jane.doe |
| `cost_center` | Finance GL mapping | CC-1234, OPEX-DATA |

### Applying Tags in Snowflake

```sql
-- Create tag objects (one-time setup)
CREATE TAG IF NOT EXISTS governance.tags.department;
CREATE TAG IF NOT EXISTS governance.tags.environment
  ALLOWED_VALUES 'prod', 'staging', 'dev', 'sandbox';
CREATE TAG IF NOT EXISTS governance.tags.application;
CREATE TAG IF NOT EXISTS governance.tags.owner;
CREATE TAG IF NOT EXISTS governance.tags.cost_center;

-- Tag a warehouse
ALTER WAREHOUSE analytics_wh SET TAG
  governance.tags.department = 'Data',
  governance.tags.environment = 'prod',
  governance.tags.application = 'Analytics Platform',
  governance.tags.owner = 'data-eng-team',
  governance.tags.cost_center = 'CC-5678';
```

### Monitoring Tag Coverage

The dashboard Governance tab shows:
- **Coverage score** — % of warehouses with all 5 required tags
- **Unattributed spend** — dollars flowing through untagged warehouses
- **Tag quality issues** — empty values, placeholders ("TBD"), case mismatches
- **Cost by department** — spend breakdown by department tag

The `required-tags-missing` policy (severity: BLOCKER) flags warehouses without full tag coverage.

### Enforcement

Snowflake doesn't have Azure-style "deny without tags" policies. Instead, use:

1. **Detection + Alert** (Flashlight does this):
   - Policy engine flags untagged warehouses
   - Dashboard shows unattributed spend prominently

2. **Scheduled Task** (optional, in Snowflake):
   ```sql
   CREATE TASK governance.tasks.check_untagged_warehouses
     WAREHOUSE = governance_wh
     SCHEDULE = 'USING CRON 0 8 * * MON America/New_York'
   AS
   BEGIN
     -- Alert logic here (email or notification integration)
   END;
   ```

3. **IaC Prevention** (recommended):
   - Enforce tags in Terraform/Pulumi at provisioning time
   - Reject PRs that create warehouses without tag assignments

### Tag Governance Checks (G01–G10)

### Editing Tag Governance Policies

Two governance policies in `policies.yml` control tag enforcement. Ops teams edit
these thresholds to match their organization's requirements:

**Policy: `required-tags-missing`** (which tags are mandatory)

```yaml
# In <home>/config/policies.yml
- id: required-tags-missing
  name: "All warehouses must have required tags"
  enabled: true
  category: governance
  threshold:
    required_tags: 5           # ← Change: set to 3 if you only require dept/env/owner
  action: tag_required
  severity: blocker            # ← Change: set to "warning" for softer enforcement
```

**Policy: `prod-environment-tag`** (production warehouses must be labeled)

```yaml
- id: prod-environment-tag
  name: "Production warehouses must be tagged environment=prod"
  enabled: true
  category: governance
  threshold: {}                # No configurable threshold — pattern-based
  action: tag_required
  severity: warning
```

**To edit via dashboard:**
1. Navigate to **http://localhost:8501/policies**
2. Scroll to the **governance** category
3. Expand `required-tags-missing`
4. Change `required_tags` from `5` to your desired count
5. Click **Save**

**To add a custom governance policy** (e.g., "all databases must have an owner tag"):

```yaml
# Add to policies.yml
- id: database-owner-required
  name: "All databases must have an owner tag"
  description: "Databases without an owner tag cannot be attributed for chargeback"
  enabled: true
  category: governance
  condition_sql: "domain = 'DATABASE' AND tag_count < 1"
  threshold:
    required_tags: 1
  action: tag_required
  severity: warning
  created_by: "ops-team"
```

Or create it via the dashboard: `/policies` → **"Add Policy"** tab → fill the form → **Create**.

The Governance tab on the Visibility page evaluates these checks:

| Code | Check | What It Measures | Severity |
|------|-------|-----------------|----------|
| G01 | Unattributed spend | Warehouses missing `owner` or `cost_center` | WARN/BLOCKER |
| G02 | Tag coverage score | % of warehouses with all 5 required tags | KPI |
| G03 | Coverage by object type | Warehouses vs databases vs schemas coverage | INFO |
| G04 | Tag quality issues | Empty values, placeholders (`TBD`), case mismatches | WARN |
| G05 | Untagged spend dollars | Total $ flowing through untagged warehouses | BLOCKER |
| G06 | Compliance trend | Coverage % improving or degrading month-over-month | TREND |
| G07 | Cost by department | Spend attribution breakdown by `department` tag | ATTRIBUTION |
| G08 | Cost by environment | Spend split: prod vs dev vs staging | ATTRIBUTION |
| G09 | Cost by application | Spend per workload/application tag | ATTRIBUTION |
| G10 | Orphan tags | Tags on dropped/renamed objects | INFO |

**Reading the coverage score:**
- **80%+** = Good governance. Most spend is attributable.
- **50–80%** = Gaps exist. Some teams aren't tagging. Focus on high-spend warehouses.
- **< 50%** = Critical. Most spend cannot be charged back. Prioritize tagging.

**Tag quality issues to watch for:**
- `""` (empty) — tag exists but value is blank
- `TBD` / `TODO` / `unknown` — placeholder never replaced
- `Prod` vs `prod` — inconsistent casing breaks filters

---

## 7. Hidden Waste — Hidden Spend

Hidden Waste is credit consumption that delivers little or no business value. The
`/snowflake-demo/hidden-waste` page surfaces three categories:

### Compute Hidden Waste

| Pattern | How It Wastes | What to Do |
|---------|--------------|------------|
| Idle running | Warehouse active (not suspended) but zero queries | Lower AUTO_SUSPEND or remove |
| Oversized | 4XL serving workload that fits in Medium | Downsize (test first) |

### Storage Hidden Waste

| Pattern | How It Wastes | What to Do |
|---------|--------------|------------|
| Stale tables | No read/write in 90+ days, still paying storage | Archive or DROP |
| Time Travel excess | 7-day retention on dev databases | Set to 1 day for non-prod |
| Abandoned clones | CLONE with no downstream usage | DROP the clone |

### AI / Cortex Hidden Waste (6 patterns)

| Pattern | How It Wastes | What to Do |
|---------|--------------|------------|
| Oversized models | GPT-4 / large for simple classification tasks | Use `snowflake-arctic` or smaller |
| Duplicate calls | Same input hitting Cortex repeatedly | Add caching / deduplication |
| Verbose prompts | Token-bloated completions | Trim prompts, use structured output |
| Idle Search | Cortex Search provisioned but no queries | Deprovision or consolidate |
| Agent loops | 50+ iterations, no result | Set iteration limits |
| Dev-in-prod | Development workloads using production-tier models | Route dev to cheaper tier |

### Hidden Waste KPIs

The Hidden Waste page shows:
- **Total Hidden Waste** — combined 30-day recoverable spend
- **Compute Waste** — idle + oversized warehouse credits
- **Storage Waste** — annualized stale/clone/TT excess
- **AI Waste** — model misuse + idle services

---

## 8. AI / Cortex Cost Management

### Understanding AI Spend

AI/Cortex costs appear in two places:
1. **Warehouse credits** — ML_TRAINING, CORTEX_AI, CORTEX_SEARCH, CORTEX_AGENTS warehouses
2. **Serverless credits** — metered separately (CORTEX_AI_FUNCTIONS, CORTEX_ANALYST, etc.)

The LeaderBoard shows total AI spend as a percentage of your overall Snowflake cost.

### AI Optimization Checks

| Check | Source View | What It Surfaces |
|-------|------------|-----------------|
| Service Metering | `metering_history` | Credit consumption by Cortex service |
| Function Usage | `cortex_ai_functions_usage_history` | Per-function, per-model call volume + cost |
| Search Daily | `cortex_search_daily_usage_history` | Cortex Search cost by consumption type |

### AI Governance Policies

Two policies control AI spend out of the box:

```yaml
# Cap daily AI credits (default: 100)
- id: ai-daily-credit-cap
  threshold:
    daily_cap: 100          # Adjust to your budget
  action: budget_cap
  severity: critical

# Limit agent iterations (default: 50)
- id: cortex-agent-loop-limit
  threshold:
    max_iterations: 50      # Prevents runaway loops
  action: alert
  severity: warning
```

### Recommended AI Cost Controls

1. **Set budget caps** — adjust `daily_cap` to match your AI budget
2. **Monitor function usage** — Function Usage shows which models/functions burn most
3. **Review Search provisioning** — Search tab flags provisioned services with low query volume
4. **Watch for agent loops** — unbounded iterations can burn hundreds of credits
5. **Right-size models** — use smaller models for simple tasks (classification, extraction)

---

## 9. Snowflake Visibility — 11 Optimization Levers (Detail)

The Visibility page (`/snowflake-demo/visibility`) provides deep-dive tabs:

### Warehouse / Compute Tab
- **Idle warehouses** — `avg_running < 0.15` with 100+ credits = BLOCKER
- **Queue pressure** — queries queueing indicates undersized warehouse
- **Cost efficiency** — credits per TB scanned, credits per 1K queries
- Date-range filtering for time-boxed analysis

### Query Performance Tab
- **Attributed cost** — top 50 query patterns by compute credits
- **Expensive patterns** — scan > 1TB, spill > 0, elapsed > 5min
- **Cache reuse** — repeated queries with < 40% cache hit (optimization target)

### Storage Tab
- **Storage trend** — table + stage + failsafe + hybrid + archive over time
- **Top tables** — 30 largest tables by total bytes

### AI & Cortex Tab
- AI service detail + model-level drill-down
- Daily AI spend trend

### Data Design Tab
- Auto-clustering candidates
- Materialized view ROI analysis

### Ingestion & Orchestration Tab
- Snowpipe cost per pipe (credits/GB/files)
- Serverless task credit burn
- COPY INTO patterns

### Data Movement Tab
- Cross-region/account byte transfers
- Replication credit usage

### Governance Tab
- Tag coverage G02–G10 checks (see Section 6)
- Cost attribution by department/environment/application

---

## 10. Dashboard Navigation

### Available Pages

| Page | URL | Purpose |
|------|-----|---------|
| Home | `/` | Cross-provider spend overview |
| TCO | `/tco` | Total Cost of Ownership (multi-cloud) |
| Efficiency & Waste | `/efficiency-waste` | Billed-but-not-used spend |
| **Cost Policies** | `/policies` | **Manage governance rules** |
| Snowflake Executive | `/snowflake-demo/executive` | 3-pillar cost summary |
| Snowflake Visibility | `/snowflake-demo/visibility` | 11 optimization levers |
| Hidden Waste | `/snowflake-demo/hidden-waste` | Hidden credit consumption |

### Cost Distribution Charts

The **LeaderBoard** page shows two charts that break your spend into six service
categories (Managed Compute, Serverless Compute, AI & ML, Storage, Data Transfer,
Other):

- **Cost Distribution by Service** — current month pie chart
- **Cost by Service — Last 12 Months** — stacked bar chart

Both charts use the same data sources and category definitions so they stay
consistent. For a full explanation of what each service type means, see
[Snowflake Service Types](snowflake-service-types.md).

---

## 11. Policy Actions Explained

| Action | What It Means | What You Do |
|--------|--------------|-------------|
| `alert` | Advisory notification | Review and decide |
| `shutdown` | Warehouse should be suspended | Set/lower AUTO_SUSPEND |
| `require_approval` | Needs sign-off before proceeding | Route to `approval_role` |
| `downsize` | Resource is over-provisioned | Reduce size or retention |
| `tag_required` | Missing governance tags | Apply tags before proceeding |
| `budget_cap` | Spending exceeds limit | Investigate workload, apply limits |

Flashlight policies are **advisory** — they detect and report. They do not automatically
terminate warehouses or block operations. The recommended actions tell your team what to do.

---

## 12. Severity Levels

| Severity | Meaning | Response Time |
|----------|---------|---------------|
| `blocker` | Violates a hard governance rule | Immediate (before next review) |
| `critical` | Significant cost risk | Same day |
| `warning` | Moderate concern | Within sprint |
| `info` | Informational, monitor trend | Next review cycle |

---

## 13. Operational Workflows

### Daily Check

```bash
flashlight policies evaluate
```

Run this in your morning routine or schedule it in cron/Airflow.

### Weekly Review

1. Open dashboard → `/policies` → "Current Violations" tab
2. Review new violations since last week
3. Assign owners to critical violations
4. Adjust thresholds if false positives are high

### Monthly Governance Report

```bash
# Export policy status for reporting
flashlight policies export

# Full data refresh
flashlight ingest
flashlight policies evaluate > /tmp/monthly-violations.txt
```

### Onboarding a New Warehouse

Before deploying a new warehouse:
1. Ensure it has all 5 required tags
2. Set `AUTO_SUSPEND` (recommended: 5–15 minutes)
3. Start with a reasonable size (scale up later)
4. If 4XL+, get Director approval first

---

## 14. Troubleshooting

| Issue | Cause | Fix |
|-------|-------|-----|
| `flashlight policies evaluate` shows no violations | Data path mismatch or no data | Run `flashlight ingest` first |
| "No policies file" error | Haven't initialized | Run `flashlight init` |
| Dashboard `/policies` page blank | No GOLD data | Run `flashlight transform` |
| Policy changes not reflected | YAML syntax error | Validate with `flashlight policies list` |
| Too many false positives | Thresholds too aggressive | Edit `policies.yml`, raise thresholds |

---

## 15. File Reference

| File | What It Is | Who Edits It |
|------|-----------|-------------|
| `<home>/config/connections.yml` | Snowflake credentials | Infrastructure team |
| `<home>/config/policies.yml` | Cost governance rules | Operations team |
| `<home>/gold/snowflake/*.parquet` | Materialized cost views | Auto-generated (read-only) |

Where `<home>` = `FLASHLIGHT_HOME` env var or the platform user-data directory.
