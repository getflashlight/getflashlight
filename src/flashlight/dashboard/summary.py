"""Template spend summaries and cross-provider analytics for the dashboard."""

from __future__ import annotations

from datetime import date

import pandas as pd

from flashlight.dashboard.data import gold_df, provider_label
from flashlight.dashboard.data import to_date as _d
from flashlight.dashboard.theme import md_money
from flashlight.transform.catalog import discover_provider_groups


def driver_dim(group: str) -> tuple[str, str, str]:
    """Mover grain for a provider: ``(sql_id_col, display_label, mom_view)``."""
    if group == "databricks":
        return "sku_id", "SKU", "sku_month_over_month"
    return "service_name", "Service", "spend_by_service_month"


def provider_spend_summary(
    group: str, label: str, start: date, end: date, *, partial: bool
) -> str:
    """One-line NL summary of net spend and the top mover in the selected window."""
    sm = start.replace(day=1)
    agg = gold_df(
        "SELECT coalesce(sum(net_cost),0) AS net FROM "
        f'"{group}".monthly_bill WHERE charge_month >= \'{sm}\' AND charge_month <= \'{end}\''
    ).iloc[0]
    net = float(agg["net"])
    if not net:
        return f"No {label} spend in the selected range."

    current = _d(gold_df("SELECT date_trunc('month', CURRENT_DATE) AS m").iloc[0]["m"])
    cmp_end = min(end.replace(day=1), (pd.Timestamp(current) - pd.DateOffset(months=1)).date())
    prior = (pd.Timestamp(cmp_end) - pd.DateOffset(months=1)).date()
    bill = gold_df(
        f"SELECT coalesce(sum(net_cost) FILTER (WHERE charge_month='{cmp_end}'),0) AS cur, "
        f"coalesce(sum(net_cost) FILTER (WHERE charge_month='{prior}'),0) AS prev "
        f'FROM "{group}".monthly_bill'
    ).iloc[0]
    cur, prev = float(bill["cur"]), float(bill["prev"])
    delta = cur - prev
    partial_note = " (current month is still accruing)" if partial else ""
    if not prev:
        return f"{label} net spend is {md_money(net)} in the selected window{partial_note}."

    if delta == 0:
        mover_line = f"Flat vs {prior:%b %Y} at month grain."
    else:
        id_col, id_label, _ = driver_dim(group)
        if group == "databricks":
            movers = gold_df(
                f"SELECT {id_col} AS k, cost_delta FROM \"{group}\".sku_month_over_month "
                f"WHERE charge_month = '{cmp_end}' AND cost_delta IS NOT NULL "
                "ORDER BY abs(cost_delta) DESC LIMIT 1"
            )
        else:
            movers = _service_movers(group, cmp_end, prior)
        top = ""
        if not movers.empty:
            name = str(movers.iloc[0]["k"])
            if group == "databricks":
                name = name.replace("ENTERPRISE_", "")
            d = float(movers.iloc[0]["cost_delta"])
            top = f", mostly **{name}** ({d:+,.0f})"
        verb = "rose" if delta > 0 else "fell"
        mover_line = (
            f"{verb} {md_money(abs(delta))} ({100 * delta / prev:+.1f}%) vs {prior:%b %Y}{top}."
        )

    return f"{label} net spend {md_money(net)} in the selected window{partial_note}. {mover_line}"


def _service_movers(group: str, month: date, prior: date) -> pd.DataFrame:
    """Per-service cur(``net_cost``)/prev/delta/%-change for two months, ordered by |delta|."""
    df = gold_df(
        f"SELECT service_name AS k, "
        f"coalesce(sum(net_cost) FILTER (WHERE charge_month='{month}'),0) AS net_cost, "
        f"coalesce(sum(net_cost) FILTER (WHERE charge_month='{prior}'),0) AS prev "
        f'FROM "{group}".spend_by_service_month '
        f"WHERE charge_month IN ('{month}','{prior}') GROUP BY service_name"
    )
    df["cost_delta"] = df["net_cost"] - df["prev"]
    df["cost_pct_change"] = df.apply(
        lambda r: round(100 * r["cost_delta"] / r["prev"], 1) if r["prev"] else None, axis=1
    )
    return df.loc[df["cost_delta"].abs().sort_values(ascending=False).index]


def cross_provider_movers(month: date, prior: date, *, limit: int = 8) -> pd.DataFrame:
    """Largest absolute MoM deltas across providers at each provider's natural grain."""
    frames: list[pd.DataFrame] = []
    for group in discover_provider_groups():
        label = provider_label(group)
        id_col, driver_type, _ = driver_dim(group)
        if group == "databricks":
            df = gold_df(
                f"SELECT '{label}' AS provider, {id_col} AS driver, cost_delta, cost_pct_change "
                f'FROM "{group}".sku_month_over_month WHERE charge_month = \'{month}\' '
                "AND cost_delta IS NOT NULL"
            )
        else:
            svc = _service_movers(group, month, prior)
            if svc.empty:
                continue
            svc["provider"] = label
            svc["driver"] = svc["k"]
            svc["cost_pct_change"] = svc.apply(
                lambda r: 100 * r["cost_delta"] / r["prev"] if r["prev"] else None, axis=1
            )
            df = svc[["provider", "driver", "cost_delta", "cost_pct_change"]]
        if not df.empty:
            if group == "databricks":
                df["driver"] = df["driver"].str.replace("ENTERPRISE_", "", regex=False)
            df["driver_type"] = driver_type
            frames.append(df)
    if not frames:
        return pd.DataFrame()
    out = pd.concat(frames, ignore_index=True)
    out["abs_delta"] = out["cost_delta"].abs()
    return (
        out.sort_values("abs_delta", ascending=False)
        .head(limit)
        .drop(columns=["abs_delta"])
        .rename(columns={"driver_type": "type"})
    )
