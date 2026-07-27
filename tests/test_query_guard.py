from collections.abc import Iterator

import pytest

from flashlight.core.settings import get_settings
from flashlight.gold import reader
from flashlight.gold.reader import QueryError, run_select


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM raw.focus_record",
        "UPDATE gold.spend_trend_daily SET net_cost = 0",
        "DROP VIEW gold.tco_summary_month",
        "SELECT 1; DROP TABLE meta.ingest_run",
        "SELECT * FROM raw.focus_record",
        "SELECT * FROM meta.ingest_run",
        "INSERT INTO gold.x VALUES (1)",
        # The old flat `gold.` schema is gone — reject it with a clear pointer.
        "SELECT * FROM gold.monthly_bill",
        # Structural DuckDB statements a keyword-blocklist has to name explicitly —
        # none of these are `insert`/`update`/`delete`/`drop`/`create`/etc.
        "ATTACH 'evil.db' AS evil",
        "PRAGMA database_list",
        "SET threads=4",
        "INSTALL httpfs",
        "CALL pragma_version()",
        "COPY (SELECT 1) TO 'x.parquet'",
        "EXPORT DATABASE 'dump'",
        "DETACH evil",
        "RESET threads",
        "CHECKPOINT",
        "VACUUM",
    ],
)
def test_run_select_rejects_unsafe(sql: str) -> None:
    with pytest.raises(QueryError):
        run_select(sql)


def test_run_select_rejects_non_select() -> None:
    with pytest.raises(QueryError):
        run_select("TRUNCATE gold.spend_trend_daily")


@pytest.fixture
def lake_home(tmp_path, monkeypatch) -> Iterator[object]:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("FLASHLIGHT_HOME", str(tmp_path))
    get_settings.cache_clear()
    # The reader connection is cached across tests keyed on gold_dir+mtime; a
    # fresh FLASHLIGHT_HOME must not inherit a connection built for another
    # test's home.
    monkeypatch.setattr(reader, "_conn", None)
    monkeypatch.setattr(reader, "_signature", None)
    yield tmp_path
    get_settings.cache_clear()


def test_run_select_allows_plain_select(lake_home) -> None:  # type: ignore[no-untyped-def]
    assert run_select("select 1 as x") == [{"x": 1}]


def test_run_select_allows_with_query(lake_home) -> None:  # type: ignore[no-untyped-def]
    rows = run_select("with t as (select 1 as x) select x from t")
    assert rows == [{"x": 1}]


def test_run_select_connection_locks_configuration(lake_home) -> None:  # type: ignore[no-untyped-def]
    rows = run_select("select current_setting('lock_configuration') as locked")
    assert rows[0]["locked"] in ("true", "1", True)
