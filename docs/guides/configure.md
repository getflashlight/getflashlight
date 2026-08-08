# Configure Flashlight

Flashlight keeps non-secret configuration under `<FLASHLIGHT_HOME>/config/`. Start with
`flashlight init`; it creates a commented `connections.yml` and policy/assistant defaults.

## Configuration files

| File | Purpose | Contains secrets? |
| --- | --- | --- |
| `connections.yml` | Enabled sources, endpoints, source options, credential variable names | No |
| `policies.yml` | Thresholds used by policy classifications | No |
| `assistant.yml` | Dashboard assistant provider and model selection | No API key |

Use an alternate connector file for isolated environments:

```bash
flashlight ingest --connections /srv/flashlight/prod-connections.yml
```

## Credentials

Connector configuration names the environment variable that holds each credential, such
as `token_env: DATABRICKS_TOKEN`. Flashlight resolves the value from the environment
or its supported keychain path. A `.env` in the current working directory is convenient
for local development; never commit it.

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
