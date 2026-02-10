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

5. **Run your first analysis:**

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
      ├── Analyzers              — read-only analysis → Recommendations
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
