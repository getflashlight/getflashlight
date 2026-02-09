"""Rich-based output formatting for the auralake CLI.

Provides helpers to render tables, recommendations, and status messages in
multiple output formats (table, JSON, CSV).
"""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Sequence
from enum import StrEnum
from typing import Any

from rich.console import Console
from rich.prompt import Confirm
from rich.table import Table

console = Console()
error_console = Console(stderr=True)


class OutputFormat(StrEnum):
    """Supported output serialisation formats."""

    TABLE = "table"
    JSON = "json"
    CSV = "csv"


# ---------------------------------------------------------------------------
# Table rendering
# ---------------------------------------------------------------------------

def render_table(
    title: str,
    columns: Sequence[str],
    rows: Sequence[Sequence[Any]],
    output_format: OutputFormat = OutputFormat.TABLE,
) -> None:
    """Render tabular data in the requested format.

    Parameters
    ----------
    title:
        Human-readable title displayed above the table (TABLE mode only).
    columns:
        Column header names.
    rows:
        Each inner sequence is one row whose length must match *columns*.
    output_format:
        Serialisation format.
    """
    if output_format is OutputFormat.JSON:
        records = [dict(zip(columns, row)) for row in rows]
        console.print_json(json.dumps(records, default=str))
        return

    if output_format is OutputFormat.CSV:
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(columns)
        writer.writerows(rows)
        console.print(buf.getvalue(), highlight=False)
        return

    # Default: rich table
    table = Table(title=title, show_lines=True)
    for col in columns:
        table.add_column(col)
    for row in rows:
        table.add_row(*(str(cell) for cell in row))
    console.print(table)


# ---------------------------------------------------------------------------
# Recommendation rendering
# ---------------------------------------------------------------------------

def render_recommendations(
    recs: Sequence[dict[str, Any]],
    output_format: OutputFormat = OutputFormat.TABLE,
) -> None:
    """Render a list of cost-optimisation recommendations.

    Each recommendation dict is expected to have at least the keys
    ``resource``, ``recommendation``, ``estimated_savings``, and ``priority``.

    Parameters
    ----------
    recs:
        Sequence of recommendation dicts.
    output_format:
        Serialisation format.
    """
    if not recs:
        print_warning("No recommendations found.")
        return

    columns = ["Resource", "Recommendation", "Est. Savings", "Priority"]
    rows = [
        [
            r.get("resource", ""),
            r.get("recommendation", ""),
            r.get("estimated_savings", ""),
            r.get("priority", ""),
        ]
        for r in recs
    ]
    render_table("Recommendations", columns, rows, output_format)


# ---------------------------------------------------------------------------
# Interactive prompts
# ---------------------------------------------------------------------------

def confirm_action(message: str) -> bool:
    """Prompt the user for confirmation using Rich.

    Returns *True* when the user answers yes, *False* otherwise.
    """
    return Confirm.ask(message)


# ---------------------------------------------------------------------------
# Status message helpers
# ---------------------------------------------------------------------------

def print_success(message: str) -> None:
    """Print a success message in green."""
    console.print(f"[bold green]SUCCESS[/bold green] {message}")


def print_warning(message: str) -> None:
    """Print a warning message in yellow."""
    error_console.print(f"[bold yellow]WARNING[/bold yellow] {message}")


def print_error(message: str) -> None:
    """Print an error message in red."""
    error_console.print(f"[bold red]ERROR[/bold red] {message}")
