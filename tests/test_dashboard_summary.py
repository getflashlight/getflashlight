"""Dashboard NL summary helpers."""

from __future__ import annotations

from datetime import date
from unittest.mock import patch

import pandas as pd

from auralake.dashboard.summary import provider_spend_summary


def test_provider_spend_summary_escapes_dollar_for_markdown() -> None:
    """Streamlit treats bare $ as LaTeX — summaries must escape it."""

    def fake_gold_df(sql: str) -> pd.DataFrame:
        if (
            "coalesce(sum(net_cost),0) AS net" in sql
            and "monthly_bill WHERE charge_month >=" in sql
        ):
            return pd.DataFrame([{"net": 177_127.0}])
        if "AS cur," in sql and "AS prev" in sql:
            return pd.DataFrame([{"cur": 19_000.0, "prev": 28_051.0}])
        if "date_trunc('month', CURRENT_DATE)" in sql:
            return pd.DataFrame([{"m": pd.Timestamp("2026-06-01")}])
        if "sku_month_over_month" in sql:
            return pd.DataFrame([{"k": "ENTERPRISE_JOBS_COMPUTE", "cost_delta": -3209.0}])
        raise AssertionError(f"unexpected query: {sql}")

    with patch("auralake.dashboard.summary.gold_df", side_effect=fake_gold_df):
        text = provider_spend_summary(
            "databricks", "Databricks", date(2025, 12, 31), date(2026, 6, 25), partial=True
        )

    assert "\\$177,127" in text
    assert "**\\$" in text
    assert " * *" not in text
    assert "JOBS_COMPUTE" in text
