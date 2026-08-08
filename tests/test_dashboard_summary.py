"""Dashboard NL summary helpers."""

from __future__ import annotations

from datetime import date
from unittest.mock import patch

import pandas as pd

from flashlight.dashboard.summary import compute_provider_spend_alert, provider_spend_summary


def _fake_gold_df(sql: str) -> pd.DataFrame:
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
        return pd.DataFrame(
            [
                {
                    "k": "ENTERPRISE_JOBS_COMPUTE",
                    "cost_delta": -3209.0,
                    "cost_pct_change": -14.4,
                }
            ]
        )
    raise AssertionError(f"unexpected query: {sql}")


def test_provider_spend_summary_formats_dollars_bold() -> None:
    """NiceGUI's markdown has no LaTeX-vs-$ conflict (unlike Streamlit's), so no
    escape is needed — just the bold-dollar formatting.
    """
    with patch("flashlight.dashboard.summary.gold_df", side_effect=_fake_gold_df):
        text = provider_spend_summary(
            "databricks", "Databricks", date(2025, 12, 31), date(2026, 6, 25), partial=True
        )

    assert "$177,127" in text
    assert "**$" in text
    assert " * *" not in text
    assert "JOBS_COMPUTE" in text


def test_compute_provider_spend_alert_structures_mom_and_movers() -> None:
    with patch("flashlight.dashboard.summary.gold_df", side_effect=_fake_gold_df):
        alert = compute_provider_spend_alert(
            "databricks",
            "Databricks",
            date(2025, 12, 31),
            date(2026, 6, 25),
            partial=True,
            mover_limit=5,
        )

    assert alert.window_net == 177_127.0
    assert alert.partial is True
    assert alert.driver_label == "SKU"
    assert alert.cmp_month == date(2026, 5, 1)
    assert alert.prior_month == date(2026, 4, 1)
    assert alert.cur_net == 19_000.0
    assert alert.prior_net == 28_051.0
    assert alert.delta == -9_051.0
    assert alert.movers[0][0] == "JOBS_COMPUTE"
    assert alert.movers[0][1] == -3209.0
