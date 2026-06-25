import pytest

from auralake.gold.reader import QueryError, run_select


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
    ],
)
def test_run_select_rejects_unsafe(sql: str) -> None:
    with pytest.raises(QueryError):
        run_select(sql)


def test_run_select_rejects_non_select() -> None:
    with pytest.raises(QueryError):
        run_select("TRUNCATE gold.spend_trend_daily")
