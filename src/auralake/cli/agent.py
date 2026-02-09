"""Collector agent management CLI."""
from __future__ import annotations

from pathlib import Path

import typer

from auralake.cli._options import ConfigOption, VerboseOption
from auralake.core.output import print_error, print_success, print_warning, render_table, OutputFormat

agent_app = typer.Typer(no_args_is_help=True)


@agent_app.command("start")
def agent_start(
    config: ConfigOption = None,
    verbose: VerboseOption = False,
    daemon: bool = typer.Option(False, "--daemon", help="Run as background daemon."),
) -> None:
    """Start the collector agent."""
    from auralake.core.config import load_config
    from auralake.core.logging import setup_logging

    setup_logging(verbose)
    try:
        cfg = load_config(Path(config) if config else None)

        if daemon:
            print_warning("Daemon mode not yet implemented. Running in foreground.")

        from auralake.agent.collector import Collector
        collector = Collector(cfg)
        print_success(f"Starting collector (interval: {cfg.agent.interval_seconds}s)")
        collector.start()
    except KeyboardInterrupt:
        print_success("Collector stopped.")
    except Exception as exc:
        print_error(f"Collector failed: {exc}")
        raise typer.Exit(1) from None


@agent_app.command("stop")
def agent_stop(
    config: ConfigOption = None,
    verbose: VerboseOption = False,
) -> None:
    """Stop the running collector agent."""
    print_warning("Agent stop: send SIGTERM to the running collector process.")


@agent_app.command("status")
def agent_status(
    config: ConfigOption = None,
    verbose: VerboseOption = False,
) -> None:
    """Show collector agent status and statistics."""
    from auralake.core.config import load_config
    from auralake.core.logging import setup_logging

    setup_logging(verbose)
    try:
        cfg = load_config(Path(config) if config else None)
        from auralake.db.engine import init_engine, get_session
        from auralake.db.repositories import AgentStateRepository

        init_engine(cfg.database.url)
        with get_session() as session:
            repo = AgentStateRepository(session)
            state = repo.get_or_create("default")
            render_table(
                "Agent Status",
                ["Property", "Value"],
                [
                    ["Status", state.status],
                    ["Queries Collected", str(state.queries_collected)],
                    ["Plans Collected", str(state.plans_collected)],
                    ["Last Run", str(state.last_run_at) if state.last_run_at else "Never"],
                ],
                OutputFormat.TABLE,
            )
    except Exception as exc:
        print_error(f"Failed to get agent status: {exc}")
        raise typer.Exit(1) from None
