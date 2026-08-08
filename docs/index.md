# <img src="assets/logo.svg" alt="" width="40" style="vertical-align:-8px; margin-right:0.5rem;"> Flashlight

*Finds what's burning money in the dark.*

![Version](https://img.shields.io/badge/version-0.2.0-blue)
![Python versions](https://img.shields.io/badge/python-3.12%2B-blue)
![License](https://img.shields.io/badge/license-Apache--2.0-blue)

FOCUS-based, multi-cloud cloud-spend visualization.

Flashlight ingests cloud billing in the [FinOps FOCUS](https://focus.finops.org/focus-specification/)
format, standardizes it into a layered data model, and serves a bundled NiceGUI
dashboard plus an MCP server for agents. It answers what you're actually
spending — across every cloud and data platform on one FOCUS-normalized bill,
plus how much of it is recoverable waste.

## Choose a path

<div class="grid cards" markdown>

- :material-rocket-launch: **Try it in five minutes**

  ---

  Install Flashlight, load the FOCUS sample, and open the dashboard.

  [Start the quickstart](quickstart.md)

- :material-cloud-download: **Connect production billing**

  ---

  Configure AWS, Databricks, or Redshift with least-privilege credentials.

  [Connect a source](getting-started/real-source.md)

- :material-robot-outline: **Use an agent**

  ---

  Give an MCP-compatible agent read-only access to the same metrics as the dashboard.

  [Set up MCP](guides/mcp.md)

- :material-book-open-page-variant: **Understand the numbers**

  ---

  Learn the FOCUS model, the lake layers, and the integrity rules behind every total.

  [Read the concepts](concepts/focus-and-integrity.md)

</div>

## Why use Flashlight?

- **One binary, no infra.** `pip install getflashlight`. No Docker, no database
  server. State is Parquet under `FLASHLIGHT_HOME`, queried by a throwaway
  in-memory DuckDB in each process.
- **FOCUS-native.** Every connector maps its source into one canonical
  `FocusRecord`. One cost metric (`EffectiveCost`) is summed everywhere; nothing
  gets to invent its own column.
- **Recoverable spend, not just spend.** A parallel efficiency/waste plane
  measures the billed-but-not-used gap — idle, underutilized, wrong compute
  placement — as dollars, never as an auto-remediation action.
- **Humans and agents read the same numbers.** The dashboard and the MCP server
  are both read-only consumers of the same published GOLD Parquet, so a chart
  and an agent can't disagree.
- **Multi-cloud by construction.** A new provider is a connector that maps into
  the existing `FocusRecord`/`EfficiencyRecord` contracts — not a new dashboard
  or a new schema.

## Install

```bash
pip install getflashlight
flashlight sample               # download the FinOps FOCUS sample + seed it
flashlight dashboard serve      # dashboard → http://127.0.0.1:8501
```

Connecting real billing — AWS, Databricks, Redshift — is a `connections.yml`
away; see [Quick start](quickstart.md) and [Connectors](connectors/overview.md).

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
- [User guide](guides/configure.md) — configure, ingest, explore, deploy, and use agents
- [Concepts](concepts/focus-and-integrity.md) — FOCUS, data contracts, integrity, and metric definitions
- [Connectors](connectors/overview.md) — supported sources, prerequisites, and mapping details
- [Reference](reference/cli.md) — commands, configuration, data contracts, and MCP tools
- [Troubleshooting](troubleshooting/index.md) — solve setup, credentials, data, and serving issues
- <a href="llms.txt">llms.txt</a> — an index of these docs for LLM tooling; the MCP server (`flashlight mcp serve`) reads the same GOLD views the dashboard does
