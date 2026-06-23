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
  `runner.py` orchestrator, and `connectors/` (aws_focus, databricks, aws_infra,
  plus stubs for bigquery/snowflake/redshift).
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
- **Idempotent ingest**: upsert on `dedupe_key`; re-ingesting a restatement corrects.
- **Single currency**: ingest asserts `billing_currency == AURALAKE_BASE_CURRENCY`.
- **Attribution honesty**: unattributed AWS spend is surfaced, never silently dropped.

## Code style

Python 3.11+, ruff (E,F,I,N,W,UP), line length 100, strict mypy with the pydantic
plugin. Pydantic v2 models, `StrEnum` for enums.
