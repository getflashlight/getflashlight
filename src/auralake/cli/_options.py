"""Shared CLI options and callbacks."""
from __future__ import annotations

from typing import Annotated, Optional

import typer

from auralake.core.output import OutputFormat

# Common option definitions for reuse across commands
ProviderOption = Annotated[
    Optional[str],
    typer.Option("--provider", "-p", help="Lakehouse provider (databricks, snowflake, lake_formation)."),
]

ConfigOption = Annotated[
    Optional[str],
    typer.Option("--config", "-c", help="Path to auralake.yaml config file."),
]

WorkspaceOption = Annotated[
    Optional[str],
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
