"""Template spend summaries and cross-provider analytics for the dashboard."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import pandas as pd

from flashlight.dashboard.data import gold_df, provider_label
from flashlight.dashboard.data import to_date as _d
from flashlight.dashboard.theme import md_money
from flashlight.transform.catalog import discover_provider_groups


def _sql_str(value: str) -> str:
    """Escape a display string being inlined as a SQL literal (labels are configurable
    — see data._GROUP_LABEL_OVERRIDES — so an apostrophe in one is a syntax error)."""
    return value.replace("'", "''")


def driver_dim(group: str) -> tuple[str, str, str]:
    """Mover grain for a provider: ``(sql_id_col, display_label, mom_view)``."""
    if group == "databricks":
        return "sku_id", "SKU", "sku_month_over_month"
    return "service_name", "Service", "spend_by_service_month"


def entity_action_rows(rows: pd.DataFrame, entity_type: str, lens: str) -> pd.DataFrame:
    """One best priced action per entity for a workload/remedy lane.

    A single entity can fire several rules. They remain individually auditable in the
    dashboard, but their dollar figures must not be added into a purported next-action
    saving. This is the shared contract for the home page and Efficiency & Waste tab.
    """
    scoped = rows[(rows["entity_type"] == entity_type) & (rows["lens"] == lens)].copy()
    if scoped.empty:
        return scoped.assign(
            potential_savings=pd.Series(dtype=float), findings=pd.Series(dtype=int)
        )
    scoped["recoverable_cost"] = pd.to_numeric(
        scoped["recoverable_cost"], errors="coerce"
    ).fillna(0)
    scoped["billed_cost"] = pd.to_numeric(scoped["billed_cost"], errors="coerce").fillna(0)
    ordered = scoped.sort_values("recoverable_cost", ascending=False)
    best = ordered.drop_duplicates("entity_id").copy()
    best = best.join(scoped.groupby("entity_id").size().rename("findings"), on="entity_id")
    return best.rename(columns={"recoverable_cost": "potential_savings"}).sort_values(
        "potential_savings", ascending=False
    )


def action_group_rows(rows: pd.DataFrame) -> pd.DataFrame:
    """Conservative action potential grouped by workload type and remedy lane."""
    columns = [
        "entity_type",
        "lens",
        "potential_savings",
        "entities",
        "high_confidence",
    ]
    if rows.empty:
        return pd.DataFrame(columns=columns)
    parts: list[dict[str, object]] = []
    for (entity_type, lens), _ in rows.groupby(["entity_type", "lens"], dropna=False):
        entities = entity_action_rows(rows, str(entity_type), str(lens))
        if entities.empty:
            continue
        parts.append(
            {
                "entity_type": str(entity_type),
                "lens": str(lens),
                "potential_savings": float(entities["potential_savings"].sum()),
                "entities": int(len(entities)),
                "high_confidence": float(
                    entities.loc[entities["confidence"] == "high", "potential_savings"].sum()
                ),
            }
        )
    return pd.DataFrame(parts, columns=columns).sort_values("potential_savings", ascending=False)


def action_potential_by_month(rows: pd.DataFrame, start: date, end: date) -> pd.DataFrame:
    """Conservative potential by month and remedy lane for the Efficiency trend."""
    columns = ["charge_month", "lens", "potential_savings"]
    if rows.empty:
        return pd.DataFrame(columns=columns)
    month = pd.to_datetime(rows["charge_month"])
    scoped = rows.loc[(month >= pd.Timestamp(start)) & (month <= pd.Timestamp(end))]
    parts: list[pd.DataFrame] = []
    for charge_month, month_rows in scoped.groupby("charge_month"):
        groups = action_group_rows(month_rows)
        if groups.empty:
            continue
        parts.append(
            groups.groupby("lens", as_index=False)["potential_savings"]
            .sum()
            .assign(charge_month=charge_month)
        )
    return pd.concat(parts, ignore_index=True)[columns] if parts else pd.DataFrame(columns=columns)


def conservative_potential_total(rows: pd.DataFrame, *, cost_column: str) -> float:
    """Best action per entity/lane for a differently named potential-cost column."""
    if rows.empty:
        return 0.0
    ranked = rows.copy()
    ranked[cost_column] = pd.to_numeric(ranked[cost_column], errors="coerce").fillna(0)
    best = ranked.sort_values(cost_column, ascending=False).drop_duplicates(
        ["entity_type", "entity_id", "lens"]
    )
    return float(best[cost_column].sum())


@dataclass(frozen=True)
class ProviderSpendAlert:
    """Structured MoM spend signal for the Alerts tab (and unit tests).

    *window_net* is the selected range total. MoM fields compare the latest **complete**
    month in range against its prior — never a still-accruing current month. *movers* is
    ordered by ``|cost_delta|`` descending; empty when there is no prior month to compare.
    """

    label: str
    window_net: float
    partial: bool
    driver_label: str
    cmp_month: date | None = None
    prior_month: date | None = None
    cur_net: float = 0.0
    prior_net: float = 0.0
    movers: tuple[tuple[str, float, float | None], ...] = ()

    @property
    def delta(self) -> float | None:
        if self.prior_month is None or not self.prior_net:
            return None
        return self.cur_net - self.prior_net

    @property
    def pct_change(self) -> float | None:
        d = self.delta
        if d is None or not self.prior_net:
            return None
        return 100.0 * d / self.prior_net


def compute_provider_spend_alert(
    group: str,
    label: str,
    start: date,
    end: date,
    *,
    partial: bool,
    bill_view: str = "monthly_bill",
    scope_sql: str = "",
    mover_limit: int = 10,
) -> ProviderSpendAlert:
    """Window net + MoM movers for a provider page's Alerts tab.

    *bill_view* and *scope_sql* exist for a page covering a subset of its group's
    services (``/aws`` is scoped to Redshift's own service names): ``monthly_bill``
    carries no service dimension, so a narrowed caller passes the service-dimensioned
    ``spend_by_service_month`` plus its predicate instead.
    """
    sm = start.replace(day=1)
    scoped = f" AND {scope_sql}" if scope_sql else ""
    _, driver_label, _ = driver_dim(group)
    agg = gold_df(
        "SELECT coalesce(sum(net_cost),0) AS net FROM "
        f'"{group}".{bill_view} WHERE charge_month >= \'{sm}\' AND charge_month <= \'{end}\''
        f"{scoped}"
    ).iloc[0]
    window_net = float(agg["net"])
    if not window_net:
        return ProviderSpendAlert(
            label=label, window_net=0.0, partial=partial, driver_label=driver_label
        )

    current = _d(gold_df("SELECT date_trunc('month', CURRENT_DATE) AS m").iloc[0]["m"])
    cmp_end = min(end.replace(day=1), (pd.Timestamp(current) - pd.DateOffset(months=1)).date())
    prior = (pd.Timestamp(cmp_end) - pd.DateOffset(months=1)).date()
    bill = gold_df(
        f"SELECT coalesce(sum(net_cost) FILTER (WHERE charge_month='{cmp_end}'),0) AS cur, "
        f"coalesce(sum(net_cost) FILTER (WHERE charge_month='{prior}'),0) AS prev "
        f'FROM "{group}".{bill_view}' + (f" WHERE {scope_sql}" if scope_sql else "")
    ).iloc[0]
    cur, prev = float(bill["cur"]), float(bill["prev"])
    if not prev:
        return ProviderSpendAlert(
            label=label,
            window_net=window_net,
            partial=partial,
            driver_label=driver_label,
            cmp_month=cmp_end,
            prior_month=prior,
            cur_net=cur,
            prior_net=0.0,
        )

    movers = _top_movers(group, cmp_end, prior, scope_sql=scope_sql, limit=mover_limit)
    return ProviderSpendAlert(
        label=label,
        window_net=window_net,
        partial=partial,
        driver_label=driver_label,
        cmp_month=cmp_end,
        prior_month=prior,
        cur_net=cur,
        prior_net=prev,
        movers=movers,
    )


def provider_spend_summary(
    group: str,
    label: str,
    start: date,
    end: date,
    *,
    partial: bool,
    bill_view: str = "monthly_bill",
    scope_sql: str = "",
) -> str:
    """One-line NL summary of net spend and the top mover in the selected window.

    Kept as a thin wrapper over :func:`compute_provider_spend_alert` for unit tests and
    any caller that still wants a single markdown string. The provider page itself uses
    the structured alert on the Alerts tab.
    """
    alert = compute_provider_spend_alert(
        group,
        label,
        start,
        end,
        partial=partial,
        bill_view=bill_view,
        scope_sql=scope_sql,
        mover_limit=1,
    )
    if not alert.window_net:
        return f"No {label} spend in the selected range."

    partial_note = " (current month is still accruing)" if alert.partial else ""
    if not alert.prior_net:
        return (
            f"{label} net spend is {md_money(alert.window_net)} "
            f"in the selected window{partial_note}."
        )

    assert alert.prior_month is not None
    delta = alert.delta
    assert delta is not None
    if delta == 0:
        mover_line = f"Flat vs {alert.prior_month:%b %Y} at month grain."
    else:
        top = ""
        if alert.movers:
            name, d, _ = alert.movers[0]
            top = f", mostly **{name}** ({d:+,.0f})"
        verb = "rose" if delta > 0 else "fell"
        pct = alert.pct_change
        assert pct is not None
        mover_line = (
            f"{verb} {md_money(abs(delta))} ({pct:+.1f}%) vs {alert.prior_month:%b %Y}{top}."
        )

    return (
        f"{label} net spend {md_money(alert.window_net)} in the selected window"
        f"{partial_note}. {mover_line}"
    )


def _top_movers(
    group: str,
    month: date,
    prior: date,
    *,
    scope_sql: str = "",
    limit: int = 10,
) -> tuple[tuple[str, float, float | None], ...]:
    """``(display_name, cost_delta, cost_pct_change)`` ordered by ``|delta|``."""
    id_col, _, _ = driver_dim(group)
    if group == "databricks":
        movers = gold_df(
            f"SELECT {id_col} AS k, cost_delta, cost_pct_change "
            f'FROM "{group}".sku_month_over_month '
            f"WHERE charge_month = '{month}' AND cost_delta IS NOT NULL "
            "ORDER BY abs(cost_delta) DESC "
            f"LIMIT {int(limit)}"
        )
    else:
        movers = _service_movers(group, month, prior, scope_sql=scope_sql).head(limit)
    if movers.empty:
        return ()
    out: list[tuple[str, float, float | None]] = []
    for row in movers.itertuples(index=False):
        name = str(row.k)
        if group == "databricks":
            name = name.replace("ENTERPRISE_", "")
        pct = getattr(row, "cost_pct_change", None)
        pct_f = None if pct is None or (isinstance(pct, float) and pd.isna(pct)) else float(pct)
        out.append((name, float(row.cost_delta), pct_f))
    return tuple(out)


def _service_movers(
    group: str, month: date, prior: date, *, cost_col: str = "net_cost", scope_sql: str = ""
) -> pd.DataFrame:
    """Per-service cur/prev/delta/%-change for two months, ordered by |delta|.

    *cost_col* picks the measure: ``net_cost`` (credits netted in — what the invoice
    says) or ``gross_cost`` (charges only). The home page uses the latter, so a one-off
    credit doesn't top the mover list as a fake collapse in the service it landed on.

    *scope_sql* is an extra predicate for a page covering a subset of the group's
    services (``/aws`` is scoped to Redshift's own service names) — empty for every
    provider-wide caller, which is all of them but that one.
    """
    scoped = f" AND {scope_sql}" if scope_sql else ""
    df = gold_df(
        f"SELECT service_name AS k, "
        f"coalesce(sum({cost_col}) FILTER (WHERE charge_month='{month}'),0) AS net_cost, "
        f"coalesce(sum({cost_col}) FILTER (WHERE charge_month='{prior}'),0) AS prev "
        f'FROM "{group}".spend_by_service_month '
        f"WHERE charge_month IN ('{month}','{prior}'){scoped} GROUP BY service_name"
    )
    df["cost_delta"] = df["net_cost"] - df["prev"]
    df["cost_pct_change"] = df.apply(
        lambda r: round(100 * r["cost_delta"] / r["prev"], 1) if r["prev"] else None, axis=1
    )
    return df.loc[df["cost_delta"].abs().sort_values(ascending=False).index]


def cross_provider_movers(
    month: date, prior: date, *, limit: int = 8, exclude_credits: bool = False
) -> pd.DataFrame:
    """Largest absolute MoM deltas across providers at each provider's natural grain.

    *exclude_credits* measures the service-grain movers on charges only (``gross_cost``),
    so a one-off credit isn't reported as the month's biggest mover — see
    :func:`_service_movers`. It doesn't reach the Databricks branch: that one reads
    ``sku_month_over_month``, which is built on ``net_cost`` alone and has no
    credit-excluded variant (Databricks bills carry no credit lines in practice). If one
    ever does, that SKU shows up here credits-and-all.
    """
    frames: list[pd.DataFrame] = []
    for group in discover_provider_groups():
        label = provider_label(group)
        id_col, driver_type, _ = driver_dim(group)
        if group == "databricks":
            df = gold_df(
                f"SELECT '{_sql_str(label)}' AS provider, {id_col} AS driver, "
                "cost_delta, cost_pct_change "
                f'FROM "{group}".sku_month_over_month WHERE charge_month = \'{month}\' '
                "AND cost_delta IS NOT NULL"
            )
        else:
            svc = _service_movers(
                group, month, prior, cost_col="gross_cost" if exclude_credits else "net_cost"
            )
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
