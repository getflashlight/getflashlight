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
BRONZE Parquet. The normal transform then publishes Snowflake GOLD views. Separately, it
dumps ACCOUNT_USAGE tables into `<home>/account_usage/` for Visibility/LeaderBoard.
The dashboard and MCP never open a Snowflake connection at request time — ingest is the
only writer that talks to Snowflake.

## Dashboard

Open `/snowflake` after a successful ingest (or `flashlight sample`). It is the
Visibility/LeaderBoard surface over local `account_usage/` Parquet. Before the first
successful ACCOUNT_USAGE pull it shows an empty state. FOCUS `gold/snowflake/` still
exists for MCP and shared GOLD consumers; the `/snowflake` page itself is Visibility.

## Data layout

| Layer | Location | Writer | Reader |
|---|---|---|---|
| BRONZE | `<home>/bronze/` | `fl ingest` | transform |
| ACCOUNT_USAGE | `<home>/account_usage/` | `fl ingest` / `fl sample` | Visibility dashboard |
| SILVER | transform working views | transform | transform |
| GOLD | `<home>/gold/snowflake/` | transform | MCP / GOLD consumers |

## Operational notes

- Snowflake Account Usage can have source-side latency. Re-run ingest after the
  accounting period settles when reconciling finalized charges.
- Give the configured role access to the cost views and ACCOUNT_USAGE views required
  by the connector.
- Use `fl ingest --full-refresh --connector Snowflake-prod --start YYYY-MM-DD` when
  a configuration change requires rebuilding retained Snowflake BRONZE history.
- `fl sample` installs synthetic ACCOUNT_USAGE into the same `account_usage/` lake
  layout a live ingest uses.

For field-level categorization, see [FOCUS mapping](focus-mapping.md) and
[Snowflake service types](service-types.md).
