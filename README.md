# Flashlight

<p align="center">
  <img src="https://getflashlight.app/assets/logo.svg" width="112" alt="Flashlight — the signal in the noise">
</p>

<p align="center">
  <a href="https://github.com/getflashlight/getflashlight/actions/workflows/ci.yml"><img src="https://github.com/getflashlight/getflashlight/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="https://pypi.org/project/getflashlight/"><img src="https://img.shields.io/pypi/v/getflashlight" alt="PyPI"></a>
  <a href="https://pypi.org/project/getflashlight/"><img src="https://img.shields.io/badge/Python-3.12%2B-blue" alt="Python 3.12 or later"></a>
  <a href="https://github.com/getflashlight/getflashlight/blob/main/LICENSE"><img src="https://img.shields.io/github/license/getflashlight/getflashlight" alt="Apache-2.0 license"></a>
</p>

Flashlight is a local cloud-cost explorer. It normalizes cloud billing into a FinOps FOCUS
lake, then serves the same published metrics to a dashboard and MCP-compatible agents.
Use it to inspect spend, allocation context, and evidence-backed recoverable-spend
candidates—never to make cloud changes automatically.

It runs locally: no hosted control plane, database server, or Docker is required. Data is
stored as Parquet under `FLASHLIGHT_HOME` and queried with in-memory DuckDB.

## Install and try the sample

```bash
pip install getflashlight
flashlight sample
flashlight dashboard serve
```

Open `http://127.0.0.1:8501` to explore deterministic sample billing and telemetry. The
sample is the fastest way to confirm the installation without cloud credentials.

## Connect a real source

Use the dashboard's **Connections** page to add AWS, Databricks, Amazon Redshift, or
Snowflake. The dashboard stores credentials in the operating system's credential store; it
writes only non-secret connection references to configuration files.

For a real source, select a continuous sync range of at least six months. That provides the
history needed for trends, month-over-month comparisons, and savings analysis.

Read the [connection walkthrough](https://getflashlight.app/getting-started/real-source/)
before syncing. It explains each field, the required permissions, and the queries each
connector performs.

| Source | Primary use | Setup and permissions |
| --- | --- | --- |
| AWS FOCUS Data Export | Canonical AWS cost data | [AWS connector](https://getflashlight.app/connectors/aws/) |
| Databricks | FOCUS cost mapping and operational telemetry | [Databricks connector](https://getflashlight.app/connectors/databricks/) |
| Amazon Redshift | Efficiency and workload telemetry; AWS supplies its cost | [Redshift connector](https://getflashlight.app/connectors/redshift/) |
| Snowflake | Organization cost and driver-health telemetry | [Snowflake connector](https://getflashlight.app/connectors/snowflake/) |

For a CLI, scheduler, or headless deployment, provide the named credential environment
variables through the process environment or your secret manager. A local `.env` is a
development convenience and must never be committed.

## Use Flashlight with an agent

The dashboard can start a local, read-only MCP server and provides the client configuration.
For a long-running service, start it directly:

```bash
flashlight mcp serve
```

The default endpoint is `http://127.0.0.1:8002/mcp`. It has no built-in authentication, so
keep it on localhost or behind your own authenticated private network boundary.

## How the data stays trustworthy

`flashlight ingest` is the sole writer. It publishes complete GOLD Parquet metric views;
the dashboard and MCP server are read-only consumers of those views. Every cost view uses
`EffectiveCost` at charge-period grain, preserving credits and refunds while avoiding
partial published results.

Read [Data architecture](https://getflashlight.app/architecture/) and
[FOCUS and cost integrity](https://getflashlight.app/concepts/focus-and-integrity/) for the
data contract and reconciliation rules.

## Documentation and support

- [Documentation](https://getflashlight.app/)
- [Dashboard guide](https://getflashlight.app/guides/dashboard/)
- [MCP guide](https://getflashlight.app/guides/mcp/)
- [Configuration reference](https://getflashlight.app/reference/configuration/)
- [Report an issue](https://github.com/getflashlight/getflashlight/issues)
- [Contributing guide](https://github.com/getflashlight/getflashlight/blob/main/CONTRIBUTING.md)
