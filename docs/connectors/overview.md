# Connector overview

A connector maps a source system into Flashlight's canonical contracts. Cost connectors
produce FOCUS records. Some connectors also provide optional efficiency, driver-health,
AI-usage, storage-location, or compute-inventory telemetry.

## Support matrix

| Connector | Cost | Efficiency | Other telemetry | Status |
| --- | --- | --- | --- | --- |
| AWS FOCUS export | Yes | No | — | Supported |
| AWS Cost Explorer | Yes, coarser totals | No | — | Supported fallback |
| Databricks system tables | Yes | Yes | Driver health, AI usage, storage locations, compute inventory | Supported |
| Amazon Redshift | No* | Yes | Query patterns and operational telemetry | Supported |
| Snowflake | Yes | No | Driver health (`support_status` vs published minimums) | Supported |
| BigQuery | — | — | — | Planned stub |

\*Redshift cost is expected to arrive through AWS FOCUS data. The Redshift connector does
not duplicate that cost pull; it adds telemetry that makes the AWS cost more actionable.

## Multiple connections

You may configure multiple connections of the same type. Give each a distinct `name`.
Flashlight stamps that stable connector identity into the source provenance and uses it
when targeting `flashlight ingest --connector …`.

## Before enabling a connector

1. Read its setup guide and grant the documented minimum permissions.
2. Store credentials in environment variables or the supported keychain path.
3. Run a small explicit date range.
4. Inspect the run result, dashboard totals, and expected provider/account dimensions.
5. Expand to historical backfill only after the mapping and scope are correct.
