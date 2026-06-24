"""Orchestration: a failing connector is isolated but the run still surfaces it."""

from __future__ import annotations

import pytest

from auralake.core.exceptions import IngestError
from auralake.ingest import runner
from auralake.ingest.runner import ConnectorOutcome, run_ingest


def _stub(monkeypatch, outcomes: list[ConnectorOutcome], built: list[bool]) -> None:  # type: ignore[no-untyped-def]
    # One config per outcome; build_connector/run_connector are replaced so no real
    # S3/Parquet work happens — we exercise only the aggregate-and-raise logic.
    monkeypatch.setattr(runner, "load_connections", lambda _c: [object()] * len(outcomes))
    monkeypatch.setattr(runner, "build_connector", lambda c: c)
    seq = iter(outcomes)
    monkeypatch.setattr(runner, "run_connector", lambda _conn, _w: next(seq))

    def _build_gold() -> int:
        built.append(True)
        return 11

    monkeypatch.setattr(runner, "build_gold", _build_gold)


def test_partial_failure_rebuilds_gold_then_raises(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    built: list[bool] = []
    _stub(
        monkeypatch,
        [
            ConnectorOutcome(name="databricks", rows=42, ok=True),
            ConnectorOutcome(name="aws_focus", rows=0, ok=False, detail="S3 list failed"),
        ],
        built,
    )
    with pytest.raises(IngestError) as exc:
        run_ingest()
    # The failed connector is named, and GOLD was still rebuilt from what landed.
    assert exc.value.failed == ["aws_focus"]
    assert built == [True]


def test_all_success_returns_rows(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    built: list[bool] = []
    _stub(
        monkeypatch,
        [
            ConnectorOutcome(name="databricks", rows=10, ok=True),
            ConnectorOutcome(name="aws_focus", rows=5, ok=True),
        ],
        built,
    )
    assert run_ingest() == 15
    assert built == [True]
