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
docker compose up -d                                        # db, migrate, server, mcp, grafana
docker compose --profile ingest run --rm ingest            # pull billing data
open http://localhost:3000                                  # Grafana → Auralake → TCO Overview
```

* API: `http://localhost:8001` (`/health`, `/api/v1/metrics`)
* MCP: `http://localhost:8002` (streamable-http)
* CLI: `uv run --project packages/cli auralake metrics`

## FOCUS handling (why the numbers are trustworthy)

The SILVER/GOLD layer enforces the rules that make FOCUS data safe to sum:
one cost metric per view (`EffectiveCost`), charge-period grain only, partial
current period flagged, credit/refund signs preserved, single-currency asserted
at ingest, and AWS spend that can't be attributed to a cluster shown as an
explicit **unattributed** bucket rather than hidden.

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
  server/     FastAPI read API + ingest triggers
  mcp/        MCP server over the GOLD views
packages/cli/ thin HTTP CLI
deploy/grafana/ provisioned datasource + TCO dashboard
```
