"""atomic_publish: subdir-aware swap, first-publish mkdir, and stale-group prune."""

from __future__ import annotations

from pathlib import Path

from flashlight.lake.publish import atomic_publish


def _write(path: Path, text: str = "x") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def test_publish_lands_files_under_group_dirs(tmp_path) -> None:  # type: ignore[no-untyped-def]
    staging = tmp_path / "gold.staging"
    target = tmp_path / "gold"
    _write(staging / "aws" / "monthly_bill.parquet")
    _write(staging / "shared" / "tco_summary_month.parquet")

    # target/aws doesn't exist yet → first-publish mkdir path.
    published = atomic_publish(staging, target)

    assert published == 2
    assert (target / "aws" / "monthly_bill.parquet").exists()
    assert (target / "shared" / "tco_summary_month.parquet").exists()
    assert not staging.exists()  # staging tree removed


def test_publish_prunes_stale_group(tmp_path) -> None:  # type: ignore[no-untyped-def]
    target = tmp_path / "gold"
    # A provider that existed in a prior run.
    _write(target / "snowflake" / "monthly_bill.parquet")

    staging = tmp_path / "gold.staging"
    _write(staging / "aws" / "monthly_bill.parquet")
    _write(staging / "shared" / "tco_summary_month.parquet")

    atomic_publish(staging, target)

    # snowflake is gone from the new data → its group dir is pruned.
    assert not (target / "snowflake").exists()
    assert (target / "aws").exists()
    assert (target / "shared").exists()
