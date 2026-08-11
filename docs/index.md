# <img src="assets/logo.svg" alt="" width="40" style="vertical-align:-8px; margin-right:0.5rem;"> Flashlight

*Finds what's burning money in the dark.*

![Version](https://img.shields.io/pypi/v/getflashlight)
![Python versions](https://img.shields.io/badge/python-3.12%2B-blue)
![License](https://img.shields.io/badge/license-Apache--2.0-blue)

Flashlight is a local cloud-cost explorer for teams that need answers they can inspect.
It brings billing and operational data into a [FinOps FOCUS](https://focus.finops.org/focus-specification/)
lake, then serves the same published metrics to a dashboard and to MCP-compatible agents.
It shows spend, allocation context, and evidence-backed recoverable-spend candidates; it
does not change cloud resources.

## Choose a path

<div class="grid cards" markdown>

- :material-rocket-launch: **Try the sample**

    ---

    Install Flashlight, load linked sample billing and telemetry, and open the dashboard.

    [Start the quickstart](quickstart.md)

- :material-cloud-download: **Connect a source**

    ---

    Configure AWS, Databricks, Redshift, or Snowflake with documented read-only access.

    [Choose a connector](getting-started/real-source.md)

- :material-robot-outline: **Use an agent**

    ---

    Give an MCP-compatible agent read-only access to the same metrics as the dashboard.

    [Set up MCP](guides/mcp.md)

- :material-book-open-page-variant: **Understand the numbers**

    ---

    Learn what each total includes, its grain, and the integrity rules behind it.

    [Read the concepts](concepts/focus-and-integrity.md)

</div>

## What stays true

- **Your data stays local.** `pip install getflashlight` is enough: no Docker,
  database server, or hosted control plane. State is Parquet under `FLASHLIGHT_HOME`.
- **A total has one definition.** Every cost view is derived from `EffectiveCost` at
  charge-period grain. Credits, refunds, currency, and partial periods remain visible.
- **Recommendations are evidence, not automation.** Efficiency findings identify
  candidates and their supporting telemetry; Flashlight never applies a cloud change.
- **The dashboard and agents query the same contract.** Both are read-only consumers of
  the published GOLD files, so an answer can be reproduced in either interface.

## Install

```bash
pip install getflashlight
flashlight sample               # generate linked Redshift, Databricks, and FOCUS demo data
flashlight dashboard serve      # dashboard → http://127.0.0.1:8501
```

Connecting real billing starts with `connections.yml`; see [Quick start](quickstart.md)
and [Connectors](connectors/overview.md).

## Data integrity

The SILVER/GOLD layer enforces the rules that make FOCUS data safe to sum:

- One cost metric per view (`EffectiveCost`)
- Charge-period grain only, partial current period flagged
- Credit/refund signs preserved
- Single currency asserted at ingest
- AWS spend that can't be attributed to a cluster is shown as an explicit
  unattributed bucket, never hidden

## Documentation

- [Get started](getting-started/overview.md) — installation, a short trial, and production setup
- [User guide](guides/configure.md) — configure, ingest, explore, and use agents
- [Concepts](concepts/focus-and-integrity.md) — FOCUS, data contracts, integrity, and metric definitions
- [Connectors](connectors/overview.md) — supported sources, prerequisites, and mapping details
- [Reference](reference/cli.md) — commands, configuration, data contracts, and MCP tools
- [Troubleshooting](troubleshooting/index.md) — solve setup, credentials, data, and serving issues
- <a href="llms.txt">llms.txt</a> — an index of these docs for LLM tooling; the MCP server (`flashlight mcp serve`) reads the same GOLD views the dashboard does
