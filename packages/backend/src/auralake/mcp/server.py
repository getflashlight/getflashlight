"""Auralake MCP server — lets agents discover and query FOCUS/TCO metrics.

Exposes the same GOLD views the Grafana dashboards use, so an agent and a chart
never disagree. All tools are read-only and scoped to the published views.

Tools:
  list_metrics            — catalogue of available metric views
  describe_metric(name)   — dimensions/measures/cost-metric for one view
  query_metric(name, ...) — rows from a GOLD view with optional ordering
  run_sql(sql, limit)     — ad-hoc read-only SELECT over gold/silver
"""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from auralake.core.settings import get_settings
from auralake.store.engine import init_engine
from auralake.store.query import QueryError, query_view, run_select
from auralake.transform.catalog import CATALOG, CATALOG_BY_NAME

_settings = get_settings()
mcp = FastMCP("auralake", host=_settings.mcp_host, port=_settings.mcp_port)


@mcp.tool()
def list_metrics() -> list[dict[str, Any]]:
    """List available metric views with their dimensions, measures, and cost metric."""
    return [
        {
            "name": v.name,
            "title": v.title,
            "description": v.description,
            "cost_metric": v.cost_metric.value,
            "dimensions": list(v.dimensions),
            "measures": list(v.measures),
        }
        for v in CATALOG
    ]


@mcp.tool()
def describe_metric(name: str) -> dict[str, Any]:
    """Describe a single metric view by name (e.g. 'gold.tco_summary_month')."""
    view = CATALOG_BY_NAME.get(name if name.startswith("gold.") else f"gold.{name}")
    if view is None:
        return {"error": f"Unknown metric: {name}", "available": list(CATALOG_BY_NAME)}
    return {
        "name": view.name,
        "title": view.title,
        "description": view.description,
        "cost_metric": view.cost_metric.value,
        "dimensions": list(view.dimensions),
        "measures": list(view.measures),
    }


@mcp.tool()
def query_metric(
    name: str, limit: int = 200, order_by: str | None = None, descending: bool = False
) -> dict[str, Any]:
    """Return rows from a GOLD metric view. `name` may omit the 'gold.' prefix."""
    full = name if name.startswith("gold.") else f"gold.{name}"
    try:
        rows = query_view(full, limit=limit, order_by=order_by, descending=descending)
    except QueryError as exc:
        return {"error": str(exc)}
    return {"view": full, "row_count": len(rows), "rows": rows}


@mcp.tool()
def run_sql(sql: str, limit: int = 200) -> dict[str, Any]:
    """Run a read-only SELECT over the gold/silver schemas. Mutations are rejected."""
    try:
        rows = run_select(sql, limit=limit)
    except QueryError as exc:
        return {"error": str(exc)}
    return {"row_count": len(rows), "rows": rows}


def serve_mcp() -> None:
    """Run the MCP server. Backs the ``auralake serve`` command.

    Applies pending migrations first (gated by AURALAKE_AUTO_MIGRATE) since this is
    now the primary long-running service — there is no separate migrate step.
    """
    from auralake.core.settings import get_settings

    if get_settings().auto_migrate:
        from auralake.store.migrate import upgrade_to_head

        upgrade_to_head()
    init_engine()
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    serve_mcp()
