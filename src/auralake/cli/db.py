"""Database management commands."""
from __future__ import annotations

from pathlib import Path

import typer

from auralake.cli._options import ConfigOption, VerboseOption
from auralake.core.config import load_config
from auralake.core.logging import setup_logging
from auralake.core.output import print_error, print_success
from auralake.db.engine import create_all_tables, init_engine

db_app = typer.Typer(no_args_is_help=True)


@db_app.command("init")
def db_init(
    config: ConfigOption = None,
    verbose: VerboseOption = False,
) -> None:
    """Initialize the database and create all tables."""
    setup_logging(verbose)
    try:
        cfg = load_config(Path(config) if config else None)
        init_engine(cfg.database.url)
        create_all_tables()
        print_success("Database initialized successfully.")
    except Exception as exc:
        print_error(f"Database initialization failed: {exc}")
        raise typer.Exit(1) from None


@db_app.command("migrate")
def db_migrate(
    config: ConfigOption = None,
    verbose: VerboseOption = False,
) -> None:
    """Run pending Alembic migrations."""
    setup_logging(verbose)
    try:
        from alembic.config import Config as AlembicConfig
        from alembic import command as alembic_command

        cfg = load_config(Path(config) if config else None)
        alembic_cfg = AlembicConfig("alembic.ini")
        alembic_cfg.set_main_option("sqlalchemy.url", cfg.database.url)
        alembic_command.upgrade(alembic_cfg, "head")
        print_success("Migrations applied successfully.")
    except Exception as exc:
        print_error(f"Migration failed: {exc}")
        raise typer.Exit(1) from None


@db_app.command("status")
def db_status(
    config: ConfigOption = None,
    verbose: VerboseOption = False,
) -> None:
    """Show current migration status."""
    setup_logging(verbose)
    try:
        from alembic.config import Config as AlembicConfig
        from alembic import command as alembic_command

        cfg = load_config(Path(config) if config else None)
        alembic_cfg = AlembicConfig("alembic.ini")
        alembic_cfg.set_main_option("sqlalchemy.url", cfg.database.url)
        alembic_command.current(alembic_cfg, verbose=verbose)
    except Exception as exc:
        print_error(f"Failed to get migration status: {exc}")
        raise typer.Exit(1) from None
