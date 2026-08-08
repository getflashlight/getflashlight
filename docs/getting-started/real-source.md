# Connect a real source

Flashlight reads connector definitions from `connections.yml`. Credentials are referenced
by environment-variable name and are never stored in that file.

## 1. Initialize configuration

```bash
flashlight init
```

This creates `<FLASHLIGHT_HOME>/config/connections.yml`. Copy the appropriate example
from the generated file, set `enabled: true`, and remove unused example connectors.

## 2. Provide credentials

For a local evaluation, create a `.env` file in the directory where you run the command:

```dotenv
AWS_ACCESS_KEY_ID=...
AWS_SECRET_ACCESS_KEY=...
DATABRICKS_TOKEN=...
```

The CLI loads `.env` without overriding variables already present in the shell. In a
service or CI environment, use its secret manager or environment injection instead.

## 3. Ingest

```bash
flashlight ingest --start 2026-07-01 --end 2026-07-31
```

The command attempts every enabled connector, rebuilds GOLD from successful inputs, and
returns non-zero if any connector failed. Start with a bounded date range while validating
permissions and mapping.

## 4. Explore

```bash
flashlight dashboard serve
```

Open `http://127.0.0.1:8501`. Use the Connections page to see configured sources and
the run log to inspect ingest outcomes.

## Pick your connector

| Source | Primary use | Setup guide |
| --- | --- | --- |
| AWS FOCUS Data Export | Canonical AWS cost data | [AWS](../connectors/aws.md) |
| Databricks system tables | Databricks cost and operational telemetry | [Databricks](../connectors/databricks.md) |
| Amazon Redshift | Redshift efficiency telemetry; AWS provides its costs | [Amazon Redshift](../connectors/redshift.md) |

Read the [connector support matrix](../connectors/overview.md) before enabling more than
one connection of a type.
