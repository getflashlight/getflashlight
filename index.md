# Flashlight

*Finds what's burning money in the dark.*

Flashlight is a local cloud-cost explorer for teams that need answers they can inspect. It brings billing and operational data into a [FinOps FOCUS](https://focus.finops.org/focus-specification/) lake, then serves the same published metrics to a dashboard and to MCP-compatible agents. It shows spend, allocation context, and evidence-backed recoverable-spend candidates; it does not change cloud resources.

## Choose a path

- **Try the sample**

  ______________________________________________________________________

  Install Flashlight, load linked sample billing and telemetry, and open the dashboard.

  [Start the quickstart](https://getflashlight.app/quickstart/index.md)

- **Connect a source**

  ______________________________________________________________________

  Configure AWS, Databricks, Redshift, or Snowflake with documented read-only access.

  [Choose a connector](https://getflashlight.app/getting-started/real-source/index.md)

- **Use an agent**

  ______________________________________________________________________

  Give an MCP-compatible agent read-only access to the same metrics as the dashboard.

  [Set up MCP](https://getflashlight.app/guides/mcp/index.md)

- **Understand the numbers**

  ______________________________________________________________________

  Learn what each total includes, its grain, and the integrity rules behind it.

  [Read the concepts](https://getflashlight.app/concepts/focus-and-integrity/index.md)

## What stays true

- **Your data stays local.** `pip install getflashlight` is enough: no Docker, database server, or hosted control plane. State is Parquet under `FLASHLIGHT_HOME`.
- **A total has one definition.** Every cost view is derived from `EffectiveCost` at charge-period grain. Credits, refunds, currency, and partial periods remain visible.
- **Recommendations are evidence, not automation.** Efficiency findings identify candidates and their supporting telemetry; Flashlight never applies a cloud change.
- **The dashboard and agents query the same contract.** Both are read-only consumers of the published GOLD files, so an answer can be reproduced in either interface.

## Install

```
pip install getflashlight
flashlight sample               # generate linked Redshift, Databricks, and FOCUS demo data
flashlight dashboard serve      # dashboard → http://127.0.0.1:8501
```

Connecting real billing starts with `connections.yml`; see [Quick start](https://getflashlight.app/quickstart/index.md) and [Connectors](https://getflashlight.app/connectors/overview/index.md).

## Data integrity

The SILVER/GOLD layer enforces the rules that make FOCUS data safe to sum:

- One cost metric per view (`EffectiveCost`)
- Charge-period grain only, partial current period flagged
- Credit/refund signs preserved
- Single currency asserted at ingest
- AWS spend that can't be attributed to a cluster is shown as an explicit unattributed bucket, never hidden

## Documentation

- [Get started](https://getflashlight.app/getting-started/overview/index.md) — installation, a short trial, and production setup
- [User guide](https://getflashlight.app/guides/configure/index.md) — configure, ingest, explore, and use agents
- [Concepts](https://getflashlight.app/concepts/focus-and-integrity/index.md) — FOCUS, data contracts, integrity, and metric definitions
- [Connectors](https://getflashlight.app/connectors/overview/index.md) — supported sources, prerequisites, and mapping details
- [Reference](https://getflashlight.app/reference/cli/index.md) — commands, configuration, data contracts, and MCP tools
- [Troubleshooting](https://getflashlight.app/troubleshooting/index.md) — solve setup, credentials, data, and serving issues
- [llms.txt](https://getflashlight.app/llms.txt) — an index of these docs for LLM tooling; the MCP server (`flashlight mcp serve`) reads the same GOLD views the dashboard does
