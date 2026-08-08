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
        "DROP VIEW gold.monthly_bill",
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


@pytest.mark.parametrize(
    ("order_by", "descending", "expected"),
    [
        # Regression: an LLM writes the ORDER BY clause it would write in SQL.
        # A live Databricks gpt-oss-20b sent "net_cost DESC", got "Cannot order
        # by 'net_cost DESC'", and abandoned the question instead of retrying.
        ("net_cost DESC", False, ("net_cost", True)),
        ("net_cost desc", False, ("net_cost", True)),
        ("net_cost ASC", False, ("net_cost", False)),
        # A bare column is untouched, and an explicit flag still wins over a
        # contradicting suffix (a caller passing both meant the flag).
        ("net_cost", False, ("net_cost", False)),
        ("net_cost", True, ("net_cost", True)),
        ("net_cost ASC", True, ("net_cost", True)),
        # Not a direction suffix — left alone so the catalog check still rejects it.
        ("SUM(net_cost)", False, ("SUM(net_cost)", False)),
        ("net_cost DESCENDING", False, ("net_cost DESCENDING", False)),
    ],
)
def test_split_sort_direction(order_by: str, descending: bool, expected: tuple[str, bool]) -> None:
    assert reader._split_sort_direction(order_by, descending) == expected  # noqa: SLF001


def test_query_view_rejects_an_unknown_column_even_with_a_direction_suffix(lake_home) -> None:  # type: ignore[no-untyped-def]
    """Accepting the SQL spelling must not widen *what* can be ordered by.

    Uses a fixed-group view (always catalogued, unlike a provider group that
    only exists once its data is published) so the order_by check is what
    rejects this, not the earlier unknown-view check."""
    with pytest.raises(QueryError, match="Cannot order by 'nonsense'"):
        reader.query_view("efficiency.waste_summary_month", order_by="nonsense DESC")


@pytest.mark.parametrize(
    "sql",
    [
        # Verified to return real data before the guard existed: the keyword blocklist
        # only names *mutating* statements, and a filesystem read is a plain SELECT.
        "SELECT * FROM read_csv_auto('/etc/hosts')",
        "SELECT * FROM read_csv('/etc/passwd')",
        "SELECT * FROM glob('/*')",
        "SELECT * FROM read_parquet('/tmp/anything.parquet')",
        "SELECT * FROM parquet_scan('/tmp/anything.parquet')",
        "SELECT read_text('/etc/hosts')",
        "SELECT read_blob('/etc/hosts')",
        "SELECT * FROM read_json_auto('/tmp/x.json')",
        "SELECT * FROM parquet_metadata('/tmp/x.parquet')",
        "SELECT * FROM delta_scan('/tmp/delta')",
        # Nested inside a CTE, which starts with WITH and so passes the prefix check.
        "WITH x AS (SELECT * FROM glob('/etc/*')) SELECT * FROM x",
    ],
)
def test_run_select_rejects_filesystem_reads(sql: str) -> None:
    """enable_external_access must stay on (the GOLD views are read_parquet over disk
    paths), so these are blocked at the query-string layer instead."""
    with pytest.raises(QueryError, match="Filesystem-reading"):
        run_select(sql)


def test_run_select_still_allows_a_metric_view(lake_home) -> None:  # type: ignore[no-untyped-def]
    """The counterpart: blocking read_parquet in user SQL must not break the views, whose
    own definitions are read_parquet over disk (never passed through this guard)."""
    from datetime import date
    from decimal import Decimal

    from flashlight.focus.enums import ChargeCategory, ProviderName, ServiceCategory
    from flashlight.focus.model import FocusRecord
    from flashlight.ingest.base import IngestWindow
    from flashlight.lake import bronze
    from flashlight.transform.runner import build_gold

    day = date(2026, 5, 1)
    bronze.write_window(
        "t",
        IngestWindow(day, day),
        [
            FocusRecord(
                provider_name=ProviderName.AWS,
                billing_account_id="acct",
                billing_period_start=day,
                billing_period_end=date(2026, 6, 1),
                charge_period_start=day,
                charge_period_end=day,
                billed_cost=Decimal("7"),
                effective_cost=Decimal("7"),
                list_cost=Decimal("7"),
                charge_category=ChargeCategory.USAGE,
                service_category=ServiceCategory.COMPUTE,
                service_name="AmazonEC2",
                x_source_connector="t",
            )
        ],
        ingest_run_id="r1",
    )
    build_gold()

    rows = run_select("SELECT sum(net_cost) AS total FROM aws.monthly_bill")
    assert rows and float(rows[0]["total"]) == 7.0
