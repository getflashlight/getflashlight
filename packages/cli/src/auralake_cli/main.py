"""Auralake CLI entry point."""

from __future__ import annotations

import typer
from rich.console import Console
from rich.table import Table

from auralake_cli.client import Client

app = typer.Typer(help="Auralake — FOCUS-based TCO spend visualization", no_args_is_help=True)
console = Console()


@app.command()
def health() -> None:
    """Check backend health."""
    console.print(Client().get("/health"))


@app.command()
def metrics() -> None:
    """List available GOLD metric views."""
    rows = Client().get("/api/v1/metrics")
    table = Table(title="Auralake metrics")
    table.add_column("name", style="cyan")
    table.add_column("title")
    table.add_column("cost metric", style="green")
    for r in rows:
        table.add_row(r["name"], r["title"], r["cost_metric"])
    console.print(table)


@app.command()
def metric(
    name: str,
    limit: int = typer.Option(50, help="Max rows"),
    order_by: str = typer.Option(None, help="Column to sort by"),
    desc: bool = typer.Option(False, help="Sort descending"),
) -> None:
    """Show rows from a single metric view (with or without the 'gold.' prefix)."""
    params: dict[str, object] = {"limit": limit, "descending": desc}
    if order_by:
        params["order_by"] = order_by
    data = Client().get(f"/api/v1/metrics/{name}", params=params)
    rows = data["rows"]
    if not rows:
        console.print("[yellow]No rows.[/]")
        return
    table = Table(title=data["view"])
    for col in rows[0]:
        table.add_column(str(col))
    for row in rows:
        table.add_row(*[str(v) for v in row.values()])
    console.print(table)


@app.command()
def ingest(
    start: str = typer.Option(None, help="ISO start date"),
    end: str = typer.Option(None, help="ISO end date"),
) -> None:
    """Trigger ingestion + view refresh on the backend."""
    body: dict[str, object] = {}
    if start:
        body["start"] = start
    if end:
        body["end"] = end
    console.print(Client().post("/api/v1/ingest", json=body))


if __name__ == "__main__":
    app()
