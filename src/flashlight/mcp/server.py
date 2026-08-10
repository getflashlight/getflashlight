"""Flashlight MCP server — lets agents discover and query the FOCUS metrics.

Exposes the same GOLD views the dashboard uses, so an agent and a chart
never disagree. All tools are read-only and scoped to the published views.

Tools:
  list_metrics                    — catalogue of available metric views
  describe_metric(name)           — dimensions/measures/cost-metric for one view
  query_metric(name, ...)         — rows from a GOLD view, optionally filtered/ordered
  list_dimension_values(name, …)  — distinct values of one dimension (discover filters)
  list_optimization_rules()       — the full waste/optimization rule pool (dashboard parity)
  list_policy_rules()             — the full policy-compliance rule pool (dashboard parity)
  run_sql(sql, limit)             — ad-hoc read-only SELECT over gold/silver
"""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import MCPServer

from flashlight.core.settings import get_settings
from flashlight.efficiency.policy_config import referenced_thresholds
from flashlight.efficiency.policy_rules import POLICY_RULES
from flashlight.efficiency.waste_rules import WASTE_RULES
from flashlight.gold.reader import QueryError, distinct_values, query_view, run_select
from flashlight.transform.catalog import current_catalog, current_catalog_by_name

mcp = MCPServer("flashlight")


@mcp.tool()
def list_metrics() -> list[dict[str, Any]]:
    """List available metric views (group-qualified, e.g. 'aws.monthly_bill')."""
    return [
        {
            "name": v.name,
            "title": v.title,
            "description": v.description,
            "cost_metric": v.cost_metric.value if v.cost_metric else None,
            "dimensions": list(v.dimensions),
            "measures": list(v.measures),
        }
        for v in current_catalog()
    ]


@mcp.tool()
def describe_metric(name: str) -> dict[str, Any]:
    """Describe a metric view by its group-qualified name (e.g. 'aws.monthly_bill')."""
    catalog = current_catalog_by_name()
    view = catalog.get(name)
    if view is None:
        return {"error": f"Unknown metric: {name}", "available": list(catalog)}
    return {
        "name": view.name,
        "title": view.title,
        "description": view.description,
        "cost_metric": view.cost_metric.value if view.cost_metric else None,
        "dimensions": list(view.dimensions),
        "measures": list(view.measures),
    }


@mcp.tool()
def query_metric(
    name: str,
    limit: int = 200,
    order_by: str | list[str] | None = None,
    descending: bool = False,
    filters: dict[str, Any] | None = None,
    measures: list[str] | None = None,
) -> dict[str, Any]:
    """Return rows from a GOLD metric view. `name` is group-qualified, e.g. 'aws.monthly_bill'.

    `filters` is an equality map over the view's dimensions/measures, e.g.
    {"charge_month": "2026-07-01", "compute_family": "job"}. A value may also be
    a list to match any of several values, e.g. {"charge_month": ["2026-06-01",
    "2026-07-01"]} for a few specific months. Use list_dimension_values first if
    you don't already know the valid value (a tag key, a sku_id, ...).
    `order_by` is a single column name; a list is also accepted (only the first
    entry is used — sorting is single-column) since models commonly send one.
    `measures` narrows the returned columns to this subset of the view's measures
    (default: every measure) — pass exactly one (e.g. ["net_cost"]) to get a
    chartable one-dimension/one-measure result instead of every cost column.
    """
    if isinstance(order_by, list):
        order_by = order_by[0] if order_by else None
    try:
        rows = query_view(
            name,
            limit=limit,
            order_by=order_by,
            descending=descending,
            filters=filters,
            measures=measures,
        )
    except QueryError as exc:
        return {"error": str(exc), "available": list(current_catalog_by_name())}
    return {"view": name, "row_count": len(rows), "rows": rows}


@mcp.tool()
def list_dimension_values(name: str, dimension: str, limit: int = 500) -> dict[str, Any]:
    """Distinct values of one dimension on a metric view — discover valid filter values
    (tag keys/values, sku_id, compute_family, ...) before calling query_metric with a filter.
    """
    try:
        values = distinct_values(name, dimension, limit=limit)
    except QueryError as exc:
        return {"error": str(exc)}
    return {"view": name, "dimension": dimension, "values": values}


@mcp.tool()
def list_optimization_rules() -> list[dict[str, Any]]:
    """List the full waste/optimization rule pool — deterministic and identical whether
    read from here or the dashboard (plain SQL rules, no LLM/skill judgment).

    `status` is "active" (already classifying real data into efficiency.waste_record)
    or "blocked" (a documented pattern pending the telemetry named in `requires`).
    Reference guidance only; nothing here executes a change.
    """
    return [
        {
            "category": r.category,
            "lens": r.lens,
            "label": r.label,
            "remedy": r.remedy,
            "status": "active" if r.where_sql else "blocked",
            "requires": list(r.requires),
            "source": r.source,
        }
        for r in WASTE_RULES
    ]


@mcp.tool()
def list_policy_rules() -> list[dict[str, Any]]:
    """List the full policy-compliance rule pool — deterministic and identical whether
    read from here or the dashboard (plain SQL rules, no LLM/skill judgment).

    `status` is "active" (already classifying real data into gold.policy_record) or
    "blocked" (a documented check pending the telemetry named in `requires`). Unlike
    waste rules, an active policy rule emits a row for every applicable entity every
    month (compliant/non_compliant/not_applicable), not just violations.

    `thresholds` gives the numbers a rule is actually enforcing (e.g. the maximum
    auto-termination timeout that still counts as compliant). They come from the
    user's `config/policies.yml`, defaulting to Flashlight's efficient values.
    """
    return [
        {
            "category": r.category,
            "label": r.label,
            "remedy": r.remedy,
            "status": "active" if r.applies_sql else "blocked",
            "requires": list(r.requires),
            "source": r.source,
            "thresholds": referenced_thresholds(
                r.applies_sql, r.compliant_sql, r.not_applicable_sql, r.detail_sql
            ),
        }
        for r in POLICY_RULES
    ]


@mcp.tool()
def run_sql(sql: str, limit: int = 200) -> dict[str, Any]:
    """Run a read-only SELECT over the gold/silver schemas. Mutations are rejected."""
    try:
        rows = run_select(sql, limit=limit)
    except QueryError as exc:
        return {"error": str(exc)}
    return {"row_count": len(rows), "rows": rows}


def serve_mcp() -> None:
    """Run the MCP server. Backs ``flashlight mcp serve``.

    Reads the published GOLD Parquet read-only — no database or migrations. If
    GOLD hasn't been built yet (no ``flashlight ingest`` run), the metric views are
    simply empty.
    """
    settings = get_settings()
    mcp.run(transport="streamable-http", host=settings.mcp_host, port=settings.mcp_port)


if __name__ == "__main__":
    serve_mcp()
