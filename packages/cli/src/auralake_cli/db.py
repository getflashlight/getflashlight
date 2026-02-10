"""Database management commands."""

from __future__ import annotations

import os
from pathlib import Path

import typer
from auralake_shared.core.logging import setup_logging
from auralake_shared.core.output import print_error, print_success

from auralake_cli._options import VerboseOption

db_app = typer.Typer(no_args_is_help=True)

_DEFAULT_DB_URL = "postgresql+psycopg://localhost:5432/auralake"


def _get_database_url() -> str:
    url = os.environ.get("AURALAKE_DATABASE_URL")
    if not url:
        print_error("AURALAKE_DATABASE_URL environment variable is required for db commands.")
        raise typer.Exit(1)
    return url


def _check_backend_installed() -> None:
    try:
        import auralake_backend  # noqa: F401
    except ImportError:
        print_error(
            "'auralake-backend' is required for db commands. "
            "Install with: pip install auralake-cli[db]"
        )
        raise typer.Exit(1) from None


def _run_alembic_upgrade(database_url: str) -> None:
    """Run ``alembic upgrade head`` against *database_url*."""
    import auralake_backend
    from alembic import command as alembic_command
    from alembic.config import Config as AlembicConfig

    # __file__ = .../packages/backend/src/auralake_backend/__init__.py
    # parents[2] = .../packages/backend/
    backend_pkg = Path(auralake_backend.__file__).resolve().parents[2]
    alembic_dir = backend_pkg / "alembic"
    alembic_ini = backend_pkg / "alembic.ini"

    cfg = AlembicConfig(str(alembic_ini))
    cfg.set_main_option("script_location", str(alembic_dir))
    cfg.set_main_option("sqlalchemy.url", database_url)
    alembic_command.upgrade(cfg, "head")


@db_app.command("init")
def db_init(
    verbose: VerboseOption = False,
) -> None:
    """Initialize the database (runs all alembic migrations)."""
    _check_backend_installed()
    setup_logging(verbose)
    try:
        db_url = _get_database_url()
        _run_alembic_upgrade(db_url)
        print_success("Database initialized successfully.")
    except typer.Exit:
        raise
    except Exception as exc:
        print_error(f"Database initialization failed: {exc}")
        raise typer.Exit(1) from None


@db_app.command("migrate")
def db_migrate(
    verbose: VerboseOption = False,
) -> None:
    """Run pending Alembic migrations."""
    _check_backend_installed()
    setup_logging(verbose)
    try:
        db_url = _get_database_url()
        _run_alembic_upgrade(db_url)
        print_success("Migrations applied successfully.")
    except typer.Exit:
        raise
    except Exception as exc:
        print_error(f"Migration failed: {exc}")
        raise typer.Exit(1) from None


@db_app.command("status")
def db_status(
    verbose: VerboseOption = False,
) -> None:
    """Show current migration status."""
    _check_backend_installed()
    setup_logging(verbose)
    try:
        import auralake_backend
        from alembic import command as alembic_command
        from alembic.config import Config as AlembicConfig

        db_url = _get_database_url()

        backend_pkg = Path(auralake_backend.__file__).resolve().parents[2]
        alembic_dir = backend_pkg / "alembic"
        alembic_ini = backend_pkg / "alembic.ini"

        alembic_cfg = AlembicConfig(str(alembic_ini))
        alembic_cfg.set_main_option("script_location", str(alembic_dir))
        alembic_cfg.set_main_option("sqlalchemy.url", db_url)
        alembic_command.current(alembic_cfg, verbose=verbose)
    except typer.Exit:
        raise
    except Exception as exc:
        print_error(f"Failed to get migration status: {exc}")
        raise typer.Exit(1) from None
