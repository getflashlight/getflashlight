# Troubleshooting

## Installation or command is unavailable

Confirm that you installed the PyPI package as `getflashlight`, then run
`flashlight --help`. If your shell cannot find it, use the same Python environment that
performed the install (`python -m pip show getflashlight`) or activate the environment.

## Dashboard starts but has no data

Run `flashlight sample` to verify the local lake and dashboard independently of cloud
credentials. For production data, run `flashlight ingest` in the same environment and
with the same `FLASHLIGHT_HOME` used by the dashboard. GOLD is empty until a sample or
successful transform exists.

## Ingest fails

Start with a single connector and a small explicit range:

```bash
flashlight ingest --connector "Production AWS" --start 2026-07-01 --end 2026-07-02
```

Check the connector's endpoint/identifiers, credential environment-variable names,
cloud permissions, and source-data availability. A connector can succeed with zero rows
when the source itself has no data for the window; distinguish that from an authentication
or mapping error in the command output/run log.

## Totals do not match a provider invoice

Compare equal periods, currency, account/service scope, and cost basis. Flashlight cost
views use `EffectiveCost`, preserve negative credits/refunds, and may flag a partial
current period. Read [FOCUS and cost integrity](../concepts/focus-and-integrity.md) before
changing connector filters or assuming a mapping defect.

## AWS export data is missing

Confirm that the configured S3 prefix is the export root containing `data/` and metadata,
not a parent/child prefix. Use `flashlight aws describe-export` to inspect delivered
periods, files, and size. Verify the S3 delivery policy and the ingest identity's read
access separately.

## Databricks or Redshift telemetry is missing

Canonical cost and optional telemetry have independent availability. Confirm that the
configured identity can use the SQL warehouse/Data API and can read the required system
tables. Some source features and System Tables are account/region/edition dependent.
Successful cost data does not imply every optional telemetry plane is available.

## MCP cannot be reached

Confirm `flashlight mcp serve` is running and check `FLASHLIGHT_MCP_HOST`/`_PORT`. The
server does not start in demo mode. Do not solve connectivity by binding an unauthenticated
MCP listener to the public internet; use a private network or authenticated proxy.

## Need more help

Collect the command, non-secret configuration shape, selected time window, connector
name, and complete sanitized error. Open an issue at
[GitHub Issues](https://github.com/getflashlight/getflashlight/issues) with those details.
