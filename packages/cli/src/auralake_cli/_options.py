"""Shared CLI options and callbacks."""

from __future__ import annotations

from typing import Annotated

import typer
from auralake_shared.core.output import OutputFormat

# Server option — the CLI always talks to the server via HTTP
ServerOption = Annotated[
    str | None,
    typer.Option(
        "--server",
        "-s",
        envvar="AURALAKE_SERVER_URL",
        help="Server URL (default: http://localhost:8000, or AURALAKE_SERVER_URL).",
    ),
]

# Common option definitions for reuse across commands
ProviderOption = Annotated[
    str | None,
    typer.Option(
        "--provider", "-p", help="Lakehouse provider (databricks, snowflake, lake_formation)."
    ),
]

ConfigOption = Annotated[
    str | None,
    typer.Option("--config", "-c", help="Path to auralake.yaml config file."),
]

WorkspaceOption = Annotated[
    str | None,
    typer.Option("--workspace", "-w", help="Workspace name (from config)."),
]

DryRunOption = Annotated[
    bool,
    typer.Option("--dry-run", help="Show what would be done without making changes."),
]

ApplyOption = Annotated[
    bool,
    typer.Option("--apply", help="Apply changes directly (with confirmation)."),
]

AutoOption = Annotated[
    bool,
    typer.Option("--auto", help="Apply changes automatically within safety rails."),
]

PROption = Annotated[
    bool,
    typer.Option("--pr", help="Create a pull request with the changes."),
]

OutputOption = Annotated[
    OutputFormat,
    typer.Option("--output", "-o", help="Output format."),
]

VerboseOption = Annotated[
    bool,
    typer.Option("--verbose", "-v", help="Enable verbose/debug output."),
]

DaysOption = Annotated[
    int,
    typer.Option("--days", "-d", help="Lookback period in days."),
]
