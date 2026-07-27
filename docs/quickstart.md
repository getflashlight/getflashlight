# Quick start

## Install

```bash
pip install getflashlight        # or: uv sync (from the repo)
```

**Cross-platform.** The lake home defaults to the OS user-data dir
(`platformdirs`) — `~/Library/Application Support/flashlight` on macOS,
`%LOCALAPPDATA%\flashlight\flashlight` on Windows, `~/.local/share/flashlight` on Linux
— or set `FLASHLIGHT_HOME` to override. Secrets load from a `.env` in the working
directory (real shell env wins).

## Try it with sample data (no config)

```bash
flashlight sample               # download the FinOps FOCUS sample + seed it
flashlight dashboard serve      # dashboard → http://127.0.0.1:8501
```

`flashlight sample [--rows 1000|10000]` loads the FinOps FOCUS sample CSV straight
into Parquet via a vectorized DuckDB projection — the zero-config way to see the
dashboard with real data.

## Connect your own sources

```bash
flashlight init                 # scaffold the lake home + a connections.yml
# edit connections.yml, put credentials in .env
flashlight ingest               # pull configured connectors → BRONZE, rebuild GOLD
```

## Serve

| Surface | Command | Where |
|---|---|---|
| Dashboard (humans) | `flashlight dashboard serve` | http://127.0.0.1:8501 |
| MCP server (agents) | `flashlight mcp serve` | http://localhost:8002 (streamable-http) |

Both are independent read-only processes over the published GOLD Parquet —
`ingest` is the only writer.

## Configuration

Everything is optional; defaults shown:

```bash
FLASHLIGHT_HOME=                          # lake root; default: platform user-data dir
FLASHLIGHT_BASE_CURRENCY=USD              # ingest asserts all rows match this
FLASHLIGHT_CONNECTIONS_PATH=              # default: <home>/config/connections.yml
FLASHLIGHT_PARQUET_COMPRESSION=zstd
FLASHLIGHT_PARQUET_COMPRESSION_LEVEL=3    # zstd 1-22
FLASHLIGHT_MCP_HOST=0.0.0.0
FLASHLIGHT_MCP_PORT=8002
FLASHLIGHT_DASHBOARD_HOST=127.0.0.1
FLASHLIGHT_DASHBOARD_PORT=8501
```
