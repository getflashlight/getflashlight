"""Shared visual language for the dashboard — the one place for the palette, the
Plotly layout, and the shadcn UI building blocks (KPI cards, tabs, tables).

The three views (:mod:`billing_overview`, :mod:`tco_overview`, :mod:`aws_focus`)
all route their charts through :func:`style_fig` and their headline numbers /
tables through :func:`kpi_cards` and :func:`shadcn_table`, so spend always reads
the same way — ``$``-prefixed axes, a lake-blue palette, and the modern shadcn
card/table chrome instead of raw Streamlit defaults.

shadcn components are React custom components rendered in iframes, so page CSS
can't reach inside them; they carry their own styling. Each one needs a unique
``key`` per session — the helpers derive keys from a caller-supplied prefix.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import date

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
import streamlit_shadcn_ui as ui

# Lake-water palette: teal → blue → slate, punched up for stronger contrast on light.
PALETTE = [
    "#0E7C86",  # teal (brand)
    "#1D6FB8",  # strong blue
    "#3BA7D6",  # bright sky
    "#13405E",  # deep navy
    "#2FA46B",  # green
    "#E0A33E",  # amber
    "#D45D79",  # rose
]

ACCENT = "#0E7C86"  # the brand teal used for KPI/heading accents

TEMPLATE = "plotly_white"


# ---------------------------------------------------------------------------
# Plotly
# ---------------------------------------------------------------------------
def style_fig(
    fig: go.Figure,
    *,
    currency_axis: str | None = "y",
    height: int = 340,
) -> go.Figure:
    """Apply the shared Auralake look to a Plotly figure.

    Sets the template and palette and a horizontal legend just above the plot.
    Chart titles are rendered separately via :func:`section_title` (a Plotly
    title would collide with the top legend), so this leaves room only for the
    legend. By default the y-axis renders as ``$``-prefixed currency; pass
    ``currency_axis=None`` to leave both axes untouched.
    """
    fig.update_layout(
        template=TEMPLATE,
        colorway=PALETTE,
        height=height,
        margin=dict(l=10, r=10, t=42, b=10),
        font=dict(family="Inter, system-ui, sans-serif", size=13, color="#1B2A36"),
        legend=dict(
            orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0, title_text=""
        ),
        hoverlabel=dict(bgcolor="white", font_size=12),
        plot_bgcolor="rgba(0,0,0,0)",
        bargap=0.55,
    )
    if currency_axis in ("y", "both"):
        fig.update_yaxes(tickprefix="$", tickformat=",.0f", title_text="")
    if currency_axis in ("x", "both"):
        fig.update_xaxes(tickprefix="$", tickformat=",.0f", title_text="")
    if currency_axis == "y":
        fig.update_xaxes(title_text="")
    return fig


def section_title(text: str) -> None:
    """Render a chart/section title above a plot (kept out of the figure itself)."""
    st.markdown(f"##### {text}")


def compact_money(v: float) -> str:
    """Format a dollar amount compactly for KPI cards — ``$20K`` / ``$1.2M`` / ``$950``.

    Trailing ``.0`` is trimmed so round magnitudes read clean (``$20K`` not
    ``$20.0K``); below 1,000 it falls back to a plain ``$``-grouped integer.
    """
    a = abs(v)
    for cutoff, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if a >= cutoff:
            scaled = f"{v / cutoff:.1f}".rstrip("0").rstrip(".")
            return f"${scaled}{suffix}"
    return f"${v:,.0f}"


def plotly(fig: go.Figure, *, title: str | None = None, key: str | None = None) -> None:
    """Render a styled figure (optional title above it), modebar hidden, stretched."""
    if title:
        section_title(title)
    st.plotly_chart(fig, width="stretch", config={"displayModeBar": False}, key=key)


# ---------------------------------------------------------------------------
# shadcn building blocks
# ---------------------------------------------------------------------------
def kpi_cards(cards: Sequence[tuple[str, str, str]], *, key: str) -> None:
    """Render a row of accent KPI cards from ``(title, value, sub-text)`` tuples.

    Custom HTML (not shadcn ``metric_card``) for a bolder look — a teal accent rule,
    a large coloured value, and an uppercase label — and to avoid the iframe tooltip
    leak shadcn components carry.
    """
    cols = st.columns(len(cards))
    for col, (title, content, description) in zip(cols, cards, strict=True):
        with col:
            st.markdown(
                f'<div class="aura-kpi"><div class="aura-kpi-t">{title}</div>'
                f'<div class="aura-kpi-v">{content}</div>'
                f'<div class="aura-kpi-s">{description}</div></div>',
                unsafe_allow_html=True,
            )


def tabs(options: list[str], *, key: str, default: str | None = None) -> str:
    """Render shadcn tabs and return the active option."""
    active = ui.tabs(options=options, default_value=default or options[0], key=key)
    return str(active) if active is not None else options[0]


def month_filter(months: Iterable[object], *, key: str, label: str = "Charge month") -> str | None:
    """Sidebar month selector defaulting to the most recent month (None if empty)."""
    opts = sorted({str(m) for m in months})
    if not opts:
        return None
    return st.sidebar.selectbox(label, options=opts, index=len(opts) - 1, key=key)


def date_range(
    lo: date,
    hi: date,
    *,
    key: str,
    label: str = "Date range",
    default_lo: date | None = None,
    default_hi: date | None = None,
) -> tuple[date, date]:
    """Sidebar from→to date picker bounded by ``[lo, hi]``.

    Defaults to ``[default_lo or lo, default_hi or hi]`` — pass ``default_lo`` to open
    on the first *material* month while still letting the user expand back to ``lo``.
    Defaults are clamped into ``[lo, hi]`` (a ``default_lo`` derived from a month-start
    can fall before ``lo`` when data begins mid-month — Streamlit rejects an
    out-of-bounds default). ``st.date_input`` returns a 1-tuple mid-edit (only the
    start picked); we hold the end until the range is complete so downstream queries
    never see a half-open range.
    """

    def _clamp(d: date) -> date:
        return min(max(d, lo), hi)

    val = st.sidebar.date_input(
        label,
        value=(_clamp(default_lo or lo), _clamp(default_hi or hi)),
        min_value=lo,
        max_value=hi,
        key=key,
        format="YYYY-MM-DD",
    )
    seq = list(val) if isinstance(val, tuple | list) else [val]
    start = seq[0] if len(seq) >= 1 else lo
    finish = seq[1] if len(seq) >= 2 else hi
    return start, finish


def shadcn_table(
    df: pd.DataFrame,
    *,
    key: str,
    money_cols: Sequence[str] = (),
    pct_cols: Sequence[str] = (),
    int_cols: Sequence[str] = (),
    num_cols: Sequence[str] = (),
    rename: dict[str, str] | None = None,
    max_height: int = 460,
) -> None:
    """Render a DataFrame as a shadcn table with currency/percent/number formatting.

    shadcn's table renders values verbatim, so numeric columns are pre-formatted
    to strings here (``$1,234`` / ``12.3%`` / ``1,000`` / ``1,234.56``) — top-N
    tables already arrive sorted, so losing numeric sort on these columns is fine.
    """
    disp = df.copy()
    for col in money_cols:
        if col in disp:
            disp[col] = disp[col].map(lambda v: f"${v:,.0f}" if pd.notna(v) else "—")
    for col in pct_cols:
        if col in disp:
            disp[col] = disp[col].map(lambda v: f"{v:.1f}%" if pd.notna(v) else "—")
    for col in int_cols:
        if col in disp:
            disp[col] = disp[col].map(lambda v: f"{v:,.0f}" if pd.notna(v) else "—")
    for col in num_cols:
        if col in disp:
            disp[col] = disp[col].map(lambda v: f"{v:,.2f}" if pd.notna(v) else "—")
    if rename:
        disp = disp.rename(columns=rename)
    ui.table(data=disp, key=key, maxHeight=max_height)


def html_table(
    df: pd.DataFrame,
    *,
    money_cols: Sequence[str] = (),
    pct_cols: Sequence[str] = (),
    int_cols: Sequence[str] = (),
    num_cols: Sequence[str] = (),
    rename: dict[str, str] | None = None,
) -> None:
    """Render a DataFrame as a styled inline HTML table (no shadcn iframe).

    Same currency/percent/number formatting as :func:`shadcn_table`, but rendered via
    the pandas Styler's HTML so it sits on the page directly — cohesive with
    :func:`heatmap_table` and free of the iframe ``title`` tooltip shadcn carries.
    """
    disp = df.copy()
    if rename:
        disp = disp.rename(columns=rename)

    def _name(col: str) -> str:
        return rename.get(col, col) if rename else col

    fmt: dict[str, object] = {}
    for col in money_cols:
        if _name(col) in disp.columns:
            fmt[_name(col)] = lambda v: f"${v:,.0f}" if pd.notna(v) else "—"
    for col in pct_cols:
        if _name(col) in disp.columns:
            fmt[_name(col)] = lambda v: f"{v:.1f}%" if pd.notna(v) else "—"
    for col in int_cols:
        if _name(col) in disp.columns:
            fmt[_name(col)] = lambda v: f"{v:,.0f}" if pd.notna(v) else "—"
    for col in num_cols:
        if _name(col) in disp.columns:
            fmt[_name(col)] = lambda v: f"{v:,.2f}" if pd.notna(v) else "—"

    styler = (
        disp.style.format(fmt, na_rep="—")
        .hide(axis="index")
        .set_table_attributes('class="auralake-heat"')
    )
    st.markdown(_HEAT_TABLE_CSS + styler.to_html(), unsafe_allow_html=True)


def _heat_color(val: float, cap: float) -> str:
    """Diverging cell background for a signed % — red for increases, green for drops.

    ``cap`` is the magnitude (in %) that saturates the shade; values past it clamp.
    Returns a CSS ``background-color`` declaration (empty for NaN).
    """
    if pd.isna(val):
        return ""
    frac = max(-1.0, min(1.0, val / cap)) if cap else 0.0
    r, g, b = (214, 84, 72) if frac >= 0 else (74, 159, 100)
    alpha = 0.10 + 0.55 * abs(frac)
    return f"background-color: rgba({r}, {g}, {b}, {alpha:.2f})"


_HEAT_TABLE_CSS = """
<style>
table.auralake-heat { width: 100%; border-collapse: collapse;
  font-family: Inter, system-ui, sans-serif; font-size: 13px; }
table.auralake-heat th { text-align: right; padding: 8px 12px; background: #f8fafc;
  color: #64748b; font-weight: 600; border-bottom: 2px solid #e2e8f0; }
table.auralake-heat td { text-align: right; padding: 7px 12px; color: #1B2A36;
  border-bottom: 1px solid #eef2f6; }
table.auralake-heat th:first-child, table.auralake-heat td:first-child { text-align: left; }
</style>
"""


def heatmap_table(
    df: pd.DataFrame,
    *,
    heat_col: str,
    money_cols: Sequence[str] = (),
    heat_cap: float = 50.0,
    rename: dict[str, str] | None = None,
) -> None:
    """Render a DataFrame as an HTML heatmap table (Grafana-style MoM coloring).

    Unlike :func:`shadcn_table`, ``heat_col`` cells get a red (increase) / green
    (decrease) background scaled by magnitude (capped at ``heat_cap`` %). It renders
    via the pandas Styler's own HTML (``st.markdown(..., unsafe_allow_html=True)``),
    not ``st.dataframe`` — ``st.dataframe`` honors a Styler's cell *colors* but draws
    the numeric grid itself, dropping ``.format`` and showing ``NaN`` as ``None``; the
    Styler HTML keeps both the colors and the ``$``/``±%``/``—`` formatting, and being
    inline (not a shadcn iframe) its styles apply on the page. Top-N tables only.
    """
    disp = df.copy()
    if rename:
        disp = disp.rename(columns=rename)

    def _name(col: str) -> str:
        return rename.get(col, col) if rename else col

    money = [_name(c) for c in money_cols if _name(c) in disp.columns]
    heat = _name(heat_col)
    fmt: dict[str, object] = {c: (lambda v: f"${v:,.0f}" if pd.notna(v) else "—") for c in money}
    if heat in disp.columns:
        fmt[heat] = lambda v: f"{v:+.1f}%" if pd.notna(v) else "—"

    styler = (
        disp.style.format(fmt, na_rep="—")
        .hide(axis="index")
        .set_table_attributes('class="auralake-heat"')
    )
    if heat in disp.columns:
        styler = styler.map(lambda v: _heat_color(v, heat_cap), subset=[heat])

    st.markdown(_HEAT_TABLE_CSS + styler.to_html(), unsafe_allow_html=True)


def inject_css() -> None:
    """Page-level polish: density, accent headings, and the custom KPI cards."""
    st.markdown(
        f"""
        <style>
        .block-container {{ padding-top: 1.8rem; max-width: 1320px; }}
        h1 {{ font-weight: 750; letter-spacing: -0.015em; color: #13405E; }}
        h2, h3 {{ font-weight: 650; color: #1B2A36; }}
        section[data-testid="stSidebar"] {{ background: #f3f7fa; }}
        /* Accent rule on every section/chart heading (rendered as h5). Clear top
           margin so a heading/chart never touches the block above it. */
        .block-container h5 {{
            border-left: 3px solid {ACCENT}; padding-left: 10px; margin: 22px 0 2px;
            font-weight: 650; color: #1B2A36; letter-spacing: -0.01em;
        }}
        /* A caption sits snug under its heading but with air before the chart. */
        .block-container div[data-testid="stCaptionContainer"] {{ margin: 0 0 10px; }}
        /* Comfortable vertical rhythm between blocks. */
        .block-container [data-testid="stVerticalBlock"] {{ gap: 0.85rem; }}
        hr {{ margin: 0.4rem 0 1rem; }}
        /* KPI cards. */
        .aura-kpi {{
            background: #fff; border: 1px solid #e6edf3; border-top: 3px solid {ACCENT};
            border-radius: 10px; padding: 13px 16px 11px;
            box-shadow: 0 1px 2px rgba(16,42,54,0.05);
        }}
        .aura-kpi-t {{
            color: #64748b; font-size: 11px; font-weight: 600;
            text-transform: uppercase; letter-spacing: 0.05em;
        }}
        .aura-kpi-v {{
            color: {ACCENT}; font-size: 30px; font-weight: 760; line-height: 1.15; margin: 3px 0;
        }}
        .aura-kpi-s {{ color: #94a3b8; font-size: 12px; }}
        </style>
        """,
        unsafe_allow_html=True,
    )
