# Databricks

The Databricks connector runs a vendored Databricks System Tables-to-FOCUS 1.3 mapping
on a SQL warehouse, then writes the resulting canonical records. It can also collect
efficiency, driver-health, AI-usage, storage-location, and compute-inventory telemetry.

## Configure

```yaml
connectors:
  - type: databricks
    enabled: true
    name: Production workspace
    host: https://dbc-xxxxxxxx.cloud.databricks.com
    token_env: DATABRICKS_TOKEN
    sql_warehouse_id: null  # auto-select; set explicitly for production control
```

`DATABRICKS_TOKEN` must be available in the environment or local keychain path used by
Flashlight. Prefer a dedicated, read-only service principal where your deployment model
allows it.

## Access prerequisites

The configured identity needs access to the System Tables used by the cost mapping and
the enabled telemetry queries, including billing, compute, pipelines, and workspace
metadata. It also needs permission to use the chosen SQL warehouse.

The exact availability of System Tables varies by Databricks account, region, edition,
and feature. Flashlight degrades telemetry independently: a telemetry failure should not
discard successful canonical cost ingestion.

## Mapping and semantics

The connector uses the documented vendored SQL rather than reimplementing billing math
in Python. Flashlight adds `x_compute_class` where needed to distinguish classic and
serverless context not represented by a universal FOCUS field.

For AI costs, token measurements are stored separately from billing. A dollar-per-token
claim is made only where the serving model supports that relationship. Read
[AI costs and usage](../design/ai-costs.md) before interpreting the figures.
