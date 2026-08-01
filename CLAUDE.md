# CLAUDE.md

Guidance for Claude Code when working in this repository.

## What is Flashlight

Flashlight is a **FOCUS-based, multi-cloud TCO spend-visualization** platform. It
ingests cloud billing in the FinOps FOCUS format, standardizes it into a layered
data model (BRONZE → SILVER → GOLD), and serves a bundled NiceGUI dashboard
and an MCP server. It visualizes **current spend** — its headline is **Total Cost
of Ownership** (e.g. Databricks DBU cost + the underlying AWS infra).

It ships as a single `pip install getflashlight` — **no Docker, no database server**.
Persistent state is Parquet under `FLASHLIGHT_HOME` (default: the platform user-data
dir), queried by a throwaway in-memory **DuckDB** in each process.

A second capability, **efficiency / waste** (the `metrics` plane → `efficiency`
GOLD group), surfaces **recoverable spend** — the billed-but-not-used gap (idle,
underutilized, wrong-compute-placement). This is *measurement of recoverable
dollars*, **not** automated remediation: still no analyzers that act, no
rightsizing recommendations engine. See `docs/design/efficiency-waste.md`.

(Flashlight was previously a Databricks cost-optimization tool; that code was removed
in the FOCUS pivot. The efficiency view measures waste — it does not reintroduce the
old recommendation/remediation analyzers.)

## Project layout

Single package, src layout — `pyproject.toml` at the repo root, code under
`src/flashlight/`, tests under `tests/`. No workspace, no nesting.

The single `flashlight` console script is the operator surface. `ingest` is the sole
**writer**; `mcp serve` and `dashboard serve` are independent **read-only**
processes. There is no REST API, no database, and no migrations (Parquet is
self-describing; the `FocusRecord` Pydantic model is the schema). The three
processes never contend: many readers over immutable Parquet, publish by atomic
per-file rename. See `src/flashlight/cli.py`.

## Commands

```bash
uv sync
uv run flashlight init             # scaffold the lake home + connections.yml
uv run flashlight sample           # download the FinOps FOCUS sample + seed it
uv run flashlight ingest           # pull billing → BRONZE, rebuild GOLD
uv run flashlight transform        # rebuild GOLD from BRONZE (no re-pull)
uv run flashlight mcp serve        # MCP server :8002 (agents)
uv run flashlight dashboard serve  # NiceGUI dashboard :8501 (humans)
uv run flashlight aws create-export  # create the AWS FOCUS export
uv run ruff check src tests && uv run mypy src tests && uv run pytest
```

## Architecture (`src/flashlight/`)

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
  `FLASHLIGHT_HOME`), `schema.py` (the BRONZE Arrow schema + row builder, replacing the
  old SQLModel table; `tags` is a JSON string), `bronze.py` (partition-replace
  writes), `duck.py` (in-memory DuckDB + `register_bronze`/`register_gold`),
  `publish.py` (atomic per-file GOLD swap), `runlog.py` (`meta/runs/` run log).
  Writes are zstd (`FLASHLIGHT_PARQUET_COMPRESSION*`).
- **`transform/`** — `sql/` holds the SILVER/GOLD views (the metrics contract, in
  DuckDB SQL); `runner.py` (`build_gold`) reads BRONZE Parquet, applies the views
  in-memory, then materializes GOLD **per provider** — slicing each provider-scoped
  view by `provider_name` into `gold/<group>/<view>.parquet` via `COPY`, and the TCO
  views into `gold/shared/` (the refresh — no matviews). `catalog.py` is the
  group-aware, data-driven description of the GOLD views for consumers.
- **`gold/`** — `reader.py`, the one read surface shared by MCP and the dashboard:
  cached in-memory DuckDB over `gold/<group>/*.parquet`, with the ad-hoc-SELECT guard
  rails.
- **`mcp/`** — FastMCP server (`flashlight mcp serve`) exposing the GOLD views to
  agents. **`dashboard/`** — the NiceGUI app (`flashlight dashboard serve`, booted
  in-process via `launch.py` — no subprocess), routed by `router.py` (one `@ui.page()`
  per fixed page plus one per provider group discovered via
  `discover_provider_groups()`), styled by `chrome.py` (dark-mode panels/KPIs/tables/
  Plotly chrome), pages in `views/`, reading GOLD via `data.py`/`gold_df()`. Redshift
  has no GOLD group of its own (its cost flows into `aws.*`; only its efficiency/waste
  telemetry is connector-specific) — `views/redshift_focus.py` is a service-name-scoped
  tab nested on the AWS page, not a separate nav entry. Both dashboard and MCP are
  read-only consumers of GOLD. The one exception: `views/connections.py` lets a user
  add/edit `connections.yml` sources and trigger a sync without the CLI — it still
  never writes GOLD/BRONZE itself, it shells out to `flashlight ingest` as a
  subprocess (`ingest_runner.py`), so `ingest` stays the sole writer either way.

## Key invariants (do not violate)

- **GOLD is the only consumer surface.** The dashboard and MCP read the
  published `gold/<group>/*.parquet`, never raw/silver — so charts and agents always
  agree.
- **GOLD is split per provider.** Each distinct `provider_name` present in the data
  gets its own group — a `gold/<group>/` dir registered as the DuckDB schema
  `<group>.<view>` (e.g. `aws.monthly_bill`, `databricks.monthly_bill`) — plus three
  fixed groups: `shared` for the cross-provider TCO views (`shared.tco_*`),
  `efficiency` for the waste views (`efficiency.waste_record`), and `driver_health`
  for the client-driver fleet-health view (`driver_health.driver_health` — no
  `cost_metric`, it's a compliance signal, not spend/waste). Provider groups are
  **data-driven** (discovered from `provider_name`, not a hard-coded list), and
  `build_gold` fans the in-memory `gold.<view>` SQL out per provider at COPY time;
  publish prunes a group whose provider dropped out of the data. Each provider gets
  its own dashboard page; TCO, efficiency, and driver health are their own pages. See
  `transform/catalog.py`.
- **The efficiency/waste plane is a second medallion**, parallel to FOCUS and
  separate because utilization telemetry doesn't fit `FocusRecord`. `EfficiencyRecord`
  (`efficiency/model.py`) is its one standardized contract — connectors emit it via
  `Connector.fetch_efficiency` (best-effort: a failed pull warns and skips, never
  blocks the cost ingest). It lands in the `metrics/` Parquet root
  (`lake.metrics.write_efficiency`, partition-replace by `provider_name`/`charge_month`),
  is registered as `metrics.efficiency_record`, and `050_gold_waste.sql` classifies it
  into `efficiency.waste_record` (waste_category + recoverable_cost). New compute
  classes / platforms add **rows** (a connector emitting `EfficiencyRecord`), not views.
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
- **Single currency**: ingest asserts `billing_currency == FLASHLIGHT_BASE_CURRENCY`.
- **Attribution honesty**: unattributed AWS spend is surfaced, never silently dropped.
- **Waste honesty** (`050_gold_waste.sql`): `underutilized` requires entity-level
  `utilization_pct` (jobs and all-purpose **clusters** — the cluster is the entity, so
  cluster CPU is honest); SQL warehouses have no per-entity utilization (NULL) so are
  never flagged underutilized. `placement` is **all-purpose only** (you can't move a SQL
  warehouse to jobs compute). `idle` fires only on a **measured** zero activity, never on
  NULL (= unmeasured). `placement`/`photon_no_gain`/`photon_on_interactive_cluster` are
  `candidate` confidence — Photon's cost premium is a DBU-consumption multiplier, not a
  different $/DBU price (the Photon/non-Photon SKU pair for a tier prices identically per
  DBU), so it's priced as a flat multiplier of `billed_cost`, not a measured saving. WASTE
  and OPPORTUNITY are separate lenses (a cluster can be both — different remedies); never
  sum them into one headline. Waste `billed_cost` reconciles to the FOCUS bill. The
  Databricks efficiency query (`connectors/sql/databricks_efficiency.sql`) is validated
  against a live warehouse; entity grain is job_id / all-purpose cluster_id / warehouse_id
  × month.
- **Known Databricks coverage gaps** (investigated, not silently missing — see "Known
  limitations" in `docs/design/efficiency-waste.md`): classic JOBS/ALL_PURPOSE compute has
  no *direct* spill/shuffle signal (no system table exposes Spark stage metrics —
  confirmed via `SHOW TABLES IN system.compute`/`system.lakeflow`) — partially mitigated
  by `possible_memory_pressure`/`possible_heavy_shuffle` in `waste_rules.py`, proxy
  signals from `system.compute.node_timeline` (CPU wait, memory swap, local disk
  headroom, network I/O), always `candidate` confidence and unpriced, gated by job run
  duration so short jobs aren't flagged. DLT/Lakeflow serverless pipeline compute
  (`usage_metadata.dlt_pipeline_id`) is entirely unattributed in the efficiency/waste
  plane (none of `databricks_efficiency.sql`'s branches key on it) though its cost is
  still captured correctly by the FOCUS pull.

## Code style

Python 3.12+, ruff (E,F,I,N,W,UP), line length 100, strict mypy with the pydantic
plugin. Pydantic v2 models, `StrEnum` for enums.
