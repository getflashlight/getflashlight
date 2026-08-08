"""AI costs — what the AI slice of the bill costs, and on which endpoint.

The "AI Costs" tab on the Databricks provider page. Nested there rather than being a
top-level nav entry for the same reason Client Driver Health is: Databricks is the only
connector that emits AI-categorized FOCUS rows today, and ``gold.ai_product_family`` maps
Databricks' ``billing_origin_product`` enum specifically. When a connector for another
provider's AI products lands (AWS Bedrock stamps the same FOCUS ``service_category``), its
group's ``ai_spend_month`` populates for free and this becomes a core tab.

Two sources, joined nowhere in this module:

* ``<group>.ai_spend_month`` — **cost**, from the FOCUS bill. Always complete. Covers every
  product the provider categorizes as AI plus the ones Databricks files elsewhere (AI/BI
  Genie above all — see ``037_gold_ai_spend.sql``).
* the ``ai_usage`` group — **tokens**, from the ``system.serving`` pull. Often absent, since
  that schema is a Public Preview an account has to enable.

The cost↔token join happens in GOLD, once, and carries a ``cost_allocation_basis`` saying
whether a per-token dollar figure is defensible. Honesty stays in the places that carry it:

* blank ``allocated_cost`` / ``$ / 1M`` cells (never coalesced to $0)
* KPI sub-lines for missing telemetry and untagged spend (``—``, never a measured ``0``)
* token-detail panels only when request telemetry exists — omitted when the serving pull
  never ran or produced no measured endpoints, so empty panels don't repeat the gap

The tab follows the page date range (``sm``, ``end`` from ``provider_focus``), not a
pinned latest month. Bill-side attribution is Attribution's "Values for one key" pattern
over the raw ``tags`` JSON (dropdown defaults to ``project`` when present).

Not a waste surface: endpoint findings (idle provisioned capacity, scale-to-zero gaps) are
``efficiency.waste_record`` rows and belong on the Efficiency & Waste tab, so the
recoverable-dollar story stays in one place.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import date
from typing import NamedTuple

import pandas as pd
import plotly.express as px
from nicegui import ui

from flashlight.dashboard import chrome
from flashlight.dashboard.data import gold_df, gold_view_published, provider_label
from flashlight.dashboard.theme import compact_money
from flashlight.efficiency.waste_rules import WASTE_RULES
from flashlight.transform.catalog import AI_USAGE_GROUP as _AI_USAGE_GROUP

_GROUP = "databricks"

_STALE_MSG = (
    "This lake's published GOLD predates the AI spend view — run `flashlight transform` to "
    "rebuild it (no re-ingest needed; the AI rows are already in BRONZE)."
)

_NO_TOKEN_TELEMETRY_SUB = "no serving telemetry"

_PRODUCT_LABELS = {
    "model_serving": "Model Serving",
    "vector_search": "Vector Search",
    "ai_gateway": "AI Gateway",
    "ai_functions": "AI Functions",
    "foundation_model_training": "Foundation Model Training",
    "agent_bricks": "Agent Bricks",
    "ai_runtime": "AI Runtime",
    "agent_evaluation": "Agent Evaluation",
    "genie": "AI/BI Genie",
    "ai_bi_dashboard": "AI/BI Dashboards",
}
_REMEDY_BY_CATEGORY = {r.category: r.remedy for r in WASTE_RULES}

_UNMAPPED_LABEL = "Unmapped AI product"
_UNTAGGED = "(untagged)"

_MAX_ROWS = 40
_TREND_MONTHS = 6

KPI_SUB = "Included in Net Spend"


def _df(sql: str) -> pd.DataFrame:
    """Query an AI view, returning empty on any issue (view may be unbuilt)."""
    try:
        return gold_df(sql)
    except Exception:  # noqa: BLE001 - missing/empty view → render the empty state
        return pd.DataFrame()


def _compact_count(value: int) -> str:
    """Token counts run to billions, so abbreviate — the compact_money treatment for a
    non-money quantity (theme.compact_money would prefix a `$`)."""
    for limit, suffix in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(value) >= limit:
            return f"{value / limit:,.1f}{suffix}"
    return f"{value:,}"


def _product_label(family: object) -> str:
    """Display label for an ai_product_family value, including the unmapped NULL case."""
    if family is None or pd.isna(family):
        return _UNMAPPED_LABEL
    return _PRODUCT_LABELS.get(str(family), str(family))


def _fold_tag_key(key: str) -> str:
    """Same case/separator fold as 036_gold_tag_keys / ai_spend_month.project_tag."""
    return key.strip().lower().replace("-", "_")


def _parse_tags(raw: object) -> dict[str, str]:
    if raw is None or (isinstance(raw, float) and pd.isna(raw)):
        return {}
    if isinstance(raw, dict):
        return {str(k): str(v) for k, v in raw.items() if v is not None}
    try:
        obj = json.loads(str(raw))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    if not isinstance(obj, dict):
        return {}
    return {str(k): str(v) for k, v in obj.items() if v is not None}


def _tag_value(raw: object, key: str) -> str | None:
    """Value for *key* on a tags JSON cell, folding case/separators like GOLD."""
    folded = _fold_tag_key(key)
    for k, v in _parse_tags(raw).items():
        if _fold_tag_key(k) == folded:
            return v or None
    return None


def _tag_keys_in(rows: pd.DataFrame) -> list[str]:
    """Distinct raw tag keys present on AI spend rows, sorted case-insensitively."""
    if "tags" not in rows.columns:
        return []
    keys: set[str] = set()
    for raw in rows["tags"]:
        keys.update(_parse_tags(raw))
    return sorted(keys, key=str.lower)


def kpi_card(sm: date, end: date) -> chrome.KpiCard | None:
    """This window's AI/ML spend, as a card on the provider's KPI row.

    Same bill, same dollars — a subset of the ``net`` card, which is why it carries no
    variant colour: white marks it as part of that total, where backing storage's own hue
    marks it as a different bill. Windowed to the page's date range.

    ``None`` when the view is unpublished or the window has no AI rows: a "$0 AI spend"
    card on a lake whose GOLD predates the view would be a measurement gap rendered as a
    fact, and on a lake that genuinely runs no AI products it's a card about nothing.
    """
    if not gold_view_published(_GROUP, "ai_spend_month"):
        return None
    df = _df(
        f'SELECT sum(net_cost) AS c FROM "{_GROUP}".ai_spend_month '
        f"WHERE charge_month >= '{sm}' AND charge_month <= '{end}'"
    )
    if df.empty or pd.isna(df["c"].iloc[0]) or not float(df["c"].iloc[0]):
        return None
    return ("AI Spend", compact_money(float(df["c"].iloc[0])), KPI_SUB)


def render(sm: date, end: date) -> None:
    """AI Costs tab for the page date range [*sm*, *end*] (month-truncated start)."""
    label = provider_label(_GROUP)
    chrome.section_title(f"{label} AI costs")

    if not gold_view_published(_GROUP, "ai_spend_month"):
        chrome.section_caption(_STALE_MSG)
        return

    rows = _df(
        f'SELECT * FROM "{_GROUP}".ai_spend_month '
        f"WHERE charge_month >= '{sm}' AND charge_month <= '{end}' "
        "ORDER BY charge_month DESC, net_cost DESC"
    )
    if rows.empty:
        chrome.empty_state(
            "smart_toy",
            f"No AI spend found for {label}",
            "Nothing on this bill is categorized as 'AI and Machine Learning'. If you do run "
            "Model Serving, Vector Search or AI Functions, re-run `flashlight ingest` — the "
            "categorization comes from the billing data itself, so a lake ingested before "
            "those products were in use won't show them.",
        )
        return

    rows = rows.assign(product=rows["ai_product_family"].map(_product_label))
    tokens = _token_rows(sm, end)
    tag_keys = _tag_keys_in(rows)
    default_tag: str | None = None
    if tag_keys:
        default_tag = next((k for k in tag_keys if _fold_tag_key(k) == "project"), tag_keys[0])

    def _with_tag(tag_key: str | None) -> pd.DataFrame:
        raw_tags = rows["tags"] if "tags" in rows.columns else pd.Series([None] * len(rows))
        display = [_tag_value(raw, tag_key) if tag_key else None for raw in raw_tags]
        return rows.assign(tag_display=pd.Series(display, index=rows.index).fillna(_UNTAGGED))

    # Untagged KPI follows the tag-values panel's selected key (Attribution-style).
    kpi_slot = ui.column().classes("w-full")

    @ui.refreshable
    def _kpis_for(tag_key: str | None) -> None:
        kpi_slot.clear()
        with kpi_slot:
            _kpis(_with_tag(tag_key), tokens)

    def _on_tag(key: str | None) -> None:
        _kpis_for.refresh(key)

    _kpis_for(default_tag)
    _trend(rows)
    _by_product(rows)
    _by_resource(rows)
    _by_tag(rows, tag_keys=tag_keys, default_tag=default_tag, on_tag_change=_on_tag)
    _by_user(tokens)
    _model_economics(tokens)
    _optimization(sm, end)


class _Tokens(NamedTuple):
    """Token telemetry for the page date range, or empty frames when unmeasured."""

    published: bool
    endpoints: pd.DataFrame
    projects: pd.DataFrame
    requesters: pd.DataFrame
    models: pd.DataFrame

    @property
    def measured(self) -> bool:
        """True when at least one endpoint has request telemetry in range.

        ``endpoint_month`` is a FULL OUTER join, so cost-only rows still populate the
        frame. Those are *published*, not *measured*.
        """
        if not self.published or self.endpoints.empty:
            return False
        status = self.endpoints.get("token_coverage_status")
        if status is None:
            return False
        return bool((status == "measured").any())


def _token_rows(sm: date, end: date) -> _Tokens:
    """Read the `ai_usage` group for the page date range."""
    if not gold_view_published(_AI_USAGE_GROUP, "endpoint_month"):
        empty = pd.DataFrame()
        return _Tokens(False, empty, empty, empty, empty)

    def _range(view: str, order: str) -> pd.DataFrame:
        return _df(
            f'SELECT * FROM "{_AI_USAGE_GROUP}".{view} '
            f"WHERE charge_month >= DATE '{sm}' AND charge_month <= DATE '{end}' "
            f"ORDER BY {order} DESC NULLS LAST"
        )

    return _Tokens(
        True,
        _range("endpoint_month", "total_tokens"),
        _range("project_month", "total_tokens"),
        _range("requester_month", "total_tokens"),
        _range("model_month", "total_tokens"),
    )


def _kpis(
    rows: pd.DataFrame,
    tokens: _Tokens,
) -> None:
    net = float(rows["net_cost"].fillna(0).sum())
    endpoints = int(rows["resource_id"].nunique())
    untagged = float(rows.loc[rows["tag_display"] == _UNTAGGED, "net_cost"].sum())

    if tokens.measured:
        ep = tokens.endpoints
        measured_eps = ep[ep["token_coverage_status"] == "measured"]
        total_tokens = int(measured_eps["total_tokens"].fillna(0).sum())
        metered = ep[ep["cost_allocation_basis"] == "measured_tokens"]
        metered_cost = float(metered["net_cost"].fillna(0).sum())
        metered_tokens = int(metered["total_tokens"].fillna(0).sum())
        rate = f"${1e6 * metered_cost / metered_tokens:,.2f}" if metered_tokens else "—"
        token_kpi: chrome.KpiCard = (
            "Tokens",
            _compact_count(total_tokens),
            "in + out, measured",
            "volume",
        )
        rate_kpi: chrome.KpiCard = (
            "Cost / 1M tokens",
            rate,
            "pay-per-token endpoints only",
            "rate",
        )
    else:
        token_kpi = ("Tokens", "—", _NO_TOKEN_TELEMETRY_SUB, "volume")
        rate_kpi = ("Cost / 1M tokens", "—", _NO_TOKEN_TELEMETRY_SUB, "rate")

    chrome.kpi_row(
        [
            ("AI Spend", compact_money(net), ""),
            ("AI Resources", f"{endpoints:,}", ""),
            token_kpi,
            rate_kpi,
            (
                "Untagged AI Spend",
                compact_money(untagged),
                "",
                "unattributed",
            ),
        ]
    )


def _trend(rows: pd.DataFrame) -> None:
    """AI spend per product per month within the page range (capped at 6 months)."""
    months = sorted(rows["charge_month"].astype(str).unique())
    recent = months[-_TREND_MONTHS:]
    window = rows[rows["charge_month"].astype(str).isin(recent)]
    by_month = (
        window.groupby([window["charge_month"].astype(str), "product"], as_index=False)["net_cost"]
        .sum()
        .rename(columns={"charge_month": "month"})
    )
    if by_month.empty:
        return

    with chrome.panel():
        chrome.panel_title("AI Spend by product")
        capped = chrome.cap_series(by_month, "product", "net_cost")
        fig = px.bar(capped, x="month", y="net_cost", color="product")
        totals = capped.groupby("month")["net_cost"].sum()
        for bar_month, total in totals.items():
            fig.add_annotation(
                x=bar_month,
                y=total,
                text=compact_money(float(total)),
                showarrow=False,
                yshift=10,
                font=dict(size=11, color=chrome.INK_SECONDARY),
            )
        chrome.plot(chrome.style_fig(fig, has_legend=True, category_x=True))


def _by_product(rows: pd.DataFrame) -> None:
    """Spend split by AI product across the page range."""
    by_product = (
        rows.groupby("product", as_index=False)
        .agg(
            net_cost=("net_cost", "sum"),
            resources=("resource_id", "nunique"),
        )
        .sort_values("net_cost", ascending=False)
    )
    with chrome.panel():
        chrome.panel_title("By AI product")
        chrome.flat_table(
            by_product,
            key="ai_by_product",
            money_cols=["net_cost"],
            int_cols=["resources"],
            rename={"product": "Product", "net_cost": "Cost", "resources": "Resources"},
        )


def _by_resource(rows: pd.DataFrame) -> None:
    """Resource-grain drill across the page range.

    `Quantity` and `Unit` stay adjacent, unsummed: a Model Serving row's quantity is DBUs
    while a pay-per-token row's is tokens. Bill-tag attribution lives in ``_by_tag``.
    """
    table = (
        rows.groupby(
            ["resource_name", "product", "sku_id", "consumed_unit"],
            as_index=False,
            dropna=False,
        )
        .agg(net_cost=("net_cost", "sum"), consumed_quantity=("consumed_quantity", "sum"))
        .sort_values("net_cost", ascending=False)
    )
    with chrome.panel():
        chrome.panel_title("By resource")
        chrome.searchable_table(
            table[
                [
                    "resource_name",
                    "product",
                    "sku_id",
                    "consumed_quantity",
                    "consumed_unit",
                    "net_cost",
                ]
            ],
            key="ai_by_endpoint",
            search_col="resource_name",
            money_cols=["net_cost"],
            num_cols=["consumed_quantity"],
            rename={
                "resource_name": "Resource",
                "product": "Product",
                "sku_id": "SKU",
                "consumed_quantity": "Quantity",
                "consumed_unit": "Unit",
                "net_cost": "Cost",
            },
            max_rows=_MAX_ROWS,
            pagination=10,
        )


def _by_tag(
    rows: pd.DataFrame,
    *,
    tag_keys: list[str],
    default_tag: str | None,
    on_tag_change: Callable[[str | None], None],
) -> None:
    """AI spend by one cost-allocation tag — Attribution's "Values for one key" pattern.

    Replaces the hardcoded Tokens-by-project bill lecture: the dropdown picks any tag key
    present on AI rows in range (default ``project`` when available).
    """
    if not tag_keys or default_tag is None:
        return

    with chrome.panel():
        chrome.panel_title("AI spend by tag")
        body_container = ui.column().classes("w-full gap-2")

        @ui.refreshable
        def _values_body(sel: str) -> None:
            body_container.clear()
            with body_container:
                raw_tags = rows["tags"] if "tags" in rows.columns else pd.Series([None] * len(rows))
                tagged = rows.assign(
                    tag_value=[_tag_value(raw, sel) or _UNTAGGED for raw in raw_tags]
                )
                by_value = (
                    tagged.groupby("tag_value", as_index=False)["net_cost"]
                    .sum()
                    .sort_values("net_cost", ascending=False)
                )
                chrome.searchable_table(
                    by_value,
                    key="ai_by_tag",
                    search_col="tag_value",
                    money_cols=["net_cost"],
                    rename={"tag_value": sel, "net_cost": "Net cost"},
                    max_rows=_MAX_ROWS,
                )

        def _on_change(e: object) -> None:
            value = str(getattr(e, "value", e))
            _values_body.refresh(value)
            on_tag_change(value)

        (
            ui.select(options=tag_keys, value=default_tag, on_change=_on_change)
            .props("dense outlined")
            .classes("w-48")
            .style(f"color:{chrome.INK_PRIMARY}")
        )
        _values_body(default_tag)


def _token_table(
    rows: pd.DataFrame,
    *,
    key: str,
    label_col: str,
    label_header: str,
    search_col: str | None = None,
    fold_col: str | None = None,
    fold_header: str | None = None,
    fold_wording: dict[str, str] | None = None,
    pin_first: str | None = None,
) -> None:
    """Shared shape of the by-project / by-user tables."""
    grouped = (
        rows.groupby(label_col, as_index=False, dropna=False)
        .agg(
            input_tokens=("input_tokens", "sum"),
            output_tokens=("output_tokens", "sum"),
            total_tokens=("total_tokens", "sum"),
            request_count=("request_count", "sum"),
            allocated_cost=("allocated_cost", lambda s: s.sum(min_count=1)),
        )
        .assign(
            basis=lambda df: _fold_label(
                rows, df, label_col, "cost_allocation_basis", _BASIS_WORDING
            )
        )
        .sort_values("total_tokens", ascending=False)
    )
    extra_cols: tuple[str, ...] = ()
    extra_rename: dict[str, str] = {}
    if fold_col is not None:
        grouped = grouped.assign(
            **{fold_col: _fold_label(rows, grouped, label_col, fold_col, fold_wording or {})}
        )
        extra_cols = (fold_col,)
        extra_rename = {fold_col: fold_header or fold_col}
    if pin_first is not None:
        grouped = grouped.assign(
            _pin=(grouped[label_col] != pin_first).astype(int)
        ).sort_values(["_pin", "total_tokens"], ascending=[True, False]).drop(columns="_pin")

    cols = [
        label_col,
        *extra_cols,
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "request_count",
        "allocated_cost",
        "basis",
    ]
    rename = {
        label_col: label_header,
        "input_tokens": "Tokens in",
        "output_tokens": "Tokens out",
        "total_tokens": "Total tokens",
        "request_count": "Requests",
        "allocated_cost": "Cost (allocated)",
        "basis": "Basis",
        **extra_rename,
    }
    chrome.searchable_table(
        grouped[cols],
        key=key,
        search_col=search_col or label_col,
        money_cols=["allocated_cost"],
        int_cols=["input_tokens", "output_tokens", "total_tokens", "request_count"],
        rename=rename,
        max_rows=_MAX_ROWS,
    )


_BASIS_WORDING = {
    "measured_tokens": "per-token (metered)",
    "unallocated": "hourly — not allocatable",
    "external_passthrough": "external model — vendor bills tokens",
    "unknown": "unknown billing mode",
}


def _fold_label(
    rows: pd.DataFrame,
    grouped: pd.DataFrame,
    label_col: str,
    value_col: str,
    wording: dict[str, str],
) -> list[str]:
    """Collapse *value_col*'s several values within each group into one phrase."""
    by_label = rows.groupby(label_col, dropna=False)[value_col].apply(
        lambda s: sorted({str(v) for v in s.dropna()})
    )
    out: list[str] = []
    for key in grouped[label_col]:
        values = by_label.get(key, [])
        phrases = [wording.get(v, v) for v in values]
        if not phrases:
            out.append("—")
        elif len(phrases) == 1:
            out.append(phrases[0])
        else:
            out.append("mixed: " + ", ".join(phrases))
    return out


def _by_user(tokens: _Tokens) -> None:
    """Tokens and allocatable cost per requesting identity."""
    if not tokens.measured or tokens.requesters.empty:
        return
    with chrome.panel():
        chrome.panel_title("Tokens by user")
        _token_table(
            tokens.requesters,
            key="ai_by_user",
            label_col="requester_display",
            label_header="User",
            fold_col="requester_kind",
            fold_header="Kind",
            fold_wording={},
            pin_first="(no requester recorded)",
        )


def _model_economics(tokens: _Tokens) -> None:
    """$ per 1M tokens by model — descriptive only, no waste rule."""
    if not tokens.measured or tokens.models.empty:
        return

    table = (
        tokens.models.groupby(
            ["model_name", "model_kind", "serving_mode"], as_index=False, dropna=False
        )
        .agg(
            total_tokens=("total_tokens", "sum"),
            request_count=("request_count", "sum"),
            allocated_cost=("allocated_cost", lambda s: s.sum(min_count=1)),
            cost_per_million_tokens=(
                "cost_per_million_tokens",
                lambda s: s.sum(min_count=1),
            ),
            endpoints=("endpoint_id", "nunique"),
        )
        .sort_values("total_tokens", ascending=False)
    )
    with chrome.panel():
        chrome.panel_title("Model unit economics")
        chrome.searchable_table(
            table,
            key="ai_model_economics",
            search_col="model_name",
            money_cols=["allocated_cost"],
            num_cols=["cost_per_million_tokens"],
            int_cols=["total_tokens", "request_count", "endpoints"],
            rename={
                "model_name": "Model",
                "model_kind": "Kind",
                "serving_mode": "Serving mode",
                "total_tokens": "Total tokens",
                "request_count": "Requests",
                "allocated_cost": "Cost (allocated)",
                "cost_per_million_tokens": "$ / 1M tokens",
                "endpoints": "Endpoints",
            },
            max_rows=_MAX_ROWS,
        )


def _optimization(sm: date, end: date) -> None:
    """Endpoint findings — pointer panel, only when at least one finding fired in range."""
    if not gold_view_published("efficiency", "waste_record"):
        return

    rows = _df(
        "SELECT waste_category, lens, confidence, count(DISTINCT entity_id) AS endpoints, "
        "sum(recoverable_cost) AS recoverable_cost FROM efficiency.waste_record "
        f"WHERE entity_type = 'endpoint' AND charge_month >= DATE '{sm}' "
        f"AND charge_month <= DATE '{end}' "
        "GROUP BY waste_category, lens, confidence ORDER BY recoverable_cost DESC NULLS LAST"
    )
    if rows.empty:
        return

    with chrome.panel():
        chrome.panel_title("What can be optimized")
        chrome.section_caption(
            "Endpoint findings only — full fleet on Efficiency & Waste. $0 recoverable is "
            "unpriced. WASTE and OPPORTUNITY are different remedies; never add them."
        )
        table = rows.assign(remedy=rows["waste_category"].map(_REMEDY_BY_CATEGORY))
        chrome.flat_table(
            table[["waste_category", "lens", "confidence", "endpoints", "recoverable_cost",
                   "remedy"]],
            key="ai_optimization",
            money_cols=["recoverable_cost"],
            int_cols=["endpoints"],
            rename={
                "waste_category": "Finding",
                "lens": "Lens",
                "confidence": "Confidence",
                "endpoints": "Endpoints",
                "recoverable_cost": "Recoverable",
                "remedy": "Remedy",
            },
        )
