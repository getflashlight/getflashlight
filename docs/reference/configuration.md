# Configuration reference

## Connection configuration

`connections.yml` has one `connectors` list. Each connector must declare `type` and
`enabled`; `name` is recommended and must be unique when more than one connection of a
type is configured.

| Type | Required identifiers | Credential references |
| --- | --- | --- |
| `aws_focus` | S3 bucket/prefix for export, or Cost Explorer mode | `access_key_env`, `secret_key_env`, or normal AWS credential chain |
| `databricks` | `host`; optional `sql_warehouse_id` | `token_env` |
| `redshift` | provisioned `cluster_identifier`; `database` | AWS variables/profile, Data API secret, or optional database/bastion fields |

See the generated `connections.yml` for the full commented schema and source-specific
examples. It is intentionally the closest documentation to the versioned validation
model.

## Environment variables

| Setting | Environment variable | Default |
| --- | --- | --- |
| Lake home | `FLASHLIGHT_HOME` | Platform user-data directory |
| Base currency | `FLASHLIGHT_BASE_CURRENCY` | `USD` |
| Connections path | `FLASHLIGHT_CONNECTIONS_PATH` | `connections.yml` under config |
| Parquet compression | `FLASHLIGHT_PARQUET_COMPRESSION` | `zstd` |
| Compression level | `FLASHLIGHT_PARQUET_COMPRESSION_LEVEL` | `3` |
| Ingest workers | `FLASHLIGHT_INGEST_MAX_WORKERS` | `3` |
| Ingest lookback | `FLASHLIGHT_INGEST_LOOKBACK_DAYS` | `35` |
| DuckDB memory limit | `FLASHLIGHT_DUCKDB_MEMORY_LIMIT` | `4GB` |
| DuckDB temporary directory | `FLASHLIGHT_DUCKDB_TEMP_DIR` | Lake-home temp directory |
| MCP host / port | `FLASHLIGHT_MCP_HOST`, `FLASHLIGHT_MCP_PORT` | `0.0.0.0`, `8002` |
| Dashboard host / port | `FLASHLIGHT_DASHBOARD_HOST`, `FLASHLIGHT_DASHBOARD_PORT` | `127.0.0.1`, `8501` |
| Static docs directory | `FLASHLIGHT_DOCS_DIR` | unset |

Assistant provider settings use `FLASHLIGHT_ASSISTANT_PROVIDER`,
`FLASHLIGHT_ASSISTANT_MODEL`, and `FLASHLIGHT_ASSISTANT_BASE_URL`. The assistant API key
is intentionally resolved separately, not from the shared settings model.
