# Get started

Flashlight is a local-first cloud-spend observability application. It normalizes billing
data into the FinOps FOCUS model, builds a Parquet metrics lake, and exposes the same
published metrics through a dashboard and an MCP server.

## Before you begin

You need Python 3.12 or later. A first evaluation needs no cloud credentials: the sample
dataset is enough. To use production data, you need access to at least one supported
source and permission to read its billing or telemetry data.

| Your goal | Start here |
| --- | --- |
| Evaluate Flashlight locally | [Quickstart](../quickstart.md) |
| Connect AWS, Databricks, or Redshift | [Connect a real source](real-source.md) |
| Understand how a total is calculated | [FOCUS and cost integrity](../concepts/focus-and-integrity.md) |
| Query metrics from an agent | [Use Flashlight with agents](../guides/mcp.md) |
| Run it as an operator | [Ingest and manage data](../guides/ingest.md) |

## What Flashlight stores

Flashlight stores its state as Parquet files under `FLASHLIGHT_HOME`. It does not require
a server database, Docker, or a hosted control plane. The default location is the
operating-system user-data directory; set `FLASHLIGHT_HOME` to choose another location.

The `ingest` command is the only writer. The dashboard and MCP server are independent,
read-only readers of published GOLD files. This means a dashboard chart and an agent
query read the same data contract.

## Core workflow

1. Configure one or more connectors and provide their credentials outside the config file.
2. Run `flashlight ingest` to write canonical BRONZE data and publish GOLD metrics.
3. Explore the dashboard or query the MCP server.
4. Re-run ingest on a schedule; use `flashlight transform` when only metric definitions
   need rebuilding.

Read [Data architecture](../architecture.md) for the detailed model.
