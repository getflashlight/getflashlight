"""The unified `flashlight` CLI exposes only the operator surface (MCP-only)."""

import re

import pytest
from typer.testing import CliRunner

from flashlight.cli import app

runner = CliRunner()

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _plain(text: str) -> str:
    """Strip ANSI escapes from Typer/Rich help output.

    Rich wraps its help panel in bold/dim styling regardless of NO_COLOR (that
    env var only suppresses color, not other text attributes), which can split
    a hyphenated flag like "--dry-run" across escape codes on CI's terminal
    width detection even though CliRunner's captured stream isn't a real tty.
    """
    return _ANSI_RE.sub("", text)


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # Point the remembered-target state file at a fresh dir so persisted buckets
    # never leak between tests (or in from a real run).
    monkeypatch.setenv("FLASHLIGHT_STATE_DIR", str(tmp_path / "state"))
    # Rich (Typer's help renderer) colorizes output on some CI runners even
    # though CliRunner's captured stream isn't a real terminal, splitting
    # substrings like "--dry-run" across ANSI escape codes. NO_COLOR is the
    # standard opt-out Rich honors — force it for deterministic assertions.
    monkeypatch.setenv("NO_COLOR", "1")


def test_root_help_lists_operator_commands() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for token in ("init", "ingest", "transform", "mcp", "dashboard", "aws"):
        assert token in result.output


def test_no_rest_api_or_db_commands() -> None:
    # MCP-only: the REST API server, its client group, and the migrate command are gone.
    result = runner.invoke(app, ["--help"])
    assert " api " not in result.output
    assert "db" not in result.output


def test_dashboard_serve_help_offers_development_reload() -> None:
    result = runner.invoke(app, ["dashboard", "serve", "--help"])
    assert result.exit_code == 0
    assert "--dev" in _plain(result.output)


def test_dashboard_serve_passes_development_mode(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from flashlight.dashboard import launch

    seen: list[bool] = []
    monkeypatch.setattr(launch, "serve_dashboard", lambda *, dev: seen.append(dev))

    result = runner.invoke(app, ["dashboard", "serve", "--dev"])
    assert result.exit_code == 0
    assert seen == [True]


def test_dashboard_dev_mode_uses_the_flashlight_watcher(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from flashlight.dashboard import launch

    watched: list[bool] = []
    monkeypatch.setattr(launch, "_serve_with_reload", lambda: watched.append(True))

    launch.serve_dashboard(dev=True)
    assert watched == [True]


def test_sample_generates_cross_cloud_demo_data(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from flashlight import sample

    generated: list[str] = []
    monkeypatch.setattr(sample, "load_sample", lambda: generated.append("cross_cloud"))

    result = runner.invoke(app, ["sample"])

    assert result.exit_code == 0
    assert generated == ["cross_cloud"]


def test_sample_clean_removes_cross_cloud_demo_data(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from flashlight import sample

    cleaned: list[str] = []
    monkeypatch.setattr(sample, "cleanup", lambda: cleaned.append("cross_cloud"))

    result = runner.invoke(app, ["sample", "--clean"])

    assert result.exit_code == 0
    assert cleaned == ["cross_cloud"]


def test_aws_group_lists_all_subcommands() -> None:
    result = runner.invoke(app, ["aws", "--help"])
    assert result.exit_code == 0
    for cmd in (
        "create-export",
        "bucket-policy",
        "describe-export",
        "update-export",
        "delete-export",
    ):
        assert cmd in result.output


def test_aws_create_export_is_under_aws_group() -> None:
    result = runner.invoke(app, ["aws", "create-export", "--help"])
    assert result.exit_code == 0
    # Applies by default now; --dry-run is the opt-in preview flag.
    assert "--dry-run" in _plain(result.output)


def test_target_is_remembered_across_aws_commands(tmp_path) -> None:  # type: ignore[no-untyped-def]
    missing = tmp_path / "none.yml"
    # First run records bucket/prefix/region…
    first = runner.invoke(
        app,
        ["aws", "create-export", "--dry-run", "--yes", "--bucket", "remembered-bkt",
         "--prefix", "focus/export", "--s3-region", "us-west-2", "--connections", str(missing)],
    )
    assert first.exit_code == 0
    # …so a second run with no flags or input reuses them (--yes skips the prompts).
    second = runner.invoke(
        app, ["aws", "create-export", "--dry-run", "--yes", "--connections", str(missing)], input=""
    )
    assert second.exit_code == 0
    assert "remembered-bkt" in second.output
    assert "us-west-2" in second.output


def test_aws_create_export_dry_run_emits_request(tmp_path) -> None:  # type: ignore[no-untyped-def]
    missing = tmp_path / "none.yml"  # forces empty defaults, bucket from flag
    result = runner.invoke(
        app,
        ["aws", "create-export", "--dry-run", "--yes", "--bucket", "b", "--prefix", "p",
         "--s3-region", "us-west-1", "--connections", str(missing)],
    )
    assert result.exit_code == 0
    assert "FOCUS_1_2_AWS" in result.output
    assert "DRY RUN" in result.output


def test_aws_create_export_prompts_for_missing_inputs(tmp_path) -> None:  # type: ignore[no-untyped-def]
    # No --bucket and empty config → interactively prompt; then prefix/region
    # accept their defaults (blank lines).
    missing = tmp_path / "none.yml"
    result = runner.invoke(
        app,
        ["aws", "create-export", "--dry-run", "--connections", str(missing)],
        input="prompted-bucket\n\n\n",
    )
    assert result.exit_code == 0
    assert "prompted-bucket" in result.output
    assert "DRY RUN" in result.output


def test_aws_create_export_aborts_without_bucket_input(tmp_path) -> None:  # type: ignore[no-untyped-def]
    # Non-interactive (no input) → the bucket prompt hits EOF and aborts.
    missing = tmp_path / "none.yml"
    result = runner.invoke(
        app, ["aws", "create-export", "--dry-run", "--connections", str(missing)], input=""
    )
    assert result.exit_code != 0


def test_aws_delete_export_aborts_when_not_confirmed(tmp_path) -> None:  # type: ignore[no-untyped-def]
    # delete-export applies by default, so it must confirm (default No) before
    # touching AWS. Pressing Enter (or 'n') aborts before any API call.
    missing = tmp_path / "none.yml"
    result = runner.invoke(
        app, ["aws", "delete-export", "--connections", str(missing)], input="\n"
    )
    assert result.exit_code != 0
    assert "Aborted" in result.output


def test_cleanup_removes_all_lake_data(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("FLASHLIGHT_HOME", str(tmp_path / "home"))
    from flashlight.lake import paths

    paths.ensure_layout()
    (paths.bronze_dir() / "x_source_connector=demo").mkdir(parents=True)
    (paths.gold_dir() / "spend.parquet").write_text("data")
    (paths.runs_dir() / "run-demo.parquet").write_text("data")

    # --yes skips the confirm; data dirs are recreated empty afterward.
    result = runner.invoke(app, ["cleanup", "--yes"])
    assert result.exit_code == 0
    assert not any(paths.bronze_dir().iterdir())
    assert not any(paths.gold_dir().iterdir())
    assert not any(paths.runs_dir().iterdir())
    # Re-running is an idempotent no-op.
    again = runner.invoke(app, ["cleanup", "--yes"])
    assert again.exit_code == 0
    assert "Nothing to clean" in again.output


def test_cleanup_aborts_without_confirmation(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("FLASHLIGHT_HOME", str(tmp_path / "home"))
    from flashlight.lake import paths

    paths.ensure_layout()
    (paths.gold_dir() / "spend.parquet").write_text("data")

    result = runner.invoke(app, ["cleanup"], input="\n")
    assert result.exit_code != 0
    assert "Aborted" in result.output
    # Nothing was removed.
    assert (paths.gold_dir() / "spend.parquet").exists()
