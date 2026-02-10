"""Aura Lake CLI — lakehouse cost optimization tool."""

from __future__ import annotations

import os

import typer

from auralake_cli.agent import agent_app
from auralake_cli.auth import auth_app
from auralake_cli.client import AuralakeClient
from auralake_cli.connections import connections_app
from auralake_cli.db import db_app

app = typer.Typer(
    name="auralake",
    help="Aura Lake — multi-platform lakehouse cost optimization CLI.",
    no_args_is_help=True,
    rich_markup_mode="rich",
)

DEFAULT_SERVER_URL = "http://localhost:8000"


def build_client(server: str | None = None) -> AuralakeClient:
    """Build an HTTP client pointing at the Auralake server.

    Resolution priority (highest to lowest):
    1. Explicit ``server`` argument (from CLI flags)
    2. ``AURALAKE_SERVER_URL`` / ``AURALAKE_API_KEY`` environment variables
    3. ``~/.auralake/credentials.json`` file
    """
    from auralake_cli.auth import load_credentials

    creds = load_credentials()

    url = (
        server
        or os.environ.get("AURALAKE_SERVER_URL")
        or creds.get("server_url")
        or DEFAULT_SERVER_URL
    )
    api_key = os.environ.get("AURALAKE_API_KEY") or creds.get("api_key")
    return AuralakeClient(base_url=url, api_key=api_key)


app.add_typer(agent_app, name="agent", help="Collector agent management.")
app.add_typer(db_app, name="db", help="Database management.")
app.add_typer(auth_app, name="auth", help="Authentication and API key management.")
app.add_typer(connections_app, name="connections", help="Provider connection management.")


@app.callback()
def main_callback() -> None:
    """Aura Lake — multi-platform lakehouse cost optimization."""
