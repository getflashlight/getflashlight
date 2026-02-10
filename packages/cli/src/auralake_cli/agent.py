"""Collector agent management CLI."""

from __future__ import annotations

import typer

from auralake_cli._options import ServerOption
from auralake_cli._rendering import OutputFormat, print_error, print_success, render_table

agent_app = typer.Typer(no_args_is_help=True)


@agent_app.command("status")
def agent_status(
    connection_id: str | None = typer.Argument(default=None, help="Connection ID (optional)"),
    server: ServerOption = None,
) -> None:
    """Show collector agent status."""
    from auralake_cli.main import build_client

    client = build_client(server)
    try:
        if connection_id:
            data = client.agent_status_connection(connection_id)
        else:
            data = client.agent_status()
        render_table(
            "Agent Status",
            ["Property", "Value"],
            [[k, str(v)] for k, v in data.items()],
            OutputFormat.TABLE,
        )
    except Exception as exc:
        print_error(f"Failed to get agent status: {exc}")
        raise typer.Exit(1) from None


@agent_app.command("collect")
def agent_collect(
    connection_id: str = typer.Argument(help="Connection ID to collect data from"),
    server: ServerOption = None,
) -> None:
    """Trigger data collection for a connection."""
    from auralake_cli.main import build_client

    client = build_client(server)
    try:
        data = client.agent_collect(connection_id)
        print_success(f"Collection started: run_id={data.get('run_id', 'unknown')}")
    except Exception as exc:
        print_error(f"Failed to start collection: {exc}")
        raise typer.Exit(1) from None


@agent_app.command("cancel")
def agent_cancel(
    connection_id: str = typer.Argument(help="Connection ID to cancel collection for"),
    server: ServerOption = None,
) -> None:
    """Cancel a running collection."""
    from auralake_cli.main import build_client

    client = build_client(server)
    try:
        client.agent_cancel(connection_id)
        print_success("Collection cancelled.")
    except Exception as exc:
        print_error(f"Failed to cancel collection: {exc}")
        raise typer.Exit(1) from None


@agent_app.command("retry")
def agent_retry(
    connection_id: str = typer.Argument(help="Connection ID"),
    worker_name: str = typer.Argument(help="Worker name to retry"),
    server: ServerOption = None,
) -> None:
    """Retry a failed worker for a connection."""
    from auralake_cli.main import build_client

    client = build_client(server)
    try:
        data = client.agent_retry(connection_id, worker_name)
        print_success(f"Retry started: {data}")
    except Exception as exc:
        print_error(f"Failed to retry worker: {exc}")
        raise typer.Exit(1) from None


@agent_app.command("history")
def agent_history(
    server: ServerOption = None,
) -> None:
    """Show collection run history."""
    from auralake_cli.main import build_client

    client = build_client(server)
    try:
        runs = client.agent_history()
        if not runs:
            print_success("No collection runs found.")
            return
        render_table(
            "Collection History",
            ["Run ID", "Connection", "Status", "Started", "Completed"],
            [
                [
                    str(r.get("id", "")),
                    str(r.get("connection_id", "")),
                    str(r.get("status", "")),
                    str(r.get("started_at", "")),
                    str(r.get("completed_at", "")),
                ]
                for r in runs
            ],
            OutputFormat.TABLE,
        )
    except Exception as exc:
        print_error(f"Failed to get collection history: {exc}")
        raise typer.Exit(1) from None
