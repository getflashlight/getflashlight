# CLAUDE.md

Guidance for Claude Code when working in this repository.

## What is Auralake

Auralake is a **FOCUS-based, multi-cloud TCO spend-visualization** platform. It
ingests cloud billing in the FinOps FOCUS format, standardizes it into a layered
data model (BRONZE → SILVER → GOLD), and serves preconfigured Grafana dashboards
and an MCP server. It visualizes **current spend** — its headline is **Total Cost
of Ownership** (e.g. Databricks DBU cost + the underlying AWS infra). It does
**not** produce optimization recommendations in v1.

(Auralake was previously a Databricks cost-optimization tool; that code was
removed in the FOCUS pivot. Don't reintroduce analyzers/recommendations.)

## Monorepo

uv workspace (virtual root `pyproject.toml`), two packages:

```
packages/backend/  → auralake-backend (the platform)
packages/cli/      → auralake-cli (thin HTTP client)
```

## Commands

```bash
uv sync
uv run --project packages/backend auralake-server      # API :8000
uv run --project packages/backend auralake-mcp         # MCP :8002
uv run --project packages/backend auralake-ingest      # pull billing → BRONZE
uv run --project packages/backend auralake-transform   # (re)build SILVER/GOLD views
uv run --project packages/backend auralake-db-migrate  # alembic upgrade head
uv run ruff check packages/ && uv run mypy packages/backend && uv run pytest
docker compose up -d                                   # full stack
```

## Architecture (`packages/backend/src/auralake/`)

- **`focus/`** — the canonical internal FOCUS record (`model.py`) and controlled
  vocab (`enums.py`). Every connector maps its source into a `FocusRecord`; this
  is the one contract between ingestion and storage.
- **`ingest/`** — `Connector` ABC (`base.py`), YAML config (`config.py`), the
  `runner.py` orchestrator, and `connectors/` (aws_focus, focus_file, databricks,
  aws_infra, plus stubs for bigquery/snowflake/redshift). FOCUS-shaped sources share
  `connectors/_focus_map.py`. The **databricks** connector runs the vendored
  Databricks→FOCUS 1.3 query (`connectors/sql/databricks_focus_1_3.sql`, from
  `databricks-solutions/cloud-infra-costs`) on a warehouse and maps its output —
  don't reintroduce hand-rolled DBU math. Re-pull that file upstream to update it.
- **`store/`** — SQLModel BRONZE tables (`models.py`: `raw.focus_record`,
  `meta.ingest_run`), engine, idempotent `upsert.py`, and read-only `query.py`.
- **`transform/`** — `sql/` holds the SILVER/GOLD views (the metrics contract);
  `runner.py` applies them; `catalog.py` describes the GOLD views for consumers.
- **`server/`** — FastAPI: `/api/v1/metrics*` read API + `/api/v1/ingest` trigger.
- **`mcp/`** — FastMCP server exposing the same GOLD views to agents.

## Key invariants (do not violate)

- **GOLD is the only consumer surface.** Grafana and MCP read `gold.*`, never raw/
  silver — so charts and agents always agree.
- **One cost metric per aggregation.** Canonical is `EffectiveCost` (mapped to
  `cost` in SILVER). Never sum across FOCUS cost columns.
- **Charge-period grain only** when aggregating; never the billing period.
- **TCO double-count guard** (`silver.tco_resource_month`): classic compute = DBU +
  attributed AWS infra; serverless = DBU only. The `x_compute_class` stamped by the
  Databricks connector drives this.
- **Partition-replace ingest**: each run is authoritative for the (connector,
  charge-period window) it pulls — `delete_window` purges that range, then a plain
  insert loads the fresh pull, atomically in one savepoint. Re-running is idempotent
  and self-purging (bad/orphaned rows can't survive). `dedupe_key` (incl.
  `record_id`/`record_type`) is now only a within-batch uniqueness guard, not an
  upsert key. Databricks corrections (RETRACTION/RESTATEMENT) land as distinct rows
  and net via `SUM` downstream.
- **Single currency**: ingest asserts `billing_currency == AURALAKE_BASE_CURRENCY`.
- **Attribution honesty**: unattributed AWS spend is surfaced, never silently dropped.

## Code style

Python 3.11+, ruff (E,F,I,N,W,UP), line length 100, strict mypy with the pydantic
plugin. Pydantic v2 models, `StrEnum` for enums.
