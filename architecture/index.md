# Architecture

flowchart LR subgraph sources[Cloud sources] aws_export["AWS FOCUS Data Export"] aws_ce["AWS Cost Explorer"] databricks["Databricks system tables"] redshift["Amazon Redshift telemetry"] snowflake["Snowflake usage and telemetry"] end subgraph writer[One writer] ingest["flashlight ingest"] end subgraph lake[Parquet lake · FLASHLIGHT_HOME] bronze\["BRONZE<br/>canonical source records"\] silver\["SILVER<br/>normalized in memory"\] gold\["GOLD<br/>published metric views"\] end subgraph readers[Read-only consumers] dashboard\["Dashboard<br/>human interface"\] mcp\["MCP server<br/>agent interface"\] end aws_export --> ingest aws_ce --> ingest databricks --> ingest redshift --> ingest snowflake --> ingest ingest --> bronze --> silver --> gold gold --> dashboard gold --> mcp

## The important boundary

`flashlight ingest` is the only process that writes the lake. The dashboard and MCP server open their own in-memory DuckDB connections over already-published GOLD Parquet; neither changes source data or local lake files. A publish uses atomic per-file replacement, so readers see either the prior complete metric or the next complete metric—not a partial one.

That boundary is why a chart and an agent answer use the same metric contract, and why a dashboard may control an ingest without becoming a second writer. There is no database server, REST API, or migration layer: Parquet is the persistent store and `FocusRecord` is the canonical cost-record schema.

The dashboard can *launch* the other two rather than doing their work: its Connections page shells out to `flashlight ingest` and its MCP server page starts/stops `flashlight mcp serve`. Both go through the same CLI entrypoint a terminal user would run, so there's one implementation of each — the dashboard is a control surface, never a second writer and never a second server.

User configuration lives in `<home>/config/`: `connections.yml` (sources), `policies.yml` (policy thresholds), `assistant.yml` (the BYOK model choice). Env vars override each file; secrets are in the OS keychain (or the env vars the config names), never in these files.

## The medallion

| Layer      | Location                 | What belongs there                                                                                                         | Why it exists                                                                                       |
| ---------- | ------------------------ | -------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------- |
| **BRONZE** | `bronze/`                | Canonical FOCUS cost records and source telemetry, partitioned by connector and charge month                               | It is the durable source of truth. Re-ingesting a partition replaces it, so retries are idempotent. |
| **SILVER** | In memory                | The cleaned, normalized cost model                                                                                         | Every cost metric starts from one canonical `EffectiveCost` value at charge-period grain.           |
| **GOLD**   | `gold/<group>/*.parquet` | Published metric views, including provider spend, efficiency, policy, driver health, AI usage, and backing storage/compute | It is the stable contract shared by dashboard and MCP readers.                                      |

GOLD is split by provider, such as `aws.monthly_bill` and `databricks.monthly_bill`, plus cross-provider groups. The `storage` group labels AWS S3 cost with Unity Catalog's bucket map; it is **never** added to Databricks spend. See [Backing storage](https://getflashlight.app/design/backing-storage/index.md).

### Why the billing period stops at BRONZE

FOCUS defines two time grains: the **charge period** (when usage happened) and the **billing period** (which invoice it lands on). `BillingPeriodStart`/`End` are ingested and stored on every BRONZE row — `FocusRecord` is the schema of record, so nothing is thrown away — but they are deliberately **not** projected into `silver.focus_normalized`, and no GOLD view or dashboard control exposes them.

Three reasons, in order of weight:

1. **Only the charge period is additive.** Aggregating on the billing period double-counts usage that was re-invoiced and hides usage not yet invoiced. Keeping the columns out of SILVER means no downstream view *can* accidentally group by them — the invariant is enforced by the schema, not by everyone remembering it.
1. **They carry no information this data doesn't already have.** Measured across both lakes: `billing_period_start` equals `date_trunc('month', charge_period_start)` and `billing_period_end` equals the following month for **940,790 of 940,791** rows of a real AWS + Databricks lake and **1,023 of 1,024** rows of the demo lake, across all three mapping paths (`connectors/_focus_map.py`, `connectors/aws_focus.py`, `focus/sql_mapping.py`). The sole exception is one synthetic Oracle row in the FinOps FOCUS sample. A billing-period dimension would be a relabelled `charge_month`.
1. **A second month key is a trap.** Two plausible month columns in SILVER invites a future view to pick the wrong one, and the failure is silent — the numbers still add up, they're just answering a different question.

If a future connector reports a genuinely different billing period (a non-calendar billing cycle, or invoicing that lags the charge month), this stops being true. That's why the claim is a test, not a comment: `tests/test_billing_period_invariant.py` fails the moment the committed demo lake violates it. Invoice-level reconciliation is served instead by `invoice_reconciliation_month`, which groups by the real `InvoiceId`.

## The efficiency plane

Utilization telemetry doesn't fit `FocusRecord`, so efficiency is a second, parallel medallion: connectors emit `EfficiencyRecord` rows (best-effort — a failed pull never blocks the cost ingest) into the `metrics/` Parquet root, and the GOLD waste view classifies them into waste categories with recoverable cost. Details: [Efficiency / waste](https://getflashlight.app/design/efficiency-waste/index.md).

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
