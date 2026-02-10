"""Auth management commands (API key creation, login, credential storage)."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import typer
from auralake_shared.core.output import print_error, print_success

auth_app = typer.Typer(no_args_is_help=True)

CREDENTIALS_DIR = Path.home() / ".auralake"
CREDENTIALS_FILE = CREDENTIALS_DIR / "credentials.json"


# ---------------------------------------------------------------------------
# Credential file helpers
# ---------------------------------------------------------------------------


def load_credentials() -> dict:
    """Load credentials from ~/.auralake/credentials.json, or return empty dict."""
    if CREDENTIALS_FILE.exists():
        try:
            return json.loads(CREDENTIALS_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def save_credentials(server_url: str, api_key: str) -> None:
    """Write credentials to ~/.auralake/credentials.json with 0600 permissions."""
    CREDENTIALS_DIR.mkdir(parents=True, exist_ok=True)
    data = load_credentials()
    data["server_url"] = server_url
    data["api_key"] = api_key
    CREDENTIALS_FILE.write_text(json.dumps(data, indent=2) + "\n")
    CREDENTIALS_FILE.chmod(stat.S_IRUSR | stat.S_IWUSR)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def _check_backend_installed() -> None:
    try:
        import auralake_backend  # noqa: F401
    except ImportError:
        print_error(
            "'auralake-backend' is required for auth commands. "
            "Install with: pip install auralake-cli[db]"
        )
        raise typer.Exit(1) from None


@auth_app.command("login")
def login(
    server: str = typer.Option(
        "http://localhost:8000", "--server", "-s", help="Auralake server URL."
    ),
    key: str = typer.Option(..., "--key", "-k", help="API key (ak_...)."),
) -> None:
    """Store server URL and API key in ~/.auralake/credentials.json."""
    if not key.startswith("al_"):
        print_error("API key must start with 'al_'.")
        raise typer.Exit(1)

    save_credentials(server, key)
    print_success(f"Credentials saved to {CREDENTIALS_FILE}")
    typer.echo(f"  Server: {server}")
    typer.echo(f"  Key:    {key[:8]}...")


@auth_app.command("create-key")
def create_key(
    name: str = typer.Option(..., "--name", "-n", help="Human label for the key."),
) -> None:
    """Create a new API key (requires direct DB access). The raw key is printed once."""
    _check_backend_installed()

    db_url = os.environ.get("AURALAKE_DATABASE_URL")
    if not db_url:
        print_error("AURALAKE_DATABASE_URL environment variable is required.")
        raise typer.Exit(1)

    from auralake_backend.db.engine import get_session as _get_session
    from auralake_backend.db.engine import init_engine
    from auralake_backend.server.auth import create_api_key

    init_engine(db_url)
    with _get_session() as session:
        record, raw_key = create_api_key(session, name)

    print_success(f"API key created: {record.name} (id: {record.id})")
    typer.echo(f"\n  {raw_key}\n")
    typer.echo("Save this key — it cannot be retrieved again.")


@auth_app.command("list-keys")
def list_keys(
    server: str = typer.Option(None, "--server", "-s", help="Auralake server URL."),
    key: str = typer.Option(None, "--key", "-k", help="API key."),
) -> None:
    """List active API keys via the server API."""
    from auralake_cli.main import build_client

    client = build_client(server)
    try:
        data = client._get("/api/v1/auth/keys")
    except Exception as exc:
        print_error(f"Failed to list keys: {exc}")
        raise typer.Exit(1) from None

    if not data:
        typer.echo("No active API keys.")
        return

    for k in data:
        key_id = k.get("id", "?")
        name = k.get("name", "?")
        active = k.get("is_active", False)
        typer.echo(f"  {key_id}  {name}  active={active}")


@auth_app.command("setup")
def setup(
    server: str = typer.Option(
        "http://localhost:8000", "--server", "-s", help="Auralake server URL."
    ),
) -> None:
    """Interactive first-time setup wizard.

    1. Bootstraps the first API key (if none exist)
    2. Stores credentials locally
    3. Creates a Databricks provider connection
    4. Verifies connectivity
    """
    import httpx

    typer.echo("Auralake Setup Wizard")
    typer.echo("=" * 40)
    typer.echo()

    # Step 1 — Check health
    typer.echo(f"Connecting to {server} ...")
    try:
        resp = httpx.get(f"{server}/health", timeout=10.0)
        health = resp.json()
        typer.echo(f"  Server is up. Configured: {health.get('configured', False)}")
    except Exception as exc:
        print_error(f"Cannot reach server at {server}: {exc}")
        raise typer.Exit(1) from None

    # Step 2 — Enter API key (auto-created by the server on first startup)
    typer.echo()
    typer.echo("The server auto-creates the first API key on startup.")
    typer.echo(
        "Check the server logs for the key: docker compose logs backend | grep auto_bootstrap"
    )
    api_key = typer.prompt("  API key (al_...)")

    # Step 3 — Store credentials
    save_credentials(server, api_key)
    print_success(f"Credentials saved to {CREDENTIALS_FILE}")

    # Step 4 — Create Databricks connection
    typer.echo()
    if typer.confirm("Configure a Databricks connection now?", default=True):
        db_host = typer.prompt("  Databricks workspace URL (e.g. https://xxx.cloud.databricks.com)")
        db_token = typer.prompt("  Personal access token", hide_input=True)
        warehouse_id = typer.prompt(
            "  SQL warehouse ID (optional, for billing queries)", default=""
        )

        conn_body: dict = {
            "provider": "databricks",
            "name": "default",
            "is_default": True,
            "config": {"sql_warehouse_id": warehouse_id} if warehouse_id else {},
            "credentials": {"host": db_host, "token": db_token},
        }

        try:
            client = httpx.Client(
                base_url=server,
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=15.0,
            )
            resp = client.post("/api/v1/connections", json=conn_body)
            resp.raise_for_status()
            print_success("Databricks connection created.")
            client.close()
        except Exception as exc:
            print_error(f"Failed to create connection: {exc}")
            typer.echo("  You can create it later via: auralake connections create")

    # Step 5 — Verify
    typer.echo()
    typer.echo("Verifying setup ...")
    try:
        resp = httpx.get(
            f"{server}/health",
            timeout=10.0,
        )
        health = resp.json()
        configured = health.get("configured", False)
        if configured:
            print_success("Setup complete! Server is configured and ready.")
        else:
            typer.echo("  Server is running but not fully configured yet.")
            typer.echo("  Create a provider connection to complete setup.")
    except Exception:
        typer.echo("  Could not verify — server may need a restart.")

    typer.echo()
    typer.echo("Next steps:")
    typer.echo("  auralake cost breakdown     — view cost data")
    typer.echo("  auralake clusters analyze   — analyze cluster utilization")
    typer.echo("  auralake agent start        — start background collector")


@auth_app.command("revoke-key")
def revoke_key(
    key_id: str = typer.Argument(..., help="UUID of the key to revoke."),
    server: str = typer.Option(None, "--server", "-s", help="Auralake server URL."),
) -> None:
    """Revoke an API key via the server API."""
    import httpx

    from auralake_cli.main import build_client

    client = build_client(server)
    try:
        resp = client._client.delete(f"/api/v1/auth/keys/{key_id}")
        resp.raise_for_status()
        print_success(f"Key {key_id} revoked.")
    except httpx.HTTPStatusError as exc:
        print_error(f"Failed to revoke key: {exc.response.status_code} {exc.response.text}")
        raise typer.Exit(1) from None
