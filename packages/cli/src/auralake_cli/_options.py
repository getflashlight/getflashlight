"""Shared CLI options and callbacks."""

from __future__ import annotations

from typing import Annotated

import typer

from auralake_cli._rendering import OutputFormat

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

OutputOption = Annotated[
    OutputFormat,
    typer.Option("--output", "-o", help="Output format."),
]

VerboseOption = Annotated[
    bool,
    typer.Option("--verbose", "-v", help="Enable verbose/debug output."),
]
