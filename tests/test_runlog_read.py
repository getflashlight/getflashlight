from __future__ import annotations

from datetime import UTC, datetime

import pytest

from flashlight.core.settings import get_settings
from flashlight.lake import runlog


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):  # type: ignore[no-untyped-def]
    monkeypatch.setenv("FLASHLIGHT_HOME", str(tmp_path))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _ts(minute: int) -> datetime:
    return datetime(2026, 1, 1, 0, minute, tzinfo=UTC)


def test_read_runs_empty_when_nothing_logged() -> None:
    df = runlog.read_runs()
    assert df.empty
    assert list(df.columns) == [f.name for f in runlog.RUN_SCHEMA]


def test_read_runs_sorted_newest_first_and_limited() -> None:
    runlog.record_run(
        run_id="run-1", connector="aws_focus", status="success", rows=10,
        started_at=_ts(0), finished_at=_ts(1),
    )
    runlog.record_run(
        run_id="run-2", connector="databricks", status="failed", rows=0,
        started_at=_ts(2), finished_at=_ts(3), detail="boom",
    )

    df = runlog.read_runs(limit=1)
    assert len(df) == 1
    assert df.iloc[0]["connector"] == "databricks"
    assert df.iloc[0]["status"] == "failed"
    assert df.iloc[0]["detail"] == "boom"

    df_all = runlog.read_runs()
    assert list(df_all["connector"]) == ["databricks", "aws_focus"]


def test_read_run_groups_empty_when_nothing_logged() -> None:
    df = runlog.read_run_groups()
    assert df.empty
    assert list(df.columns) == runlog.GROUP_COLUMNS


def test_read_run_groups_aggregates_one_sync_from_its_connector_rows() -> None:
    """Three connectors sharing one run_id (one run_ingest() call, per
    runner.py::run_connector) collapse into a single group: summed rows, the
    connector count, and the [min started, max finished] span across all three.
    """
    runlog.record_run(
        run_id="sync-1", connector="aws_focus", status="success", rows=10,
        started_at=_ts(0), finished_at=_ts(2),
    )
    runlog.record_run(
        run_id="sync-1", connector="databricks", status="success", rows=90,
        started_at=_ts(1), finished_at=_ts(5),
    )
    runlog.record_run(
        run_id="sync-1", connector="redshift", status="success", rows=0,
        started_at=_ts(1), finished_at=_ts(3),
    )

    df = runlog.read_run_groups()
    assert len(df) == 1
    row = df.iloc[0]
    assert row["run_id"] == "sync-1"
    assert row["rows"] == 100
    assert row["connectors"] == 3
    assert row["status"] == "success"
    assert row["started_at"] == _ts(0)  # earliest of the three
    assert row["finished_at"] == _ts(5)  # latest of the three


def test_read_run_groups_status_is_worst_of_its_connectors() -> None:
    """Any connector failing in a sync makes the whole group read "failed" —
    even though most of it succeeded, an operator glancing at history needs to
    know something in this sync needs attention.
    """
    runlog.record_run(
        run_id="sync-1", connector="aws_focus", status="success", rows=10,
        started_at=_ts(0), finished_at=_ts(1),
    )
    runlog.record_run(
        run_id="sync-1", connector="databricks", status="failed", rows=0,
        started_at=_ts(0), finished_at=_ts(1), detail="expired token",
    )

    df = runlog.read_run_groups()
    assert df.iloc[0]["status"] == "failed"


def test_read_run_groups_sorted_newest_first_and_limited() -> None:
    runlog.record_run(
        run_id="sync-old", connector="aws_focus", status="success", rows=1,
        started_at=_ts(0), finished_at=_ts(1),
    )
    runlog.record_run(
        run_id="sync-new", connector="aws_focus", status="success", rows=2,
        started_at=_ts(2), finished_at=_ts(3),
    )

    df = runlog.read_run_groups(limit=1)
    assert len(df) == 1
    assert df.iloc[0]["run_id"] == "sync-new"

    df_all = runlog.read_run_groups()
    assert list(df_all["run_id"]) == ["sync-new", "sync-old"]


def test_read_run_groups_degrades_to_one_group_per_row_for_pre_shared_run_ids() -> None:
    """Runs logged before run_id became shared-per-sync (each connector had its
    own unique id) must not accidentally merge into one group just because
    read_run_groups() groups by run_id — each old row is genuinely its own group.
    """
    runlog.record_run(
        run_id="old-unique-1", connector="aws_focus", status="success", rows=5,
        started_at=_ts(0), finished_at=_ts(1),
    )
    runlog.record_run(
        run_id="old-unique-2", connector="databricks", status="success", rows=7,
        started_at=_ts(0), finished_at=_ts(1),
    )

    df = runlog.read_run_groups()
    assert len(df) == 2
    assert set(df["connectors"]) == {1}
