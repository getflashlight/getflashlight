"""The Parquet lake — Flashlight's persistence layer (no database, no server).

Persistent state is plain Parquet under ``FLASHLIGHT_HOME``:

    <home>/
    ├── config/connections.yml                          connector config
    ├── bronze/x_source_connector=…/charge_month=…/…     source of truth (partitioned)
    ├── gold/<view>.parquet                              the only consumer surface
    ├── gold.staging/                                    transient publish staging
    └── meta/runs/<run_id>.parquet                       ingest run log

The ingest process is the sole writer; the MCP and dashboard processes are
read-only and each spin up their own in-memory DuckDB over the same Parquet
(``lake.duck``). Concurrency is "many readers on immutable files, publish by
atomic per-file rename" — no locks, no coordination.
"""

from __future__ import annotations
