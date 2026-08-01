# Architecture

```
sources ──▶ flashlight ingest ──▶ Parquet lake (FLASHLIGHT_HOME) ──▶ readers
 AWS FOCUS export    (writer)     bronze/  partitioned, source of truth   flashlight mcp serve
 Databricks tables                gold/    *.parquet ◀── the only surface  flashlight dashboard serve
 AWS Cost Explorer                         consumers read                 (each: own in-mem DuckDB)
```

Three independent processes: `ingest` is the sole writer; `mcp serve` and
`dashboard serve` are read-only. Concurrency is "many readers over immutable
Parquet, publish by atomic per-file rename" — no locks, no server. There is no
REST API, no database, and no migrations: Parquet is self-describing and the
`FocusRecord` Pydantic model is the schema.

## The medallion

- **BRONZE** `bronze/` — canonical FOCUS records, Hive-partitioned by connector +
  charge month; partition-replace makes re-ingest idempotent and self-purging.
- **SILVER** (in-memory only) — cleaned view + the Databricks↔AWS **TCO join** with
  the double-count guard (classic compute adds infra; serverless does not).
- **GOLD** `gold/<group>/*.parquet` — the metrics contract the dashboard and MCP
  both read, so a chart and an agent never disagree. Built by `transform` via
  DuckDB `COPY`, split per provider (`aws.monthly_bill`, `databricks.monthly_bill`)
  plus `shared` (cross-provider TCO), `efficiency` (waste), and `driver_health`
  (Databricks client-driver fleet health — a compliance signal, no cost metric).

## The efficiency plane

Utilization telemetry doesn't fit `FocusRecord`, so efficiency is a second,
parallel medallion: connectors emit `EfficiencyRecord` rows (best-effort — a
failed pull never blocks the cost ingest) into the `metrics/` Parquet root, and
the GOLD waste view classifies them into waste categories with recoverable cost.
Details: [Efficiency / waste](design/efficiency-waste.md).

## Package layout

```
src/flashlight/
  focus/      canonical FOCUS model + enums
  ingest/     connectors (aws_focus, databricks, redshift) + runner
  lake/       the Parquet layer: paths, schema, bronze writes, DuckDB, publish
  transform/  SILVER/GOLD SQL + runner (builds gold/*.parquet) + metric catalog
  gold/       reader.py — the shared GOLD read surface (MCP + dashboard)
  mcp/        MCP server over the GOLD views (the agent consumer surface)
  dashboard/  NiceGUI app over the GOLD views (the human consumer surface)
  cli.py      the unified `flashlight` command
```
