"""The unified `auralake` CLI exposes only the operator surface (MCP-only)."""

import pytest
from auralake.cli import app
from typer.testing import CliRunner

runner = CliRunner()


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # Point the remembered-target state file at a fresh dir so persisted buckets
    # never leak between tests (or in from a real run).
    monkeypatch.setenv("AURALAKE_STATE_DIR", str(tmp_path / "state"))


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
    assert "--dry-run" in result.output


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
