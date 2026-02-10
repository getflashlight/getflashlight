"""Provider connection management commands."""

from __future__ import annotations

import json
from typing import Annotated

import typer

from auralake_cli._options import OutputOption, ServerOption
from auralake_cli._rendering import (
    OutputFormat,
    confirm_action,
    print_error,
    print_success,
    render_table,
)

connections_app = typer.Typer(no_args_is_help=True)


@connections_app.command("list")
def list_connections(
    server: ServerOption = None,
    output: OutputOption = OutputFormat.TABLE,
) -> None:
    """List all provider connections."""
    from auralake_cli.main import build_client

    client = build_client(server)
    connections = client.connections_list()
    rows = []
    for c in connections:
        rows.append(
            [
                c.get("id", ""),
                c.get("provider", ""),
                c.get("name", ""),
                "Yes" if c.get("is_default") else "No",
                "Yes" if c.get("has_credentials") else "No",
            ]
        )
    render_table(
        "Connections",
        ["ID", "Provider", "Name", "Default", "Credentials"],
        rows,
        output,
    )


@connections_app.command("show")
def show_connection(
    connection_id: Annotated[str, typer.Argument(help="Connection ID to show.")],
    server: ServerOption = None,
    output: OutputOption = OutputFormat.TABLE,
) -> None:
    """Show details for a specific connection."""
    from auralake_cli.main import build_client

    client = build_client(server)
    c = client.connections_get(connection_id)
    rows = [
        ["ID", str(c.get("id", ""))],
        ["Provider", c.get("provider", "")],
        ["Name", c.get("name", "")],
        ["Default", "Yes" if c.get("is_default") else "No"],
        ["Has Credentials", "Yes" if c.get("has_credentials") else "No"],
        ["Config", json.dumps(c.get("config", {}), indent=2)],
        ["Created At", c.get("created_at", "")],
        ["Updated At", c.get("updated_at", "")],
    ]
    render_table(
        f"Connection: {c.get('name', connection_id)}",
        ["Property", "Value"],
        rows,
        output,
    )


@connections_app.command("create")
def create_connection(
    server: ServerOption = None,
    provider: Annotated[
        str,
        typer.Option("--provider", "-p", help="Provider type (e.g. databricks, snowflake)."),
    ] = "databricks",
    name: Annotated[
        str,
        typer.Option("--name", "-n", help="Connection name."),
    ] = "default",
    default: Annotated[
        bool,
        typer.Option("--default/--no-default", help="Set as default connection."),
    ] = False,
    config: Annotated[
        str | None,
        typer.Option("--config", "-c", help="Connection config as JSON string."),
    ] = None,
    credentials: Annotated[
        str | None,
        typer.Option(
            "--credentials",
            help="Credentials as JSON string. If omitted, you will be prompted.",
        ),
    ] = None,
    output: OutputOption = OutputFormat.TABLE,
) -> None:
    """Create a new provider connection."""
    from auralake_cli.main import build_client

    config_dict = json.loads(config) if config else {}

    creds_dict = None
    if credentials is not None:
        creds_dict = json.loads(credentials)
    else:
        raw = typer.prompt(
            "Credentials JSON (or press Enter to skip)",
            default="",
            hide_input=True,
        )
        if raw:
            creds_dict = json.loads(raw)

    client = build_client(server)
    c = client.connections_create(
        provider=provider,
        name=name,
        is_default=default,
        config=config_dict,
        credentials=creds_dict,
    )
    print_success(f"Connection created: {c['id']}")
    rows = [
        ["ID", str(c.get("id", ""))],
        ["Provider", c.get("provider", "")],
        ["Name", c.get("name", "")],
        ["Default", "Yes" if c.get("is_default") else "No"],
        ["Has Credentials", "Yes" if c.get("has_credentials") else "No"],
    ]
    render_table("New Connection", ["Property", "Value"], rows, output)


@connections_app.command("update")
def update_connection(
    connection_id: Annotated[str, typer.Argument(help="Connection ID to update.")],
    server: ServerOption = None,
    default: Annotated[
        bool | None,
        typer.Option("--default/--no-default", help="Set or unset as default connection."),
    ] = None,
    config: Annotated[
        str | None,
        typer.Option("--config", "-c", help="Updated config as JSON string."),
    ] = None,
    credentials: Annotated[
        str | None,
        typer.Option(
            "--credentials",
            help="Updated credentials as JSON string.",
        ),
    ] = None,
    output: OutputOption = OutputFormat.TABLE,
) -> None:
    """Update an existing provider connection."""
    from auralake_cli.main import build_client

    config_dict = json.loads(config) if config else None
    creds_dict = json.loads(credentials) if credentials else None

    client = build_client(server)
    c = client.connections_update(
        connection_id,
        is_default=default,
        config=config_dict,
        credentials=creds_dict,
    )
    print_success(f"Connection updated: {c['id']}")
    rows = [
        ["ID", str(c.get("id", ""))],
        ["Provider", c.get("provider", "")],
        ["Name", c.get("name", "")],
        ["Default", "Yes" if c.get("is_default") else "No"],
        ["Has Credentials", "Yes" if c.get("has_credentials") else "No"],
    ]
    render_table("Updated Connection", ["Property", "Value"], rows, output)


@connections_app.command("delete")
def delete_connection(
    connection_id: Annotated[str, typer.Argument(help="Connection ID to delete.")],
    server: ServerOption = None,
    yes: Annotated[
        bool,
        typer.Option("--yes", "-y", help="Skip confirmation prompt."),
    ] = False,
) -> None:
    """Delete a provider connection."""
    from auralake_cli.main import build_client

    if not yes and not confirm_action(f"Delete connection {connection_id}?"):
        print_error("Aborted.")
        raise typer.Exit(code=1)

    client = build_client(server)
    client.connections_delete(connection_id)
    print_success(f"Connection {connection_id} deleted.")
