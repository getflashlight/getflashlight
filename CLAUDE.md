# CLAUDE.md

Guidance for Claude Code when working in this repository.

## What is Flashlight

Flashlight is a **FOCUS-based, multi-cloud spend-visualization** platform. It
ingests cloud billing in the FinOps FOCUS format, standardizes it into a layered
data model (BRONZE → SILVER → GOLD), and serves a bundled NiceGUI dashboard
and an MCP server. It visualizes **current spend** — every cloud and data platform
normalized onto one FOCUS bill, sliced per provider.

(A cross-provider **TCO** capability — Databricks DBU cost joined to the AWS infra
backing it — used to be the headline. It was removed: the `silver.tco_*` views, the
`gold/shared/` group, and the TCO dashboard page are gone. Don't reintroduce them.)

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
uv run ruff check src tests scripts && uv run mypy src tests scripts && uv run pytest
```

## Architecture (`src/flashlight/`)

- **`focus/`** — the canonical internal FOCUS record (`model.py`) and controlled
  vocab (`enums.py`). Every connector maps its source into a `FocusRecord`; this
  is the one contract between ingestion and storage.
- **`ingest/`** — `Connector` ABC (`base.py`), YAML config (`config.py`), the
  `runner.py` orchestrator, and `connectors/` (aws_focus, databricks, redshift, plus
  stubs for bigquery/snowflake). Each connector config carries an optional `name`
  (falls back to `type`; enforced unique via `effective_connector_name`) — needed
  once there's more than one connection of a type (e.g. several Redshift clusters),
  since `Connector.name` is set from it and is what BRONZE partitioning, the runlog,
  and the dashboard use to tell connections apart. **`aws_focus`** is the one AWS
  cost source — `AwsFocusConfig.cost_source` picks `"focus_export"` (the S3 FOCUS
  Data Export, default) or `"cost_explorer"` (a coarser Cost Explorer fallback, no
  export needed but needs `ce:GetCostAndUsage`) explicitly; there's no automatic
  detection between them. `include_services` defaults to Redshift's own FOCUS service names
  **+ Amazon S3 + Amazon EC2** (`DEFAULT_INCLUDE_SERVICES`) — S3 and EC2 are in the default
  because the storage and cloud-VM cost behind Unity Catalog / a classic Databricks
  cluster are billed by AWS and Databricks' DBU-only bill can't show either (see
  `docs/design/backing-storage.md`, `docs/design/backing-compute.md`); it's a pushed-down
  `ServiceName IN (...)` predicate, and
  `[]` means the whole account. FOCUS-shaped sources share `connectors/_focus_map.py`.
  The **databricks** connector runs the vendored
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
  view by `provider_name` into `gold/<group>/<view>.parquet` via `COPY`, and the
  cross-provider views into their fixed groups (the refresh — no matviews).
  `catalog.py` is the group-aware, data-driven description of the GOLD views for
  consumers.
- **`gold/`** — `reader.py`, the one read surface shared by MCP and the dashboard:
  cached in-memory DuckDB over `gold/<group>/*.parquet`, with the ad-hoc-SELECT guard
  rails.
- **`mcp/`** — FastMCP server (`flashlight mcp serve`) exposing the GOLD views to
  agents. **`dashboard/`** — the NiceGUI app (`flashlight dashboard serve`, booted
  in-process via `launch.py` — no subprocess), routed by `router.py` (one `@ui.page()`
  per fixed page plus one per provider group discovered via
  `discover_provider_groups()`), styled by `chrome.py` (dark-mode panels/KPIs/tables/
  Plotly chrome), pages in `views/`, reading GOLD via `data.py`/`gold_df()`. Every
  provider page carries the **same five core tabs** — Trend & changes, Breakdown,
  Attribution (`views/attribution.py`), Efficiency & Waste (`views/efficiency_waste.py`) and
  Policy Compliance (`views/policy.py`) — plus per-provider extras (Databricks: AI Costs
  (`views/ai_costs.py`), Backing storage (`views/backing_storage.py`), Backing compute
  (`views/backing_compute.py`) and Client Driver Health, its only producer). Home is
  the only cross-provider page; the old `/utilization` and `/leaderboard` pages are gone
  (`router._RETIRED_ROUTES` 307s them to Home). Redshift
  has no GOLD group of its own (its cost flows into `aws.*`; only its efficiency/waste
  telemetry is connector-specific) — `views/redshift_focus.py` **is** the whole `/aws`
  page (not a nested tab), and it is a *configuration of* `provider_focus.render`: a
  `provider_focus.Scope` narrowing the `aws` group to `REDSHIFT_SERVICE_NAMES`, plus four
  hooks (`scope_caption`, `breakdown_lead`, `attribution_tab`, `efficiency_tab`) for the
  genuinely Redshift-shaped panels. It used to be a 700-line fork, which is why the
  identically-labelled tabs held different panels there than everywhere else. The tab
  *labels* never vary — a hook changes what's inside a tab, never whether it exists.
  Both dashboard and MCP are
  read-only consumers of GOLD. The exceptions are the two **control surfaces**, which
  drive a CLI subprocess rather than doing the work in-process, so the CLI stays the one
  implementation: `views/connections.py` edits `connections.yml` and shells out to
  `flashlight ingest` (`ingest_runner.py`), so `ingest` stays the sole writer either way;
  `views/mcp_server.py` starts/stops `flashlight mcp serve` (`mcp_runner.py`) and shows its
  status, endpoint, live output and tool inventory (read from `mcp.list_tools()`, so it
  can't drift). `mcp serve` can't run in-process — `MCPServer.run()` blocks its thread and
  owns an event loop — and the subprocess handle lives in a **module-level** global because
  NiceGUI page functions re-run per client. Status also probes the port
  (`launch.port_in_use`), so a server started in a terminal reads as up; the dashboard is a
  control surface for the server, not its owner.
- **All user config is `<home>/config/*.yml`** — `connections.yml` (`ingest/config.py`),
  `policies.yml` (`efficiency/policy_config.py`), `assistant.yml` (BYOK provider/model/
  base_url, `dashboard/assistant_config.py`). One directory to back up or mount. Each is
  `@lru_cache`-loaded with env vars overriding the file (`FLASHLIGHT_ASSISTANT_*`), each is
  scaffolded with a commented template generated from its own model's field descriptions
  (`scaffold.py`), and **none of them ever holds a secret** — those go to the OS keychain
  with an env-var fallback (`assistant_credentials.py`, `ingest/connection_credentials.py`).
  Don't put durable settings in NiceGUI's `app.storage.general`: it lands under
  `NICEGUI_STORAGE_PATH`, which a read-only deployment points at a tmpfs, and it's
  invisible to the CLI and MCP. That's why the assistant's config moved out of it.

## Key invariants (do not violate)

- **GOLD is the only consumer surface.** The dashboard and MCP read the
  published `gold/<group>/*.parquet`, never raw/silver — so charts and agents always
  agree.
- **GOLD is split per provider.** Each distinct `provider_name` present in the data
  gets its own group — a `gold/<group>/` dir registered as the DuckDB schema
  `<group>.<view>` (e.g. `aws.monthly_bill`, `databricks.monthly_bill`) — plus the
  fixed cross-provider groups: `efficiency` for the waste views
  (`efficiency.waste_record`), `driver_health` for the client-driver fleet-health view
  (`driver_health.driver_health`), `policy` for the governance findings
  (`policy.policy_record`) — those two with no `cost_metric`, they're compliance
  signals, not spend/waste — `ai_usage` for the AI token views
  (`ai_usage.project_month`, `.requester_month`, `.model_month`, `.endpoint_month`), and
  `storage` for the backing-storage views (`storage.backing_storage_month`,
  `storage.storage_location`) and `compute` for the backing-compute views
  (`compute.backing_compute_month`, `compute.compute_instance`) — those last two
  cross-provider **by construction**, since every row carries two provider columns:
  `billing_provider_name` (who invoices it, AWS) and `platform_provider_name` (whose
  metadata claims the bucket/instance, Databricks).
  **Every fixed group must be in `catalog.FIXED_GROUPS`** — one missing from that set
  becomes a *phantom provider*: `discover_provider_groups()` hands it to the nav and
  `router._provider_page` renders `provider_focus` against a `<group>.monthly_bill` that
  doesn't exist. Provider groups are **data-driven** (discovered from `provider_name`, not a hard-coded list), and
  `build_gold` fans the in-memory `gold.<view>` SQL out per provider at COPY time;
  publish prunes a group whose provider dropped out of the data. Each provider gets
  its own dashboard page, and efficiency/waste is a **core tab on every one of them** —
  including providers with no telemetry, which get a named empty state rather than a
  hidden tab ("never measured" must not look like "nothing to find"). Only AI Costs,
  Backing storage, Backing compute and driver health are Databricks-only extras — the
  first and last because Databricks is their sole *producer*, Backing storage/Backing
  compute because each has two producers (`aws_focus` for the AWS cost, `databricks` for
  the Databricks-side map — Unity Catalog's bucket map, or `system.compute.node_timeline`'s
  instance/cluster map) but a single *subject*, which serves the same purpose: keep a
  page-specific tab off every other provider page (see `router.py`). Policy compliance
  is a core tab. See `transform/catalog.py`.
- **The efficiency views are one plane at three stages, shown per provider.**
  `metrics.efficiency_record` → `efficiency.utilization_entity_month` (measurement: how
  hard was it working?) → `efficiency.waste_record` (verdict: what's recoverable?) →
  `efficiency.waste_by_owner_month` (attribution rollup: whose?). They were three
  cross-provider surfaces; they are now the Efficiency & Waste and Attribution tabs on
  each provider page. `waste_record` only holds entities a rule *fired* on, so the tab
  leads with `efficiency_waste.coverage_caption()` — the measured / not-applicable /
  no-telemetry split — because on real data only ~10% of Databricks entity-months carry a
  utilization reading and 0% of AWS's do. Absence of a finding is mostly absence of
  measurement; never let the UI imply otherwise. The rest of the measurement stage
  (per-signal readings, `cost_per_native_unit`) is deliberately GOLD/MCP-only, not a
  dashboard panel: `primary_signal_value` mixes percentages with day counts and
  `cost_per_native_unit` mixes $/DBU with $/MB, so any table showing them must label each
  row's own unit and must never make them a sortable numeric column.
- **AI cost is only allocatable per token where tokens are the meter.** The AI plane
  (`ai_usage/` Parquet root → `metrics.ai_usage` → the `ai_usage` GOLD group, fed by
  `Connector.fetch_ai_usage` over `system.serving.*`) reports **token volume per project and
  per user** beside the endpoint's FOCUS cost. Every row carries a `cost_allocation_basis`,
  and `allocated_cost`/`cost_per_million_tokens` are **NULL for three of its four values** —
  pay-per-token serving is metered per token so a token-share split is honest, but
  provisioned throughput/compute bills per provisioned *hour* (an idle endpoint bills real
  money with zero tokens, so a token split would move its idle cost onto whoever sent
  traffic) and an external model's tokens are billed by the vendor on a bill this lake never
  sees. **NULL there means "not allocatable by token", never "$0" — never coalesce it**, and
  never sum `allocated_cost` with its named complement `unallocated_cost`. The cost↔token
  join is Databricks-internal with `provider_name` *in the join key*, so it is not the
  forbidden cross-provider join. The endpoint↔cost join is `FULL OUTER`: an endpoint with
  cost but no token rows must stay visible (`token_coverage_status`), because unmeasured must
  never look like efficient. `system.serving` is an unverified Public Preview — the pull
  probes and degrades in three rungs (`full`/`usage_only`/`none`) and never blocks the cost
  ingest. AI *cost* needs none of this: `gold.ai_spend_month` reads the bill, and it scopes
  by `service_category = 'AI and Machine Learning'` **plus** an explicit product list for AI
  products the vendored query files elsewhere (AI/BI Genie bills as warehouse-shaped usage).
  Its window total is also a KPI card on the provider page (`ai_costs.kpi_card` via
  `extra_kpis`) — a **slice of** `net`, not an addition to it (same bill), so it says "part
  of Databricks net" and keeps the default hue, the inverse of the backing-storage card
  beside it. Omitted rather than shown as $0 when the window has no AI rows.
  Endpoint *verdicts* go through the existing waste plane as `entity_type='endpoint'` rows —
  `idle` and `failed` are entity-type-agnostic and fire with no new rule. See
  `docs/design/ai-costs.md`.
- **The efficiency/waste plane is a second medallion**, parallel to FOCUS and
  separate because utilization telemetry doesn't fit `FocusRecord`. `EfficiencyRecord`
  (`efficiency/model.py`) is its one standardized contract — connectors emit it via
  `Connector.fetch_efficiency` (best-effort: a failed pull warns and skips, never
  blocks the cost ingest). It lands in the `metrics/` Parquet root
  (`lake.metrics.write_efficiency`, partition-replace by `provider_name`/`charge_month`),
  is registered as `metrics.efficiency_record`, and `050_gold_waste.sql` classifies it
  into `efficiency.waste_record` (waste_category + recoverable_cost). New compute
  classes / platforms add **rows** (a connector emitting `EfficiencyRecord`), not views.
  Its siblings follow the identical pattern with their own model + Parquet root + optional
  best-effort `Connector` hook: `DriverHealthRecord` (`driver_health/`), `AiUsageRecord`
  (`ai_usage/`), `StorageLocationRecord` (`storage_locations/` →
  `metrics.storage_location`, via `Connector.fetch_storage_locations`), and
  `ComputeInstanceRecord` (`compute_instances/` → `metrics.compute_instance`, via
  `Connector.fetch_compute_instances`). Each root is a
  **sibling** of `metrics_dir()`, never nested inside it — `duck.register_metrics` globs
  `metrics_dir()/**/*.parquet` with `union_by_name`, so a differently-shaped dataset in
  that tree would silently corrupt the view. The storage plane is partitioned by
  `snapshot_month`, **not** `charge_month`: Unity Catalog exposes only current state, so
  it's a point-in-time inventory, not a charge period (and so not in `PERIOD_DIMENSIONS`).
  The compute-instance plane, unlike storage, IS partitioned by `charge_month` and
  partition-replaced by window like `DriverHealthRecord` — `system.compute.node_timeline`
  reports bounded historical activity, not present-tense state (see
  `docs/design/backing-compute.md`).
- **`spend_trend_daily` is one row per (day, service), NOT per day.** It carries
  `service_name` so a service-scoped page (`/aws`) can have a daily series at all. Any
  provider-wide daily consumer must `sum(net_cost) ... GROUP BY charge_day` — forgetting
  it makes a chart draw several points per x, which reads as real volatility, not a bug.
  Pinned by `tests/test_lake_roundtrip.py`.
- **A narrowed page's views are scopable, account-wide, or unavailable — three states,
  never two** (`provider_focus.Scope`). `account_wide` is checked **before** "does the
  view carry the dimension?", because `credits_month` *does* carry `service_name` and must
  still be read account-wide: AWS applies credits at account level, often untagged, so
  filtering would hide part of the discount and corrupt the Redshift page's account-level
  bucket, which nets credits against unused commitment. `unavailable` (a group total,
  percentage, per-SKU variance or forecast) means the panel must **state why it's absent**,
  never silently widen — `_drilldown`'s `sku_month_over_month` aggregate and
  `provider_spend_summary` both read whole-provider views and would otherwise report the
  entire AWS bill under a Redshift heading. `unscoped(g).where(v, c) == f"WHERE {c}"` for
  every provider base view is pinned in `tests/test_page_scope.py` — that identity is what
  makes threading `Scope` through the shared panels a no-op for other providers.
- **The monthly bar chart and the 3-month forecast are one figure** (`_monthly_drill`,
  Trend & changes). They were two panels with two independent y-scales, which drew a $14K
  projection as a taller bar than a $40K actual month. Sharing an axis is only honest if the
  projection can't be mistaken for measurement, so the forecast trace is grey where the
  actuals carry the palette, **hatched** where they're solid, named `Forecast (projection)`
  in the legend, and carries **no `custom_data`** — which is also what makes it inert on
  click (`_on_click` reads `customdata` to identify the month, so its absence *is* the
  guard; don't "fix" that by giving the trace customdata). A month that already has an
  actual bar **never** also gets a forecast bar: `barmode` is `stack`, so the two would pile
  into one column that is part measured and part invented. That happens whenever the newest
  data lands on the 1st–2nd of a month (the view fits on complete days, so it projects from
  the previous month); that month's own projection is the KPI row's run-rate card. The
  three "no forecast, and here's why" states (unpublished / not scopable / <3 complete
  months of history) survive as captions under the same chart — `_forecast_series` returns them.
- **The KPI row is one card per fact.** `list` and `savings` had their own cards next to
  `Realized discount`; `net + savings = list` is arithmetic a reader doesn't need three
  tiles for, and they crowded out cards carrying what `net` can't (AI spend, backing
  storage, credits, the run-rate projection). One `Realized discount` card keeps the whole
  discount story, with the list total as its denominator (`off $232.8K list`) — a percentage
  with no base is the one thing that card must not become.
- **Both the home page and every provider page open on YTD** (`chrome.year_start`, one
  definition shared with the `YTD` quick range). A finance question has a fixed anchor; the
  old rolling 6-month window silently redrew every month, so the same page compared week to
  week wasn't the same window. It reads off the *data's* last month, not today, so a stale
  lake opens on its own last year rather than an empty January. The two surfaces must keep
  the same default — they're compared constantly.
- **A waste rule is only listed where its signal is measured** (`WasteRule.providers` /
  `entity_types`, applied via `_COVERAGE` at import so a new rule can't be forgotten;
  `coverage_groups(provider_name)`). Over-inclusion is the dangerous direction: a rule on
  a coverage table for a provider that never measured it renders as **"clean"** — "we
  checked, found nothing". The four `sql_warehouse_*` rules are the trap; they read
  Databricks-only fields (`cache_hit_pct`, `spill_query_count`,
  `warehouse_type='SERVERLESS'`) on an `entity_type` Redshift also emits, so entity_type
  alone doesn't scope them. Guarded by `tests/test_rule_coverage.py`.
- **One cost metric per aggregation.** Canonical is `EffectiveCost` (mapped to
  `cost` in SILVER). Never sum across FOCUS cost columns.
- **Charge-period grain only** when aggregating; never the billing period.
- **The home page is charges-only; provider pages are net.** `views/home_overview.py`
  reads `monthly_bill.gross_cost` (non-credit rows) for every KPI, chart and mover — a
  one-off credit lands in a single month and nets against it, so at that altitude net
  reads as a spend collapse (a real AWS Redshift goodwill credit showed Jul 2026 as a
  −$46K "drop"). Credits are never dropped: the note under the KPI row labels the page
  charges-only (a generic one-liner — no totals, month or provider names) and each credit
  line is itemized in `gold.credits_month`, surfaced on the
  provider's own page (Redshift → Breakdown → "Discounts & credits"). Provider pages
  keep `net_cost` — that's the answer to "what did I owe?". **The assistant follows the
  same split** (`assistant_engine._PLAN_INSTRUCTIONS`): a spend / breakdown / trend /
  mover question plans `gross_cost`, `net_cost` is only for what was owed or paid.
  Without that rule the planner defaulted to `net_cost` and answered "break down last
  month's spend" with $10K against the home page's $68K — the two surfaces disagreeing
  by the size of one credit.
- **Display label ≠ `provider_name`.** `data.provider_label` is what a human reads and is
  **derived, not a constant**: `_GROUP_LABEL_RESOLVERS`/`data._aws_label` reads "AWS
  Redshift" while every `service_name` in the `aws` group is one of Redshift's own, and
  plain "AWS" once it holds more (`include_services` now defaults to Redshift **+ S3 +
  EC2**, so a static string was wrong in one direction or the other). It fails toward the *narrower*
  label on any query problem — under-claiming beats implying the whole account is there.
  `data.provider_name_for_group` is the value to filter or join on. Never put a label in a
  SQL predicate or use it as a lookup key into another view's rows.
- **No cross-provider cost join.** Databricks DBU spend and the AWS infra backing it
  are never added together into one figure — the TCO join that did that is gone, and
  nothing replaced it. `x_compute_class` is still stamped at ingest but no GOLD view
  keys off it. `provider_name` is the top-level split; a "total across providers" is
  the consumer's arithmetic over per-provider views, never a GOLD column.
  The line, sharply: joining AWS **cost** to Databricks **metadata** is allowed and is
  exactly what `storage.backing_storage_month` and `compute.backing_compute_month` do
  (S3 rows labelled by Unity Catalog's bucket list; EC2 rows labelled by
  `system.compute.node_timeline`'s instance/cluster map). Joining AWS cost to Databricks
  *cost* is the removed TCO capability. Both views live in their own GOLD groups so
  nothing writes into `gold/databricks/` — `databricks.monthly_bill` and the Databricks
  KPIs are untouched *by construction*, not by discipline (pinned by
  `test_backing_storage_never_changes_databricks_spend` /
  `test_backing_compute_never_changes_databricks_spend`).
- **Backing storage counts MANAGED storage only, and is a floor not a total**
  (`065_gold_storage.sql`, `views/backing_storage.py`, see
  `docs/design/backing-storage.md`). `mapping='databricks'` requires
  `location_kind='metastore_root'` — storage Databricks provisioned and whose lifecycle it
  owns. **External locations are excluded on purpose**: that data pre-existed and is merely
  registered for access, so the bucket exists whether or not Databricks reads it and costing
  it would *double-claim* another team's data-lake spend. They're still recorded in
  `storage.storage_location` (the audit trail for "why isn't this bucket counted?"), just not
  costed — never widen `bucket_map` to include them. Consequently workspace DBFS roots and
  per-catalog storage roots are missing too, so the figure **under-reports**; fixable via
  account-level `AccountClient.storage.list()`, deliberately deferred. The tab used to *say*
  so on screen (`TWO_BILLS_CAPTION`/`FLOOR_CAPTION` plus a coverage and a gap line) — four
  paragraphs above the first number, removed on request. The rules did not change, only their
  on-screen prose: the two-bills rule, the floor and the gaps now live only in this file, in
  `065_gold_storage.sql`'s header and in `docs/design/backing-storage.md`. What survives in
  the UI is the cost figure itself (`≤ $…` for a prefix-scoped upper bound, `$… (shared)`
  when several catalogs share a bucket, bare `$…` when the catalog owns the whole bucket)
  plus a one-line caption under the table explaining `≤`, and the `S3 cost (AWS-billed)`
  column heading.
  The window total also renders as a **KPI card on the Databricks page**
  (`backing_storage.kpi_card`, wired through `provider_focus.render`'s `extra_kpis`): the
  two numbers now sit side by side in one row, which is exactly where someone will be
  tempted to add them — don't. `Databricks net` stays DBU-only, the card names its own
  biller and says "not in net", and it carries a distinct hue for that reason. It is
  **omitted, never rendered as $0**, when nothing is mapped. The `metastore_root` filter also gives managed storage **precedence** for free on
  a bucket that is both. Never infer ownership from bucket *naming patterns*.
  Beyond that: the AWS bill's S3 `ResourceId` is **bucket**-grained while a metastore root is
  normally `s3://bucket/<metastore-id>`, so a `prefix_scoped` mapping is an **upper bound**
  and must never be rendered as a bare number. Every S3 row is kept —
  `databricks` / `unmapped` / `no_resource_id` — so the figure has an honest denominator;
  summing all of them reproduces the account's S3 bill exactly. `bucket_map` aggregates to
  **one row per bucket** because a raw join would multiply its cost. Absence of a mapping is
  never "no Databricks storage cost" — a `cost_explorer`-sourced AWS connection can never map
  a bucket (no `resource_id`). An empty metadata pull is a **no-op, not a purge**.
  The tab deliberately shows **no per-bucket list of unmanaged buckets**: on a real account
  (2,008 buckets, one metastore root) it buried the one number the tab exists to report.
- **Backing compute is the identical shape as backing storage, for a CLASSIC cluster's
  cloud VM instead of its storage bucket** (`066_gold_compute.sql`,
  `views/backing_compute.py`, see `docs/design/backing-compute.md`). The map comes from
  Databricks' own `system.compute.node_timeline` (`instance_id`, `cluster_id`, `driver`,
  `node_type`), pulled by `DatabricksConnector.fetch_compute_instances` on a SQL
  warehouse — unlike storage's pure-REST bucket pull. `mapping='databricks'` means the
  EC2 instance's id AND charge_month matched a row there; `unmapped` deliberately
  includes non-instance EC2-service resources (EBS volumes, Elastic IPs) that carry a
  ResourceId but never match an instance. **CLASSIC COMPUTE ONLY, so the figure is a
  floor, not a total** — `node_timeline` has zero rows for serverless SQL
  warehouses/jobs/DLT pipelines (no customer-visible instance exists at all), and that
  gap grows as serverless adoption grows; there is no equivalent of storage's deferred
  `AccountClient.storage.list()` fix here, because there is genuinely nothing to list.
  One structural improvement over storage: `system.compute.node_timeline` reports
  **bounded historical activity** (rows carry `start_time`/`end_time`), not present-tense
  state, so `ComputeInstanceRecord` partition-replaces by real `charge_month` (like
  `DriverHealthRecord`) instead of storage's present-tense-snapshot-applied-to-history
  hack, and the GOLD join matches `(instance_id, charge_month)` rather than one snapshot
  against every month of cost. The cost is `system.compute.node_timeline`'s ~90-day
  retention: an instance's activity before that window at the time of a given ingest can
  never be recovered after the fact. EC2 joined the S3-era default
  `DEFAULT_INCLUDE_SERVICES` (`ingest/_ec2_service_names.py`) for the same reason S3 did
  — the mapped figure needs a denominator by default — and is excluded from `aws.*`
  GOLD via `silver.focus_provider_bill` the same way S3 is. The KPI card
  (`backing_compute.kpi_card`) shares Backing storage's hue (both are "a satellite AWS
  bill, not a slice of net") and is **omitted, never rendered as $0**, when nothing is
  mapped. ⚠ The exact FOCUS `ServiceName` for EC2 is UNVALIDATED against a live export —
  same caveat class as the S3/Redshift keyword tables.
- **Partition-replace ingest**: each run is authoritative for the (connector,
  charge-period window) it pulls — `lake.bronze.write_window` removes that
  connector's `x_source_connector=…/charge_month=…/` partition dirs across the
  window, then writes the fresh pull. Re-running is idempotent and self-purging (a
  month the source no longer reports loses its partition; bad/orphaned rows can't
  survive). `dedupe_key` (incl. `record_id`/`record_type`) is only a within-batch
  uniqueness guard. Databricks corrections (RETRACTION/RESTATEMENT) land as distinct
  rows and net via `SUM` downstream.
- **Single currency**: ingest asserts `billing_currency == FLASHLIGHT_BASE_CURRENCY`.
- **Attribution honesty**: untagged/unattributed spend is surfaced, never silently
  dropped — the tag views drop untagged rows by construction, so
  `spend_tag_coverage_month.untagged_cost` is the honest remainder they reconcile
  against.
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
