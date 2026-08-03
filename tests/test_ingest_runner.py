"""Orchestration: ingest runs every connector, then raises for whatever failed."""

from __future__ import annotations

import pytest

from flashlight.core.exceptions import ConnectorError, IngestError
from flashlight.ingest import runner
from flashlight.ingest.runner import ConnectorOutcome, run_ingest
from flashlight.lake import bronze as bronze_module
from flashlight.lake import runlog as runlog_module


def _stub(monkeypatch, outcomes: list[ConnectorOutcome], built: list[bool], ran: list[str]) -> None:  # type: ignore[no-untyped-def]
    # One distinct sentinel config per outcome (not `[object()] * n` — that would
    # give every config the same identity, and _run_efficiency's "survivors only"
    # behavior needs configs that are actually distinguishable). build_connector/
    # run_connector are replaced so no real S3/Parquet work happens — we exercise
    # only the orchestration. ``ran`` records each connector actually invoked.
    #
    # Connectors now run concurrently (a bounded thread pool — see run_ingest), so
    # the stub looks up each config's outcome by identity (thread-safe, and correct
    # regardless of which thread runs first) rather than pulling from a shared
    # iterator — a shared `next(seq)` would race across worker threads. `ran`'s
    # *order* is consequently no longer deterministic; tests assert its contents
    # with a set, not a list.
    configs = [object() for _ in outcomes]
    outcome_by_config = dict(zip(configs, outcomes, strict=True))
    monkeypatch.setattr(runner, "load_connections", lambda _c: configs)
    monkeypatch.setattr(runner, "build_connector", lambda c: c)

    def _run_connector(conn, _w, _run_id, _on_progress=None, *, full_refresh=False):  # type: ignore[no-untyped-def]
        outcome = outcome_by_config[conn]
        ran.append(outcome.name)
        return outcome

    monkeypatch.setattr(runner, "run_connector", _run_connector)

    def _build_gold() -> int:
        built.append(True)
        return 11

    monkeypatch.setattr(runner, "build_gold", _build_gold)
    monkeypatch.setattr(runner, "_run_efficiency", lambda _w, _c, _p=None: 0)
    monkeypatch.setattr(runner, "_run_driver_health", lambda _w, _c: 0)


def test_all_connectors_run_even_after_a_failure(monkeypatch) -> None:  # type: ignore[no-untyped-def]
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
    # Only the failed connector is reported...
    assert exc.value.failed == ["aws_focus"]
    # ...but every connector ran, including the one after the failure. Order isn't
    # guaranteed once connectors run concurrently (see _stub's docstring).
    assert set(ran) == {"databricks", "aws_focus", "aws_infra"}
    # GOLD is still rebuilt from the connectors that succeeded — twice: once
    # right after the cost pull, once more as the final holistic rebuild after
    # the efficiency/driver-health phases (see run_ingest).
    assert built == [True, True]


def test_run_ingest_connector_filter_runs_only_the_matching_connector(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """The dashboard's per-connection Sync button (and the CLI's --connector) pass
    `connector=<effective name>` — only that one connector's config should reach
    build_connector/run_connector, everything else about the run is unchanged.
    """
    class _Config:
        def __init__(self, type: str) -> None:  # noqa: A002 - mirrors ConnectorConfig's own field name
            self.type = type
            self.name = None

    built: list[bool] = []
    ran: list[str] = []
    outcomes = [
        ConnectorOutcome(name="aws_focus", rows=10, ok=True),
        ConnectorOutcome(name="redshift", rows=0, ok=True),
    ]
    configs = [_Config("aws_focus"), _Config("redshift")]
    outcome_by_config = dict(zip(configs, outcomes, strict=True))
    monkeypatch.setattr(runner, "load_connections", lambda _c: configs)
    monkeypatch.setattr(runner, "build_connector", lambda c: c)

    def _run_connector(conn, _w, _run_id, _on_progress=None, *, full_refresh=False):  # type: ignore[no-untyped-def]
        outcome = outcome_by_config[conn]
        ran.append(outcome.name)
        return outcome

    monkeypatch.setattr(runner, "run_connector", _run_connector)

    def _build_gold() -> int:
        built.append(True)
        return 1

    monkeypatch.setattr(runner, "build_gold", _build_gold)
    monkeypatch.setattr(runner, "_run_efficiency", lambda _w, _c, _p=None: 0)
    monkeypatch.setattr(runner, "_run_driver_health", lambda _w, _c: 0)

    rows = run_ingest(connector="redshift")
    assert ran == ["redshift"]
    assert rows == 0
    assert built == [True, True]  # cost-pull publish + final holistic rebuild


def test_all_fail_skips_gold_rebuild(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    built: list[bool] = []
    ran: list[str] = []
    _stub(
        monkeypatch,
        [
            ConnectorOutcome(name="databricks", rows=0, ok=False, detail="bad token"),
            ConnectorOutcome(name="aws_focus", rows=0, ok=False, detail="S3 list failed"),
        ],
        built,
        ran,
    )
    with pytest.raises(IngestError) as exc:
        run_ingest()
    assert exc.value.failed == ["databricks", "aws_focus"]
    assert set(ran) == {"databricks", "aws_focus"}
    assert built == []  # nothing succeeded, so no rebuild


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
    assert built == [True, True]  # cost-pull publish + final holistic rebuild
    assert set(ran) == {"databricks", "aws_focus"}


def test_efficiency_and_driver_health_get_survivors_only(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    built: list[bool] = []
    ran: list[str] = []
    _stub(
        monkeypatch,
        [
            ConnectorOutcome(name="databricks", rows=10, ok=True),
            ConnectorOutcome(name="aws_focus", rows=0, ok=False, detail="expired token"),
        ],
        built,
        ran,
    )
    efficiency_configs: list[list[object]] = []
    driver_health_configs: list[list[object]] = []

    def _record_efficiency(_w: object, configs: list[object], _p: object = None) -> int:
        efficiency_configs.append(configs)
        return 0

    def _record_driver_health(_w: object, configs: list[object]) -> int:
        driver_health_configs.append(configs)
        return 0

    monkeypatch.setattr(runner, "_run_efficiency", _record_efficiency)
    monkeypatch.setattr(runner, "_run_driver_health", _record_driver_health)
    with pytest.raises(IngestError):
        run_ingest()
    assert len(efficiency_configs[0]) == 1  # only the one connector that succeeded
    assert len(driver_health_configs[0]) == 1


def test_cost_pull_gold_publish_survives_a_later_phase_dying(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Regression test for a real incident: a process killed partway through
    the best-effort efficiency/driver-health phases left successfully-pulled
    cost data sitting in BRONZE with GOLD never published at all, because the
    old code called build_gold() exactly once, at the very end. build_gold()
    must instead run right after the cost pull too — simulated here by making
    _run_efficiency blow up outright (standing in for the process dying
    mid-phase) and confirming the cost-pull publish already happened by then.
    """
    built: list[bool] = []
    ran: list[str] = []
    _stub(
        monkeypatch,
        [ConnectorOutcome(name="databricks", rows=10, ok=True)],
        built,
        ran,
    )

    def _boom(_w: object, _c: object, _p: object = None) -> int:
        raise RuntimeError("simulated mid-run kill")

    monkeypatch.setattr(runner, "_run_efficiency", _boom)

    with pytest.raises(RuntimeError, match="simulated mid-run kill"):
        run_ingest()

    assert built == [True]


def test_run_ingest_threads_progress_callback_to_every_connector(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # The stubbed run_connector doesn't call on_progress itself (that's exercised
    # against the real run_connector below); here we only confirm run_ingest
    # passes the same callback through to every connector it runs.
    monkeypatch.setattr(runner, "load_connections", lambda _c: [object(), object()])
    monkeypatch.setattr(runner, "build_connector", lambda c: c)
    monkeypatch.setattr(runner, "build_gold", lambda: 0)
    monkeypatch.setattr(runner, "_run_efficiency", lambda _w, _c, _p=None: 0)
    monkeypatch.setattr(runner, "_run_driver_health", lambda _w, _c: 0)

    received: list[object] = []

    def _run_connector(_conn, _w, _run_id, on_progress=None, *, full_refresh=False):  # type: ignore[no-untyped-def]
        received.append(on_progress)
        return ConnectorOutcome(name="x", rows=1, ok=True)

    monkeypatch.setattr(runner, "run_connector", _run_connector)

    def sentinel(*_a: object) -> None:
        pass

    run_ingest(on_progress=sentinel)
    assert received == [sentinel, sentinel]


def test_run_connector_emits_start_done_and_failed_events(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from datetime import date

    from flashlight.ingest.base import Connector, IngestWindow
    from flashlight.ingest.runner import run_connector

    monkeypatch.setattr(runlog_module, "record_run", lambda **_kw: None)

    class _Ok(Connector):
        name = "ok"

        def fetch(self, window):  # type: ignore[no-untyped-def]
            return iter(())

    class _Broken(Connector):
        name = "broken"

        def fetch(self, window):  # type: ignore[no-untyped-def]
            raise RuntimeError("boom")
            yield  # pragma: no cover

    def _drain(_connector, _window, records, *, ingest_run_id):  # type: ignore[no-untyped-def]
        # Mirrors the real write_window: must actually iterate `records` for a
        # mid-stream connector failure to surface (a no-op stub would swallow it).
        return sum(1 for _ in records)

    monkeypatch.setattr(bronze_module, "write_window", _drain)

    events: list[tuple[str, str, int]] = []
    window = IngestWindow(date(2026, 1, 1), date(2026, 1, 31))
    run_connector(_Ok(), window, "r1", on_progress=lambda *e: events.append(e))
    assert events == [("start", "ok", 0), ("done", "ok", 0)]

    events.clear()
    run_connector(_Broken(), window, "r1", on_progress=lambda *e: events.append(e))
    assert events == [("start", "broken", 0), ("failed", "broken", 0)]


def test_run_connector_full_refresh_purges_connector_bronze_first(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from datetime import date

    from flashlight.ingest.base import Connector, IngestWindow
    from flashlight.ingest.runner import run_connector

    monkeypatch.setattr(runlog_module, "record_run", lambda **_kw: None)

    class _Stub(Connector):
        name = "aws_focus"

        def fetch(self, window):  # type: ignore[no-untyped-def]
            return iter(())

    monkeypatch.setattr(bronze_module, "write_window", lambda *_a, **_kw: 0)
    purged: list[str] = []
    monkeypatch.setattr(bronze_module, "purge_connector", lambda name: purged.append(name))

    window = IngestWindow(date(2026, 1, 1), date(2026, 1, 31))
    run_connector(_Stub(), window, "r1", full_refresh=False)
    assert purged == []

    run_connector(_Stub(), window, "r1", full_refresh=True)
    assert purged == ["aws_focus"]


def test_max_workers_bounded_by_config_count_and_setting(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from flashlight.core.settings import get_settings

    monkeypatch.setenv("FLASHLIGHT_INGEST_MAX_WORKERS", "5")
    get_settings.cache_clear()
    try:
        assert runner._max_workers(2) == 2  # fewer configs than the cap
        assert runner._max_workers(10) == 5  # capped by the setting
        assert runner._max_workers(0) == 1  # never 0 — ThreadPoolExecutor rejects that
    finally:
        get_settings.cache_clear()


def test_efficiency_writes_are_merged_not_clobbered(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Regression test: write_efficiency purges its target provider/month partition
    wholesale before writing. aws_focus and redshift both emit provider_name="AWS" —
    writing per-connector (even one at a time) means the second connector's write
    purges and silently drops the first connector's rows. _run_efficiency must
    gather every connector's records first and write exactly once.
    """
    from collections.abc import Iterator
    from datetime import date
    from decimal import Decimal

    from flashlight.efficiency.model import EfficiencyRecord, EntityType
    from flashlight.ingest.base import IngestWindow
    from flashlight.lake import metrics as metrics_module

    record_a = EfficiencyRecord(
        provider_name="AWS",
        charge_month=date(2026, 1, 1),
        entity_type=EntityType.STORAGE,
        entity_id="a",
        entity_name="a",
        billed_cost=Decimal("1"),
    )
    record_b = EfficiencyRecord(
        provider_name="AWS",
        charge_month=date(2026, 1, 1),
        entity_type=EntityType.SQL_WAREHOUSE,
        entity_id="b",
        entity_name="b",
        billed_cost=Decimal("2"),
    )

    class _Fake:
        def __init__(self, name: str, records: list[EfficiencyRecord]) -> None:
            self.name = name
            self._records = records

        def fetch_efficiency(self, _window: object) -> Iterator[EfficiencyRecord]:
            return iter(self._records)

    connectors = [
        _Fake("aws_focus", [record_a]),
        _Fake("redshift", [record_b]),
    ]

    writes: list[list[EfficiencyRecord]] = []

    def _write_efficiency(_window: object, records: list[EfficiencyRecord]) -> int:
        writes.append(records)
        return len(records)

    monkeypatch.setattr(metrics_module, "write_efficiency", _write_efficiency)

    window = IngestWindow(date(2026, 1, 1), date(2026, 1, 31))
    written = runner._run_efficiency(window, connectors)  # type: ignore[arg-type]

    assert written == 2
    assert len(writes) == 1  # one combined write, not one per connector
    assert {r.entity_id for r in writes[0]} == {"a", "b"}


def test_efficiency_progress_reports_done_and_failed_per_connector(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """A connector whose cost pull is a no-op (Redshift) otherwise reports "done"
    while its real payload — fetch_efficiency — is still silently running or has
    failed with nothing printed. _run_efficiency must emit its own progress event
    per connector so that gap is visible (see connections.py's own comment on
    _CONNECTOR_DONE_RE for the user-facing symptom this fixes).
    """
    from collections.abc import Iterator
    from datetime import date

    from flashlight.efficiency.model import EfficiencyRecord
    from flashlight.ingest.base import IngestWindow

    class _Ok:
        name = "redshift-ok"

        def fetch_efficiency(self, _window: object) -> Iterator[EfficiencyRecord]:
            return iter(())

    class _Broken:
        name = "redshift-broken"

        def fetch_efficiency(self, _window: object) -> Iterator[EfficiencyRecord]:
            raise ConnectorError(self.name, "Statement did not complete in time")

    connectors = [_Ok(), _Broken()]

    events: list[tuple[str, str, int]] = []
    window = IngestWindow(date(2026, 1, 1), date(2026, 1, 31))
    runner._run_efficiency(
        window,
        connectors,  # type: ignore[arg-type]
        lambda event, name, rows: events.append((event, name, rows)),
    )

    by_name = {name: (event, rows) for event, name, rows in events}
    assert by_name["redshift-ok"] == ("efficiency_done", 0)
    assert by_name["redshift-broken"] == ("efficiency_failed", 0)
