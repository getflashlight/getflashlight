# Auralake

**FOCUS-based, multi-cloud Total Cost of Ownership (TCO) spend visualization.**

Auralake ingests cloud billing in the [FinOps FOCUS](https://focus.finops.org/focus-specification/)
format, standardizes it into a layered data model, and serves preconfigured
**Grafana** dashboards plus an **MCP server** for agents. It answers one question
well: *what are we actually spending* — including the often-hidden TCO of a
Databricks workload (DBU cost **plus** the AWS infra it provisions).

> v1 visualizes current spend. It does not (yet) recommend optimizations.

## Architecture

```
sources ──▶ ingest (FOCUS connectors) ──▶ Postgres ──▶ Grafana
 AWS FOCUS export                          raw  (BRONZE)    └▶ MCP server
 Databricks system tables                  silver (views)
 AWS Cost Explorer (fallback)              gold (views) ◀── the only surface
                                                            consumers read
```

* **BRONZE** `raw.focus_record` — canonical FOCUS landing table (idempotent upsert).
* **SILVER** `silver.*` — cleaned view + the Databricks↔AWS **TCO join** with the
  double-count guard (classic compute adds infra; serverless does not).
* **GOLD** `gold.*` — the metrics contract Grafana and MCP both read, so a chart
  and an agent never disagree.

The store is pluggable (Postgres is the bundled default; the SQL layer ports to
DuckDB / BigQuery / Redshift / Databricks).

## Quick start

```bash
cp .env.example .env
cp config/connections.example.yml config/connections.yml   # edit sources
docker compose up -d                                        # db, mcp, grafana
docker compose --profile ingest run --rm ingest            # pull billing data
open http://localhost:3000                                  # Grafana → Auralake → TCO Overview
```

* Grafana: `http://localhost:3000` (consumer surface for humans; reads Postgres)
* MCP: `http://localhost:8002` (streamable-http; consumer surface for agents)
* CLI: `uv run --project packages/backend auralake serve | ingest | transform | aws create-export`

## FOCUS handling (why the numbers are trustworthy)

The SILVER/GOLD layer enforces the rules that make FOCUS data safe to sum:
one cost metric per view (`EffectiveCost`), charge-period grain only, partial
current period flagged, credit/refund signs preserved, single-currency asserted
at ingest, and AWS spend that can't be attributed to a cluster shown as an
explicit **unattributed** bucket rather than hidden.

## Source connectors & FOCUS mappings

| Connector | Source | How it maps to FOCUS |
|---|---|---|
| `aws_focus` | AWS Data Exports (FOCUS 1.x Parquet in S3) | already FOCUS — light coercion |
| `focus_file` | Local FOCUS CSV/Parquet (sample data, any vendor export) | already FOCUS — light coercion |
| `databricks` | Databricks system tables | **vendored Databricks → FOCUS 1.3 SQL** (below) |
| `aws_infra` | AWS Cost Explorer (fallback when no native FOCUS export) | mapped to FOCUS in Python |
| `bigquery` / `snowflake` / `redshift` | — | stubs (planned) |

### Databricks mapping (based on the Databricks FOCUS query)

The `databricks` connector does **not** hand-roll the billing math. It runs the
authoritative **Databricks System Tables → FOCUS 1.3** query, vendored verbatim at
[`packages/backend/src/auralake/ingest/connectors/sql/databricks_focus_1_3.sql`](packages/backend/src/auralake/ingest/connectors/sql/databricks_focus_1_3.sql)
from the Databricks solution accelerator
[`databricks-solutions/cloud-infra-costs`](https://github.com/databricks-solutions/cloud-infra-costs/blob/main/focus/focus_query.sql).
The connector executes it on a SQL warehouse, then feeds the FOCUS-columned output
through the same shared mapper used by the file/S3 connectors. The only field we add
is `x_compute_class` (classic vs serverless), derived from the SKU — FOCUS doesn't
carry it, but the TCO double-count guard needs it.

**This SQL is repurposable** — that's a feature, not a one-off:

- **Run it standalone.** Paste it into Databricks SQL / a notebook (set the
  `:account_prices` parameter) to materialize a FOCUS table, export it to
  Parquet/Delta, and ingest via `aws_focus`/`focus_file` — no live API needed.
- **Template for other warehouses.** It's the reference pattern for *source-side*
  FOCUS mapping; the planned `snowflake`/`bigquery`/`redshift` connectors follow the
  same shape (run a warehouse-native FOCUS query, then `map_focus_row`).
- **Fork & extend.** The upstream mapping is explicitly "best-effort"; edit the
  vendored copy to add columns or refine the `billing_origin_product` taxonomy. To
  refresh it, re-pull the upstream file and re-apply the header.

> FOCUS™ is a trademark of the FinOps Foundation; the FOCUS spec is licensed
> CC-BY 4.0. The vendored query retains its source attribution in its header.

## Development

```bash
uv sync
uv run ruff check packages/
uv run mypy packages/backend
uv run pytest
```

## Layout

```
packages/backend/src/auralake/
  focus/      canonical FOCUS model + enums
  ingest/     connectors (aws_focus, databricks, aws_infra) + runner
  transform/  SILVER/GOLD SQL + runner + metric catalog
  store/      engine, BRONZE models, idempotent upsert, read-only query
  mcp/        MCP server over the GOLD views (the agent consumer surface)
  cli.py      the unified `auralake` command (serve / ingest / transform / aws)
deploy/grafana/ provisioned datasource + TCO dashboard
```

`auralake serve` runs the MCP server; Grafana reads Postgres GOLD directly. There
is no REST API. Migrations self-apply on startup.
