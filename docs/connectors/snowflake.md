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

## Dashboard

`/snowflake` is the standard provider spend surface, backed only by materialized
Snowflake GOLD data. Run `fl ingest` after configuring the connection; before the
first successful ingest the page shows an empty state. It never queries Snowflake
from the dashboard process and has no local synthetic-data fallback. See
[Snowflake docs](../snowflake/user-guide.md).
