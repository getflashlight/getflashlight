# Auralake

**FOCUS-based, multi-cloud Total Cost of Ownership (TCO) spend visualization.**

Auralake ingests cloud billing in the [FinOps FOCUS](https://focus.finops.org/focus-specification/)
format, standardizes it into a layered data model, and serves a bundled
**Streamlit** dashboard plus an **MCP server** for agents. It answers one question
well: *what are we actually spending* — including the often-hidden TCO of a
Databricks workload (DBU cost **plus** the AWS infra it provisions).

It installs with `pip install auralake` — **no Docker, no database server**.
Persistence is Parquet under `AURALAKE_HOME`, queried by an in-memory **DuckDB**.

> v1 visualizes current spend. It does not (yet) recommend optimizations.

## Architecture

```
sources ──▶ auralake ingest ──▶ Parquet lake (~/.auralake) ──▶ readers
 AWS FOCUS export    (writer)     bronze/  partitioned, source of truth   auralake mcp serve
 Databricks tables                gold/    *.parquet ◀── the only surface  auralake dashboard serve
 AWS Cost Explorer                         consumers read                 (each: own in-mem DuckDB)
```

Three independent processes: `ingest` is the sole writer; `mcp serve` and
`dashboard serve` are read-only. Concurrency is "many readers over immutable
Parquet, publish by atomic per-file rename" — no locks, no server.

* **BRONZE** `bronze/` — canonical FOCUS records, Hive-partitioned by connector +
  charge month; partition-replace makes re-ingest idempotent and self-purging.
* **SILVER** (in-memory only) — cleaned view + the Databricks↔AWS **TCO join** with
  the double-count guard (classic compute adds infra; serverless does not).
* **GOLD** `gold/*.parquet` — the metrics contract the dashboard and MCP both read,
  so a chart and an agent never disagree. Built by `transform` via DuckDB `COPY`.

## Quick start

```bash
pip install auralake          # or: uv sync (from this repo)
auralake init                 # scaffold ~/.auralake + bundled FOCUS sample
auralake ingest               # load billing → BRONZE, rebuild GOLD
auralake dashboard serve      # dashboard → http://127.0.0.1:8501
```

* Dashboard: `http://127.0.0.1:8501` (Streamlit; consumer surface for humans)
* MCP: `http://localhost:8002` (`auralake mcp serve`; streamable-http, for agents)
* CLI: `auralake init | ingest | transform | mcp serve | dashboard serve | aws create-export`

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
  lake/       the Parquet layer: paths, schema, bronze writes, DuckDB, publish
  transform/  SILVER/GOLD SQL + runner (builds gold/*.parquet) + metric catalog
  gold/       reader.py — the shared GOLD read surface (MCP + dashboard)
  mcp/        MCP server over the GOLD views (the agent consumer surface)
  dashboard/  Streamlit app over the GOLD views (the human consumer surface)
  cli.py      the unified `auralake` command (init / ingest / transform / mcp / dashboard / aws)
```

`auralake mcp serve` and `auralake dashboard serve` are independent read-only
processes over `gold/*.parquet`. There is no REST API, no database, and no
migrations — Parquet is self-describing and `FocusRecord` is the schema.
