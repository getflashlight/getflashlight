"""The unified `auralake` CLI exposes only the operator surface (MCP-only)."""

from auralake.cli import app
from typer.testing import CliRunner

runner = CliRunner()


def test_root_help_lists_operator_commands() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for token in ("serve", "ingest", "transform", "aws"):
        assert token in result.output


def test_no_rest_api_or_db_commands() -> None:
    # MCP-only: the REST API server, its client group, and the migrate command are gone.
    result = runner.invoke(app, ["--help"])
    assert " api " not in result.output
    assert "db" not in result.output


def test_aws_create_export_is_under_aws_group() -> None:
    result = runner.invoke(app, ["aws", "create-export", "--help"])
    assert result.exit_code == 0
    assert "--apply" in result.output


def test_aws_create_export_dry_run_emits_request(tmp_path) -> None:  # type: ignore[no-untyped-def]
    missing = tmp_path / "none.yml"  # forces empty defaults, bucket from flag
    result = runner.invoke(
        app,
        ["aws", "create-export", "--bucket", "b", "--prefix", "p",
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
        ["aws", "create-export", "--connections", str(missing)],
        input="prompted-bucket\n\n\n",
    )
    assert result.exit_code == 0
    assert "prompted-bucket" in result.output
    assert "DRY RUN" in result.output


def test_aws_create_export_aborts_without_bucket_input(tmp_path) -> None:  # type: ignore[no-untyped-def]
    # Non-interactive (no input) → the bucket prompt hits EOF and aborts.
    missing = tmp_path / "none.yml"
    result = runner.invoke(app, ["aws", "create-export", "--connections", str(missing)], input="")
    assert result.exit_code != 0
