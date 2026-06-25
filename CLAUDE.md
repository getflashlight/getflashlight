# CLAUDE.md

Guidance for Claude Code when working in this repository.

## What is Auralake

Auralake is a **FOCUS-based, multi-cloud TCO spend-visualization** platform. It
ingests cloud billing in the FinOps FOCUS format, standardizes it into a layered
data model (BRONZE → SILVER → GOLD), and serves a bundled Streamlit dashboard
and an MCP server. It visualizes **current spend** — its headline is **Total Cost
of Ownership** (e.g. Databricks DBU cost + the underlying AWS infra). It does
**not** produce optimization recommendations in v1.

It ships as a single `pip install auralake` — **no Docker, no database server**.
Persistent state is Parquet under `AURALAKE_HOME` (default: the platform user-data
dir), queried by a throwaway in-memory **DuckDB** in each process.

(Auralake was previously a Databricks cost-optimization tool; that code was
removed in the FOCUS pivot. Don't reintroduce analyzers/recommendations.)

## Project layout

Single package, src layout — `pyproject.toml` at the repo root, code under
`src/auralake/`, tests under `tests/`. No workspace, no nesting.

The single `auralake` console script is the operator surface. `ingest` is the sole
**writer**; `mcp serve` and `dashboard serve` are independent **read-only**
processes. There is no REST API, no database, and no migrations (Parquet is
self-describing; the `FocusRecord` Pydantic model is the schema). The three
processes never contend: many readers over immutable Parquet, publish by atomic
per-file rename. See `src/auralake/cli.py`.

## Commands

```bash
uv sync
uv run auralake init             # scaffold the lake home + connections.yml
uv run auralake sample           # download the FinOps FOCUS sample + seed it
uv run auralake ingest           # pull billing → BRONZE, rebuild GOLD
uv run auralake transform        # rebuild GOLD from BRONZE (no re-pull)
uv run auralake mcp serve        # MCP server :8002 (agents)
uv run auralake dashboard serve  # Streamlit dashboard :8501 (humans)
uv run auralake aws create-export  # create the AWS FOCUS export
uv run ruff check src tests && uv run mypy src tests && uv run pytest
```

## Architecture (`src/auralake/`)

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
- **`lake/`** — the Parquet persistence layer. `paths.py` (on-disk layout under
  `AURALAKE_HOME`), `schema.py` (the BRONZE Arrow schema + row builder, replacing the
  old SQLModel table; `tags` is a JSON string), `bronze.py` (partition-replace
  writes), `duck.py` (in-memory DuckDB + `register_bronze`/`register_gold`),
  `publish.py` (atomic per-file GOLD swap), `runlog.py` (`meta/runs/` run log).
  Writes are zstd (`AURALAKE_PARQUET_COMPRESSION*`).
- **`transform/`** — `sql/` holds the SILVER/GOLD views (the metrics contract, in
  DuckDB SQL); `runner.py` (`build_gold`) reads BRONZE Parquet, applies the views
  in-memory, then materializes GOLD **per provider** — slicing each provider-scoped
  view by `provider_name` into `gold/<group>/<view>.parquet` via `COPY`, and the TCO
  views into `gold/shared/` (the refresh — no matviews). `catalog.py` is the
  group-aware, data-driven description of the GOLD views for consumers.
- **`gold/`** — `reader.py`, the one read surface shared by MCP and the dashboard:
  cached in-memory DuckDB over `gold/<group>/*.parquet`, with the ad-hoc-SELECT guard
  rails.
- **`mcp/`** — FastMCP server (`auralake mcp serve`) exposing the GOLD views to
  agents. **`dashboard/`** — the Streamlit app (`auralake dashboard serve`), pages in
  `views/`, reading GOLD via `gold/reader.py`. Both are read-only consumers of GOLD.

## Key invariants (do not violate)

- **GOLD is the only consumer surface.** The Streamlit dashboard and MCP read the
  published `gold/<group>/*.parquet`, never raw/silver — so charts and agents always
  agree.
- **GOLD is split per provider.** Each distinct `provider_name` present in the data
  gets its own group — a `gold/<group>/` dir registered as the DuckDB schema
  `<group>.<view>` (e.g. `aws.monthly_bill`, `databricks.monthly_bill`) — plus a
  fixed `shared` group for the cross-provider TCO views (`shared.tco_*`). Groups are
  **data-driven** (discovered from `provider_name`, not a hard-coded list), and
  `build_gold` fans the in-memory `gold.<view>` SQL out per provider at COPY time;
  publish prunes a group whose provider dropped out of the data. Each provider gets
  its own dashboard page; TCO is its own page. See `transform/catalog.py`.
- **One cost metric per aggregation.** Canonical is `EffectiveCost` (mapped to
  `cost` in SILVER). Never sum across FOCUS cost columns.
- **Charge-period grain only** when aggregating; never the billing period.
- **TCO double-count guard** (`silver.tco_resource_month`): classic compute = DBU +
  attributed AWS infra; serverless = DBU only. The `x_compute_class` stamped by the
  Databricks connector drives this.
- **Partition-replace ingest**: each run is authoritative for the (connector,
  charge-period window) it pulls — `lake.bronze.write_window` removes that
  connector's `x_source_connector=…/charge_month=…/` partition dirs across the
  window, then writes the fresh pull. Re-running is idempotent and self-purging (a
  month the source no longer reports loses its partition; bad/orphaned rows can't
  survive). `dedupe_key` (incl. `record_id`/`record_type`) is only a within-batch
  uniqueness guard. Databricks corrections (RETRACTION/RESTATEMENT) land as distinct
  rows and net via `SUM` downstream.
- **Single currency**: ingest asserts `billing_currency == AURALAKE_BASE_CURRENCY`.
- **Attribution honesty**: unattributed AWS spend is surfaced, never silently dropped.

## Code style

Python 3.11+, ruff (E,F,I,N,W,UP), line length 100, strict mypy with the pydantic
plugin. Pydantic v2 models, `StrEnum` for enums.
