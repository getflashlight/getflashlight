# Snowflake

Flashlight ingests Snowflake cost data into the shared lake and presents it through
the same BRONZE to SILVER to GOLD pipeline used by Databricks and Redshift.

## Configure

Add an enabled connector to `<home>/config/connections.yml`:

```yaml
connectors:
  - type: snowflake
    enabled: true
    name: Snowflake-prod
    account: your-account-identifier
    user_env: SNOWFLAKE_USER__SNOWFLAKE_PROD
    password_env: SNOWFLAKE_PASSWORD__SNOWFLAKE_PROD
    role: ROLE_FLASHLIGHT
    warehouse: COMPUTE_WH
    database: SNOWFLAKE
    private_key_path: ~/.ssh/snowflake_rsa_key.p8
```

Credential environment variables are read by the ingest process. Password authentication
uses `user_env` and `password_env`; key-pair authentication uses `private_key_path`.

## Ingest

Run:

```bash
uv run fl ingest --connector Snowflake-prod
```

The connector projects FOCUS-shaped cost rows in Snowflake SQL and writes them to shared
BRONZE Parquet. The normal transform then publishes Snowflake GOLD views. The dashboard
and MCP server read only those materialized views; neither opens a Snowflake connection.

## Dashboard

Open `/snowflake` after a successful ingest. It is the standard provider spend page with
the common spend, allocation, efficiency, and policy surfaces plus Snowflake client-driver
health where telemetry is available. Before the first successful ingest it shows an empty
state. There is no local synthetic-data fallback.

## Data layout

| Layer | Location | Writer | Reader |
|---|---|---|---|
| BRONZE | `<home>/bronze/` | `fl ingest` | transform |
| SILVER | transform working views | transform | transform |
| GOLD | `<home>/gold/snowflake/` | transform | dashboard and MCP |

## Operational notes

- Snowflake Account Usage can have source-side latency. Re-run ingest after the
  accounting period settles when reconciling finalized charges.
- Give the configured role access to the cost views required by the connector.
- Use `fl ingest --full-refresh --connector Snowflake-prod --start YYYY-MM-DD` when
  a configuration change requires rebuilding retained Snowflake BRONZE history.
 - `fl sample` seeds Snowflake FOCUS cost records through the same shared BRONZE-to-GOLD
   path as a real ingest; it does not create a separate dashboard dataset.

For field-level categorization, see [FOCUS mapping](focus-mapping.md) and
[Snowflake service types](service-types.md).
