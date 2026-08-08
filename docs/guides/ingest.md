# Ingest and manage data

## Run an ingest

```bash
flashlight ingest
```

Without dates, Flashlight uses its configured lookback window. Specify an inclusive range
for a backfill or a reproducible run:

```bash
flashlight ingest --start 2026-01-01 --end 2026-06-30
```

Each enabled connector runs independently. A failed connector does not prevent successful
connectors from publishing their data; the process still exits with an error so schedulers
can detect the partial failure.

## Common operations

| Goal | Command |
| --- | --- |
| Run one configured source | `flashlight ingest --connector "Prod cost"` |
| Pull data but delay metric rebuild | `flashlight ingest --no-transform` |
| Build GOLD from existing BRONZE data | `flashlight transform` |
| Replace a connector's full retained history | `flashlight ingest --full-refresh --start … --end …` |
| Seed public sample data | `flashlight sample` |
| Remove only sample data | `flashlight sample --clean` |
| Preview a destructive cleanup | `flashlight cleanup --dry-run` |

## Refreshes and history

Ingest replaces the affected connector/month partitions, which makes normal re-ingestion
idempotent for the requested window. `--full-refresh` first removes that connector's
entire BRONZE history. Always pair it with an explicit, sufficiently wide date range;
otherwise the default lookback becomes the new retained history.

## Scheduling

Run `flashlight ingest` from your existing scheduler: cron, a CI runner, a container
platform, or your workflow orchestrator. Give each job the same `FLASHLIGHT_HOME`, source
configuration, and secret environment. A single writer should own a lake location.

The dashboard and MCP server may run at the same time as ingest: they consume immutable
published GOLD files. See [Data architecture](../architecture.md) for the publishing model.

## Clean up safely

`flashlight cleanup` removes all writer-produced lake data but keeps configuration. It
requires confirmation unless `--yes` is passed. To scope cleanup to a source, use
`flashlight cleanup --connector aws_focus`; it then rebuilds GOLD from the remaining data.
