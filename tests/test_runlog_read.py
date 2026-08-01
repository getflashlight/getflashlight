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
