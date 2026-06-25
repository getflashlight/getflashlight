"""Orchestration: ingest is fail-fast — the first connector failure aborts the run."""

from __future__ import annotations

import pytest

from auralake.core.exceptions import IngestError
from auralake.ingest import runner
from auralake.ingest.runner import ConnectorOutcome, run_ingest


def _stub(monkeypatch, outcomes: list[ConnectorOutcome], built: list[bool], ran: list[str]) -> None:  # type: ignore[no-untyped-def]
    # One config per outcome; build_connector/run_connector are replaced so no real
    # S3/Parquet work happens — we exercise only the fail-fast orchestration. ``ran``
    # records each connector actually invoked, to prove later ones are skipped.
    monkeypatch.setattr(runner, "load_connections", lambda _c: [object()] * len(outcomes))
    monkeypatch.setattr(runner, "build_connector", lambda c: c)
    seq = iter(outcomes)

    def _run_connector(_conn, _w):  # type: ignore[no-untyped-def]
        outcome = next(seq)
        ran.append(outcome.name)
        return outcome

    monkeypatch.setattr(runner, "run_connector", _run_connector)

    def _build_gold() -> int:
        built.append(True)
        return 11

    monkeypatch.setattr(runner, "build_gold", _build_gold)


def test_failure_aborts_before_gold_and_skips_rest(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    built: list[bool] = []
    ran: list[str] = []
    _stub(
        monkeypatch,
        [
            ConnectorOutcome(name="databricks", rows=42, ok=True),
            ConnectorOutcome(name="aws_focus", rows=0, ok=False, detail="S3 list failed"),
            ConnectorOutcome(name="aws_infra", rows=99, ok=True),
        ],
        built,
        ran,
    )
    with pytest.raises(IngestError) as exc:
        run_ingest()
    assert exc.value.failed == ["aws_focus"]
    # Fail-fast: GOLD was NOT rebuilt, and the connector after the failure never ran.
    assert built == []
    assert ran == ["databricks", "aws_focus"]


def test_all_success_returns_rows_and_builds_gold(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    built: list[bool] = []
    ran: list[str] = []
    _stub(
        monkeypatch,
        [
            ConnectorOutcome(name="databricks", rows=10, ok=True),
            ConnectorOutcome(name="aws_focus", rows=5, ok=True),
        ],
        built,
        ran,
    )
    assert run_ingest() == 15
    assert built == [True]
    assert ran == ["databricks", "aws_focus"]
