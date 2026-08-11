# Configure Flashlight

For normal source setup, use the dashboard's **Connections** page. It creates and updates
the non-secret connection definition, places entered credentials in the OS keychain, and
lets you test and sync a source without editing YAML. Configuration files under
`<FLASHLIGHT_HOME>/config/` remain the right interface for automated deployments and
advanced review.

## Configure sources in the dashboard

1. Open **Connections**.
2. Select **Add connection**, choose a connector type, and complete its form.
3. Select **Save**; use **Test connection** for a Redshift connection before its first sync.
4. Keep the connection enabled, choose a continuous six-month (or longer) range, then use
   **Sync now** to establish a useful analysis baseline.

The form writes the connection's identifiers and secret *references* to `connections.yml`.
The secret values are stored outside that file. The connector-specific guides document the
least-privilege permissions and the data each connector reads.

## Configuration files

| File | Purpose | Contains secrets? |
| --- | --- | --- |
| `connections.yml` | Enabled sources, endpoints, source options, credential variable names | No |
| `policies.yml` | Thresholds used by policy classifications | No |
| `assistant.yml` | Dashboard assistant provider and model selection | No API key |

Use an alternate connector file for a scheduled or isolated environment:

```bash
flashlight ingest --connections /srv/flashlight/prod-connections.yml
```

## Credentials

When entered through the dashboard, credentials are stored in the operating system's secure
credential store (for example, Keychain on macOS, Credential Manager on Windows, or the
desktop keyring available on Linux). Flashlight records only a non-secret reference in the
connection configuration, such as `token_env: DATABRICKS_TOKEN`.

For automation or a headless environment, set that named environment variable in the process
environment or your deployment's secret manager. A `.env` in the current working directory is
convenient for local development, but it is a development convenience—not the OS keychain—and
must never be committed.

## Platform settings

All platform settings use the `FLASHLIGHT_` prefix. The most useful are:

| Variable | Default | Effect |
| --- | --- | --- |
| `FLASHLIGHT_HOME` | OS user-data directory | Lake and config location |
| `FLASHLIGHT_BASE_CURRENCY` | `USD` | Expected currency at ingest |
| `FLASHLIGHT_INGEST_LOOKBACK_DAYS` | `35` | Default ingest window |
| `FLASHLIGHT_INGEST_MAX_WORKERS` | `3` | Maximum concurrent connector pulls |
| `FLASHLIGHT_DUCKDB_MEMORY_LIMIT` | `4GB` | Per-process DuckDB memory cap |
| `FLASHLIGHT_DUCKDB_TEMP_DIR` | under lake home | DuckDB spill location |
| `FLASHLIGHT_DASHBOARD_HOST` / `_PORT` | `127.0.0.1` / `8501` | Dashboard listener |
| `FLASHLIGHT_MCP_HOST` / `_PORT` | `0.0.0.0` / `8002` | MCP listener |

See the complete [configuration reference](../reference/configuration.md).

!!! warning "MCP is unauthenticated"

    The MCP server offers read-only data access, including ad-hoc SELECT queries, but it
    does not provide authentication. Bind it to a private interface or place it behind
    your own authenticated network boundary.
