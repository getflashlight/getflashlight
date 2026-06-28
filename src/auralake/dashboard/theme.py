"""Shared visual language for the dashboard — the one place for the palette, the
Plotly layout, and the UI building blocks (KPI cards, tables).

The views (:mod:`home_overview`, :mod:`tco_overview`, :mod:`provider_focus`) all
route their charts through :func:`style_fig` and their headline numbers / tables
through :func:`kpi_cards` and :func:`html_table`, so spend always reads the same
way — ``$``-prefixed axes, a lake-blue palette, and consistent card/table chrome.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from datetime import date
from typing import Any, Literal

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

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

# Stable provider identity — same hue everywhere (charts, KPIs, nav dots).
GROUP_COLORS: dict[str, str] = {
    "aws": "#FF9900",
    "databricks": "#0E7C86",
    "google": "#4285F4",
    "microsoft": "#0078D4",
}
PROVIDER_COLORS: dict[str, str] = {
    "AWS": GROUP_COLORS["aws"],
    "Amazon Web Services": GROUP_COLORS["aws"],
    "Databricks": GROUP_COLORS["databricks"],
    "Google Cloud": GROUP_COLORS["google"],
    "Google": GROUP_COLORS["google"],
    "Microsoft": GROUP_COLORS["microsoft"],
    "Microsoft Azure": GROUP_COLORS["microsoft"],
    "Azure": GROUP_COLORS["microsoft"],
}

# Semantic hues — encode direction, savings, and attribution honesty.
SEMANTIC: dict[str, str] = {
    "increase": PALETTE[6],  # rose — spend went up
    "decrease": PALETTE[4],  # green — spend went down
    "neutral": ACCENT,
    "savings": PALETTE[4],
    "paid": PALETTE[1],
    "unattributed": PALETTE[5],  # amber — needs attention
    "volume": PALETTE[2],  # sky — usage-driven change
    "rate": PALETTE[5],  # amber — price/mix change
    "partial": "#94a3b8",
}

KpiVariant = Literal[
    "default",
    "increase",
    "decrease",
    "neutral",
    "savings",
    "paid",
    "unattributed",
    "volume",
    "rate",
]
KpiCard = tuple[str, str, str] | tuple[str, str, str, KpiVariant | str]

TEMPLATE = "plotly_white"

# Layout rhythm — single inset shared by KPIs, panels, charts, and tables.
CONTENT_PAD_PX = 12  # aligns Plotly left margin with table cell padding
SECTION_GAP = "24px"


def _fig_has_legend(fig: go.Figure) -> bool:
    """True when the figure will render a visible legend above the plot area."""
    show = fig.layout.showlegend
    if show is False:
        return False
    names = [getattr(trace, "name", None) for trace in fig.data]
    return len([n for n in names if n]) > 1


def provider_color(*, label: str | None = None, group: str | None = None) -> str:
    """Return the stable brand color for a provider group or display label."""
    if group and group in GROUP_COLORS:
        return GROUP_COLORS[group]
    if label:
        if label in PROVIDER_COLORS:
            return PROVIDER_COLORS[label]
        low = label.lower()
        for key, color in PROVIDER_COLORS.items():
            if key.lower() in low or low in key.lower():
                return color
    return ACCENT


def provider_color_map(
    labels: Iterable[str], *, groups: Iterable[str] | None = None
) -> dict[str, str]:
    """Build a Plotly ``color_discrete_map`` for a set of provider labels."""
    label_list = list(labels)
    grp_list = list(groups) if groups is not None else [None] * len(label_list)
    return {
        label: provider_color(label=label, group=grp)
        for label, grp in zip(label_list, grp_list, strict=False)
    }


def rgba_hex(hex_color: str, alpha: float) -> str:
    """Convert ``#RRGGBB`` to an ``rgba(...)`` string for Plotly fills."""
    h = hex_color.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return f"rgba({r},{g},{b},{alpha})"


def delta_variant(delta: float) -> KpiVariant:
    """Map a signed dollar delta to an increase/decrease KPI variant."""
    if delta > 0:
        return "increase"
    if delta < 0:
        return "decrease"
    return "neutral"


def _kpi_colors(
    variant: KpiVariant | str | None, *, fallback: str = ACCENT
) -> tuple[str, str, str]:
    """Return ``(border, value, background)`` for a KPI card variant."""
    if variant and variant.startswith("#"):
        base = variant
    elif variant in SEMANTIC:
        base = SEMANTIC[str(variant)]
    else:
        base = fallback
    bg = rgba_hex(base, 0.07)
    return base, base, bg


# ---------------------------------------------------------------------------
# Plotly
# ---------------------------------------------------------------------------
def style_fig(
    fig: go.Figure,
    *,
    currency_axis: str | None = "y",
    height: int = 340,
    has_legend: bool | None = None,
) -> go.Figure:
    """Apply the shared Auralake look to a Plotly figure.

    Chart titles are rendered separately via :func:`section_title`. Top margin
    reserves legend space only when a legend is actually shown.
    """
    legend_on = _fig_has_legend(fig) if has_legend is None else has_legend
    top = 36 if legend_on else 8
    fig.update_layout(
        template=TEMPLATE,
        colorway=PALETTE,
        height=height,
        margin=dict(l=CONTENT_PAD_PX, r=8, t=top, b=8),
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


def section_title(text: str, *, flush: bool = False) -> None:
    """Render a section heading (scoped class — not all Streamlit h5 elements)."""
    mod = " aura-section-title--flush" if flush else ""
    st.markdown(
        f'<p class="aura-section-title{mod}">{text}</p>',
        unsafe_allow_html=True,
    )


def section_subtitle(text: str) -> None:
    """Render a drill-down / in-section subheading."""
    st.markdown(f'<p class="aura-subsection-title">{text}</p>', unsafe_allow_html=True)


def section_caption(text: str) -> None:
    """One-line context under a section title — supports Streamlit markdown."""
    st.caption(text)


@contextmanager
def panel(
    *,
    tone: Literal["default", "teal", "green", "amber"] = "default",
    flush: bool = False,
) -> Iterator[None]:
    """Section divider — colored top rule + consistent vertical rhythm, flush edges."""
    mod = " aura-panel-start--flush" if flush else ""
    st.markdown(
        f'<div class="aura-panel-start aura-panel-{tone}{mod}"></div>',
        unsafe_allow_html=True,
    )
    yield


def md_money(v: float, *, bold: bool = True) -> str:
    """Format dollars for ``st.markdown`` — escape ``$`` so Streamlit won't parse LaTeX."""
    text = f"\\${v:,.0f}"
    return f"**{text}**" if bold else text


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


def plotly(
    fig: go.Figure,
    *,
    title: str | None = None,
    title_flush: bool = False,
    key: str | None = None,
    on_select: bool = False,
) -> Any:
    """Render a styled figure (optional title above it), modebar hidden, stretched.

    With ``on_select`` the chart becomes click-selectable (point selection) and the
    Streamlit selection event is returned so the caller can drive a drill-down;
    otherwise nothing is returned. ``on_select`` requires ``key``.
    """
    if title:
        section_title(title, flush=title_flush)
    if on_select:
        return st.plotly_chart(
            fig,
            width="stretch",
            config={"displayModeBar": False},
            key=key,
            on_select="rerun",
            selection_mode="points",
        )
    st.plotly_chart(fig, width="stretch", config={"displayModeBar": False}, key=key)
    return None


# ---------------------------------------------------------------------------
# shadcn building blocks
# ---------------------------------------------------------------------------
def kpi_cards(
    cards: Sequence[KpiCard],
    *,
    key: str,
    accent: str = ACCENT,
    partial: bool = False,
) -> None:
    """Render a row of accent KPI cards from ``(title, value, sub-text[, variant])`` tuples.

    Optional fourth element is a :data:`KpiVariant` or a ``#RRGGBB`` override. Variants
    map to semantic colors — increase (rose), decrease (green), savings, unattributed
    (amber), etc. — so direction and meaning read at a glance. When ``partial`` is set
    (current month in range), cards render slightly muted.
    """
    cols = st.columns(len(cards), gap="medium")
    partial_cls = " aura-kpi-partial" if partial else ""
    for col, card in zip(cols, cards, strict=True):
        title, content, description = card[0], card[1], card[2]
        variant = card[3] if len(card) > 3 else "default"
        border, value, bg = _kpi_colors(variant, fallback=accent)
        with col:
            st.markdown(
                f'<div class="aura-kpi{partial_cls}" '
                f'style="border-top-color:{border};background:{bg}">'
                f'<div class="aura-kpi-t">{title}</div>'
                f'<div class="aura-kpi-v" style="color:{value}">{content}</div>'
                f'<div class="aura-kpi-s">{description}</div></div>',
                unsafe_allow_html=True,
            )


def provider_card(
    *,
    name: str,
    amount: str,
    delta_text: str,
    color: str,
    delta_color: str,
    page: Any | None = None,
    link_label: str | None = None,
) -> None:
    """Home-page provider shortcut card with optional navigation link."""
    st.markdown(
        f'<div class="aura-provider-card" style="border-left-color:{color}">'
        f'<div class="name">{name}</div>'
        f'<div class="amount" style="color:{color}">{amount}</div>'
        f'<div class="delta" style="color:{delta_color}">{delta_text}</div></div>',
        unsafe_allow_html=True,
    )
    if page is not None and link_label:
        st.page_link(page, label=link_label, icon="☁️")


def filterable_table(
    df: pd.DataFrame,
    *,
    filter_col: str,
    file_name: str,
    key: str,
    money_cols: Sequence[str] = (),
    pct_cols: Sequence[str] = (),
    int_cols: Sequence[str] = (),
    num_cols: Sequence[str] = (),
    rename: dict[str, str] | None = None,
    compact: bool = False,
) -> None:
    """Searchable table with CSV export — filter applies to ``filter_col`` before formatting."""
    raw = df.copy()
    show_chrome = not compact or len(raw) > 10
    if show_chrome:
        label = (rename or {}).get(filter_col, filter_col)
        q = st.text_input(
            f"Filter {label}",
            key=f"{key}_filter",
            placeholder=f"Search {label}…",
        )
        if q:
            raw = raw[raw[filter_col].astype(str).str.contains(q, case=False, na=False)]
        btn_col, _ = st.columns([1, 3])
        with btn_col:
            st.download_button(
                "Download CSV",
                raw.to_csv(index=False).encode(),
                file_name=file_name,
                mime="text/csv",
                key=f"{key}_csv",
            )
    html_table(
        raw,
        money_cols=money_cols,
        pct_cols=pct_cols,
        int_cols=int_cols,
        num_cols=num_cols,
        rename=rename,
    )


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

    Currency/percent/number formatting rendered via the pandas Styler's HTML so it
    sits on the page directly — cohesive with :func:`heatmap_table`.
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
    hex_color = SEMANTIC["increase"] if frac >= 0 else SEMANTIC["decrease"]
    h = hex_color.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
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

    Unlike :func:`html_table`, ``heat_col`` cells get a red (increase) / green
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
    """Page-level polish: spacing rhythm, section titles, KPI cards."""
    st.markdown(
        f"""
        <style>
        .block-container {{
            padding-top: 1.5rem;
            padding-bottom: 2rem;
            max-width: 1320px;
        }}
        h1 {{
            font-weight: 750; letter-spacing: -0.015em; color: #13405E;
            margin-bottom: 0.25rem;
        }}
        h2, h3 {{ font-weight: 650; color: #1B2A36; }}
        section[data-testid="stSidebar"] {{ background: #f3f7fa; }}
        /* Section headings — scoped class, not every Streamlit heading. */
        .aura-section-title {{
            border-left: 3px solid {ACCENT};
            padding-left: 10px;
            margin: {SECTION_GAP} 0 8px;
            font-size: 1.05rem;
            font-weight: 650;
            color: #1B2A36;
            letter-spacing: -0.01em;
            line-height: 1.3;
        }}
        .aura-section-title--flush {{ margin-top: 0; }}
        .aura-subsection-title {{
            margin: 16px 0 6px;
            font-size: 0.95rem;
            font-weight: 650;
            color: #334155;
        }}
        /* Tab panels — tighten gap between tab bar and content. */
        .stTabs [data-baseweb="tab-panel"] {{
            padding-top: 12px;
        }}
        /* Section panels — top rule separates blocks without nested box padding. */
        .aura-panel-start {{
            border-top: 3px solid #e2e8f0;
            margin: {SECTION_GAP} 0 12px;
            height: 0;
        }}
        .aura-panel-start--flush {{ margin-top: 0; }}
        .aura-panel-teal {{ border-top-color: {ACCENT}; }}
        .aura-panel-green {{ border-top-color: {SEMANTIC["savings"]}; }}
        .aura-panel-amber {{ border-top-color: {SEMANTIC["unattributed"]}; }}
        /* Captions Streamlit renders outside our section_caption helper. */
        .block-container div[data-testid="stCaptionContainer"] {{
            margin: -4px 0 12px;
        }}
        hr {{ margin: 0.4rem 0 1rem; }}
        /* Plotly charts — trim default iframe padding for alignment. */
        .stPlotlyChart {{
            margin-bottom: 4px;
        }}
        /* Equal-width KPI / chart columns on wide layouts. */
        div[data-testid="stHorizontalBlock"] > div[data-testid="column"] {{
            flex: 1 1 0;
            min-width: 0;
        }}
        div[data-testid="stHorizontalBlock"] .aura-kpi {{
            width: 100%;
        }}
        /* Partial-month badge. */
        .aura-badge {{
            display: inline-block; font-size: 11px; font-weight: 650;
            letter-spacing: 0.04em; text-transform: uppercase;
            padding: 3px 8px; border-radius: 999px;
            margin: 4px 0 12px;
        }}
        .aura-badge-partial {{
            color: {SEMANTIC["partial"]}; background: rgba(148,163,184,0.15);
            border: 1px solid rgba(148,163,184,0.35);
        }}
        /* Provider shortcut cards on Home. */
        .aura-provider-card {{
            background: #fff; border: 1px solid #e6edf3; border-left: 4px solid {ACCENT};
            border-radius: 10px; padding: 12px 14px 10px; min-height: 88px;
            box-shadow: 0 1px 2px rgba(16,42,54,0.05);
        }}
        .aura-provider-card .name {{ font-weight: 650; color: #1B2A36; margin-bottom: 2px; }}
        .aura-provider-card .amount {{ font-size: 22px; font-weight: 740; line-height: 1.2; }}
        .aura-provider-card .delta {{ color: #64748b; font-size: 12px; margin-top: 2px; }}
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
        .aura-kpi-partial {{ opacity: 0.88; }}
        .aura-kpi-partial .aura-kpi-v {{ opacity: 0.75; }}
        /* Sidebar context footer. */
        .aura-sidebar-meta {{
            color: #64748b; font-size: 12px; line-height: 1.5; margin-top: 8px;
        }}
        </style>
        """,
        unsafe_allow_html=True,
    )
