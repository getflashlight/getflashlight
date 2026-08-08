# Use Flashlight with agents

Flashlight exposes a streamable HTTP MCP server over the published GOLD metrics. The
server and dashboard intentionally share the same reader, metric catalog, and data files.

## Start the server

```bash
flashlight mcp serve
```

The default endpoint is `http://localhost:8002`. Configure `FLASHLIGHT_MCP_HOST` and
`FLASHLIGHT_MCP_PORT` for another private endpoint.

!!! danger "Do not expose the default server publicly"

    MCP has no built-in authentication in Flashlight. It exposes metric discovery and
    read-only SQL. Bind it to localhost/private networking or add authentication at a
    reverse proxy before sharing it.

## Recommended agent workflow

1. Call `list_metrics` to discover published views.
2. Call `describe_metric` to learn valid dimensions and measures.
3. Use `list_dimension_values` to discover filter values.
4. Use `query_metric` for bounded, structured results.
5. Use `run_sql` only for analysis that cannot be expressed through a metric view.

See [MCP tools reference](../reference/mcp-tools.md) for exact inputs and return shapes.

## Data availability

Run `flashlight ingest` or `flashlight sample` before starting MCP. If GOLD has not been
published, metric views are empty. MCP never triggers an ingest and never changes a source
or the local lake.
