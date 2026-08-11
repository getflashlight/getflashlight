# Snowflake connector

Pulls organization-level cost from Snowflake
`ORGANIZATION_USAGE.USAGE_IN_CURRENCY_DAILY` into canonical FOCUS records, and
optionally client-driver fleet health from `ACCOUNT_USAGE`.

## What it produces

| Plane | Source | Notes |
| --- | --- | --- |
| FOCUS cost | `USAGE_IN_CURRENCY_DAILY` | Already in currency — no credit→dollar conversion. Daily grain per account / service_type / usage_type. |
| Driver health | `ACCOUNT_USAGE.SESSIONS` + `QUERY_HISTORY` | Sets `support_status` (`supported` / `unsupported` / `unknown`) against Snowflake's published minimum driver versions. |
| Efficiency / waste | — | Not implemented yet (no `fetch_efficiency`). |

## Config (`connections.yml`)

```yaml
- type: snowflake
  enabled: true
  name: Prod org
  account: xy12345.us-east-1
  user_env: SNOWFLAKE_USER
  password_env: SNOWFLAKE_PASSWORD
  role: ACCOUNTADMIN
  # warehouse: COMPUTE_WH
  # database: SNOWFLAKE
  # authenticator: externalbrowser
  # private_key_path: /path/key.pem
```

Auth priority: `private_key_path` → `authenticator` → password from `password_env`
(resolved via process env or the OS keychain, same as other connectors).

## Permissions and queries

Flashlight submits read-only `SELECT` statements over the configured date window. It never
creates Snowflake objects or changes warehouse settings. Use a dedicated role and warehouse
where practical; `ACCOUNTADMIN` is a convenient first test, not the least-privilege target.

| Plane | Source queried | What Flashlight selects and why | Access required |
| --- | --- | --- | --- |
| Cost | `SNOWFLAKE.ORGANIZATION_USAGE.USAGE_IN_CURRENCY_DAILY` | Daily organization/account/service/usage rows with `USAGE_IN_CURRENCY`, currency, adjustment, and billing fields. These become the FOCUS cost records; the date predicate limits the scan to the ingest window. | Access to the Organization Usage schema and the currency view. Snowflake identifies `ORGANIZATION_BILLING_VIEWER` as the view-specific application role; users with `ORG_USAGE_ADMIN` can access all organization-usage views. |
| Driver health | `SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY` joined to `SNOWFLAKE.ACCOUNT_USAGE.SESSIONS` | Counts queries per month, user, and client driver/version. The output only powers fleet driver-support reporting; it is not used to calculate cost. | Read access to the shared `SNOWFLAKE` database's Account Usage views—commonly granted via the appropriate Snowflake database/application roles or imported privileges—and permission to use the selected warehouse. |

The driver-health query carries user names and client-application identifiers in its result.
Treat that as operational telemetry: restrict the role, warehouse query history, and
Flashlight lake access to the people who need it. If the identity cannot read that optional
telemetry, cost ingestion should still be configured and validated independently.

Snowflake's [Organization Usage reference](https://docs.snowflake.com/en/sql-reference/organization-usage)
describes access to the organization-level views, and its
[cost-management access-control guide](https://docs.snowflake.com/user-guide/cost-access-control)
explains the viewer roles. Availability also depends on account type—for example, some
reseller contracts cannot expose `USAGE_IN_CURRENCY_DAILY`.

## Dashboard

`/snowflake` is the standard provider spend surface, backed only by materialized
Snowflake GOLD data. Run `fl ingest` after configuring the connection; before the
first successful ingest the page shows an empty state. It never queries Snowflake
from the dashboard process and has no local synthetic-data fallback. See
[Snowflake docs](../snowflake/user-guide.md).
