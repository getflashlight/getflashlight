"""Collector agent management CLI."""

from __future__ import annotations

import typer
from auralake_shared.core.output import OutputFormat, print_error, print_success, render_table

from auralake_cli._options import ServerOption

agent_app = typer.Typer(no_args_is_help=True)


@agent_app.command("start")
def agent_start(
    server: ServerOption = None,
) -> None:
    """Start the collector agent."""
    from auralake_cli.main import build_client

    client = build_client(server)
    try:
        client.agent_start()
        print_success("Agent start requested.")
    except Exception as exc:
        print_error(f"Failed to start agent: {exc}")
        raise typer.Exit(1) from None


@agent_app.command("stop")
def agent_stop(
    server: ServerOption = None,
) -> None:
    """Stop the running collector agent."""
    from auralake_cli.main import build_client

    client = build_client(server)
    try:
        client.agent_stop()
        print_success("Agent stop requested.")
    except Exception as exc:
        print_error(f"Failed to stop agent: {exc}")
        raise typer.Exit(1) from None


@agent_app.command("status")
def agent_status(
    server: ServerOption = None,
) -> None:
    """Show collector agent status and statistics."""
    from auralake_cli.main import build_client

    client = build_client(server)
    try:
        data = client.agent_status()
        render_table(
            "Agent Status",
            ["Property", "Value"],
            [
                ["Status", str(data.get("status", "unknown"))],
                ["Queries Collected", str(data.get("queries_collected", 0))],
                ["Plans Collected", str(data.get("plans_collected", 0))],
                ["Last Run", str(data.get("last_run_at", "Never"))],
            ],
            OutputFormat.TABLE,
        )
    except Exception as exc:
        print_error(f"Failed to get agent status: {exc}")
        raise typer.Exit(1) from None
