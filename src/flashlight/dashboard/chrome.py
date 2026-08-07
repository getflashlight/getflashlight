"""Shared NiceGUI visual language — dark-mode tokens, panels, KPI cards, Plotly
styling, and table helpers used by every page in ``dashboard/views/``.

Generalized from the Databricks-only prototype (``nicegui_app.py``, since folded
into the real app) — ink/surface/gridline colors and the emphasis/categorical color
rules follow the dataviz skill's validated dark-mode tokens (``references/palette.md``),
not invented colors: no glow, no tabular-nums on hero numbers, no decorative accent
bars. The app is dark-only (see the migration plan) — there's no light-mode variant.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import date
from typing import Any

import pandas as pd
import plotly.graph_objects as go
from nicegui import ui

# ── Chart chrome & ink — dark-mode tokens (dataviz skill, references/palette.md) ──
PAGE = "#0d0d0d"
SURFACE = "#1a1a19"
INK_PRIMARY = "#ffffff"
INK_SECONDARY = "#c3c2b7"
INK_MUTED = "#898781"
GRIDLINE = "#2c2c2a"
BASELINE = "#383835"
BORDER = "rgba(255,255,255,0.10)"
DEEMPHASIS = "#454542"
ACCENT = "#3987e5"  # categorical slot 1 (blue)
OPPORTUNITY = "#199e70"  # categorical slot 2 (aqua) — kept visually distinct from WASTE
WASTE = "#e66767"  # categorical slot 6 (red)
FONT_STACK = 'system-ui,-apple-system,"Segoe UI",sans-serif'

# Fixed categorical palette slots — color follows identity (SKU, provider, …), never rank.
CATEGORICAL_SLOTS: tuple[str, ...] = (
    ACCENT, OPPORTUNITY, "#c98500", "#008300", "#9085e9", WASTE, "#d55181", "#d95926",
)

# Cool slate for projected (unmeasured) series — sits off the categorical palette so a
# forecast bar can't be mistaken for a service segment, and reads clearer than muted ink
# against the dark surface (hatched grey was disappearing into the panel).
FORECAST = "#8fa3b8"

# Semantic hues — dark-mode counterpart of theme.SEMANTIC, same keys.
SEMANTIC: dict[str, str] = {
    "increase": WASTE,
    "decrease": OPPORTUNITY,
    "neutral": ACCENT,
    "savings": OPPORTUNITY,
    "paid": ACCENT,
    "unattributed": "#c98500",
    "volume": "#9085e9",
    "rate": "#c98500",
    "partial": INK_MUTED,
    "forecast": FORECAST,
}

# A plain vector monogram, not NiceGUI's emoji-to-SVG-text favicon helper — that
# renders the glyph via a generic font-family, which some browsers (Safari) fail to
# substitute with a color-emoji font and show a generic placeholder icon instead.
FAVICON_SVG = (
    f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
    f'<rect width="64" height="64" rx="14" fill="{ACCENT}"/>'
    f'<text x="32" y="45" font-size="34" font-family="Arial,sans-serif" '
    f'font-weight="700" fill="#ffffff" text-anchor="middle">F</text></svg>'
)

# Declared so Safari (and iOS "Add to Home Screen") uses this instead of probing
# /apple-touch-icon.png at the site root on spec. Both point at the same route —
# see router.register_icon_routes, which is what stops those probes 404ing.
HEAD_ICONS = (
    '<link rel="apple-touch-icon" href="/apple-touch-icon.png">'
    '<link rel="icon" type="image/svg+xml" href="/favicon.ico">'
)

HEAD_CSS = f"""
<style>
body, .q-page, .nicegui-content {{
    background: {PAGE} !important;
    color: {INK_PRIMARY};
    font-family: {FONT_STACK};
}}
/* A full-height page (assistant) owns its vertical space: nothing above the
   transcript scrolls or pads, and the transcript itself scrolls instead.
   Deliberately no hardcoded header height: .q-page-container is border-box and
   already carries padding-top equal to the *real* header height, so
   height:100vh on it leaves exactly the space under the header, and .q-page
   takes 100% of that. Duplicating the header's pixel height here would drift
   the moment its padding changes.
   .nicegui-content needs flex:1 explicitly — NiceGUI ships it as a padded flex
   column that sizes to its content, so without this the transcript's scroll
   area collapses to zero height and the composer rides up under the top bar.
   min-height:0 lets these flex children shrink below content so the inner
   scroll area is actually bounded. Scoped to .fl-full-height so every
   report-style scroll-the-page view is untouched. */
.fl-full-height .q-page-container {{ height: 100vh; }}
.fl-full-height .q-page {{ height: 100%; display: flex; flex-direction: column; overflow: hidden; }}
.fl-full-height .nicegui-content {{
    flex: 1; min-height: 0; padding: 0 !important; gap: 0 !important;
}}
.fl-table .q-table__card {{ background: transparent !important; box-shadow: none !important; }}
.fl-table thead th {{
    color: {INK_MUTED} !important; font-size: 12px !important; font-weight: 600 !important;
    border-bottom: 1px solid {BORDER} !important; background: transparent !important;
}}
.fl-table tbody td {{
    color: {INK_SECONDARY} !important; border-bottom: 1px solid {GRIDLINE} !important;
}}
.fl-table-clickable tbody tr {{ cursor: pointer; }}
.fl-table tbody tr:hover td {{ background: rgba(255,255,255,0.03) !important; }}
/* ui.code's copy button is CSS-positioned `top: 0.5rem` (nicegui.css), which reads fine
   against a multi-line block but sits visibly above-center on a single-line one (see
   mcp_server.py's "quick add" command). Center it vertically for that one shape only —
   untouched everywhere else. */
.fl-code-oneline .nicegui-code-copy {{ top: 50%; transform: translateY(-50%); }}
</style>
"""


def _q(value: str) -> str:
    """Escape a string for inlining as a single-quoted SQL literal."""
    return value.replace("'", "''")


# ── Panels / KPI cards ──────────────────────────────────────────────────────
def panel() -> ui.card:
    """A flat, hairline-bordered panel — the one visual unit (KPI, chart, table).

    No accent bar, no shadow-as-decoration: identity lives in the data (a chart
    trace's color), not the card chrome — see the dataviz skill's anti-pattern list.
    """
    return ui.card().tight().classes("w-full").style(
        f"background:{SURFACE};border:1px solid {BORDER};border-radius:10px;padding:18px 20px;"
        "min-width:0;overflow-x:auto;"
    )


def panel_title(text: str) -> None:
    ui.label(text).classes("text-sm font-medium mb-2").style(f"color:{INK_SECONDARY}")


def section_title(text: str) -> None:
    ui.label(text).classes("text-lg font-semibold").style(f"color:{INK_PRIMARY}")


def section_caption(text: str) -> None:
    ui.label(text).classes("text-xs").style(f"color:{INK_MUTED}")


def info_icon(tooltip: str) -> None:
    """A small hoverable (i) carrying a methodology note — *why*/*how computed*, not the
    finding itself. Use next to a title or caption so the finding stays a one-line scan
    and the reasoning behind it is one hover away instead of permanent paragraph text.
    """
    ui.icon("info", size="14px").style(f"color:{DEEMPHASIS};cursor:help;").tooltip(tooltip)


def caption_info(text: str, tooltip: str) -> None:
    """A :func:`section_caption` with a trailing :func:`info_icon` for supporting detail
    (definitions, edge cases, "why this number") that doesn't need to be read every time.
    """
    with ui.row().classes("items-center gap-1"):
        section_caption(text)
        info_icon(tooltip)


def kpi(title: str, value: str, sub: str, *, color: str = INK_PRIMARY) -> None:
    with panel():
        ui.label(title).classes("text-sm").style(f"color:{INK_MUTED}")
        ui.label(value).classes("text-3xl font-semibold").style(
            f"color:{color};line-height:1.2;margin:4px 0 2px;"
        )
        if sub:
            ui.label(sub).classes("text-xs").style(f"color:{INK_MUTED}")


KpiCard = tuple[str, str, str] | tuple[str, str, str, str]


def kpi_row(cards: Sequence[KpiCard], *, columns: int | None = None) -> None:
    """A row of KPI cards from ``(title, value, sub[, variant-or-hex])`` tuples.

    The optional fourth element is a :data:`SEMANTIC` key or a ``#RRGGBB`` override.
    """
    with ui.grid(columns=columns or len(cards)).classes("w-full gap-4"):
        for card in cards:
            title, value, sub = card[0], card[1], card[2]
            variant = card[3] if len(card) > 3 else None
            color = variant if variant and variant.startswith("#") else SEMANTIC.get(
                variant or "", INK_PRIMARY
            )
            kpi(title, value, sub, color=color)


def stat(title: str, value: str, sub: str, *, color: str = INK_PRIMARY) -> None:
    """One ``(title, value, sub)`` block, same content shape as :func:`kpi` but with no
    card chrome of its own — for grouping several numbers *inside* an existing
    :func:`panel` (e.g. as a table's header strip) instead of giving each its own
    bordered container. Use :func:`kpi_row` when the numbers are the whole panel;
    use :func:`stat_row` when they're context for a table that follows in the same card.
    """
    with ui.column().classes("gap-0"):
        ui.label(title).classes("text-xs").style(f"color:{INK_MUTED}")
        ui.label(value).classes("text-2xl font-semibold").style(
            f"color:{color};line-height:1.2;margin:2px 0;"
        )
        if sub:
            ui.label(sub).classes("text-xs").style(f"color:{INK_MUTED};max-width:220px;")


def stat_row(cards: Sequence[KpiCard]) -> None:
    """A row of undecorated :func:`stat` blocks — the same ``(title, value, sub[,
    variant])`` tuple shape as :func:`kpi_row`, for a panel where the table is the
    point and these numbers are its header, not a KPI section of their own.
    """
    with ui.row().classes("w-full gap-8 flex-wrap mb-1"):
        for card in cards:
            title, value, sub = card[0], card[1], card[2]
            variant = card[3] if len(card) > 3 else None
            color = variant if variant and variant.startswith("#") else SEMANTIC.get(
                variant or "", INK_PRIMARY
            )
            stat(title, value, sub, color=color)


def empty_state(
    icon: str,
    title: str,
    caption: str,
    *,
    button_label: str | None = None,
    on_click: Callable[[], object] | None = None,
) -> None:
    """A centered icon + title + caption + optional CTA, for a section with
    nothing in it yet — first-run guidance instead of a bare caption line
    ("No connections yet — add one below."), which reads like an error rather
    than an invitation.
    """
    with ui.column().classes("w-full items-center gap-2").style("padding:32px 0;"):
        ui.icon(icon, size="2rem").style(f"color:{DEEMPHASIS}")
        ui.label(title).classes("text-sm font-medium").style(f"color:{INK_SECONDARY}")
        ui.label(caption).classes("text-xs").style(f"color:{INK_MUTED};max-width:420px;text-align:center;")
        if button_label and on_click:
            ui.button(button_label, icon="add", on_click=on_click).props(
                "flat no-caps color=primary"
            ).classes("mt-1")


def status_badge(enabled: bool, *, labels: tuple[str, str] = ("Enabled", "Disabled")) -> None:
    """A colored-dot + label pill instead of plain colored text, so status reads as a
    status at a glance (same dot-plus-label shape as most connector/integration lists
    elsewhere).

    *labels* is ``(on, off)`` — Enabled/Disabled for a configured thing, Running/Stopped
    for a process (see ``views/mcp_server.py``). Only the wording varies: green-for-on
    and muted-for-off stay put, so the dot means the same thing on every page.
    """
    color = OPPORTUNITY if enabled else INK_MUTED
    with ui.row().classes("items-center gap-1.5"):
        ui.element("div").style(
            f"width:6px;height:6px;border-radius:50%;background:{color};flex:none;"
        )
        ui.label(labels[0] if enabled else labels[1]).classes("text-xs").style(f"color:{color}")


def provider_card(
    *,
    name: str,
    amount: str,
    delta_text: str,
    color: str,
    delta_color: str,
    href: str | None = None,
    note: str | None = None,
) -> None:
    """Home-page provider shortcut card with optional link to its own page."""
    with panel().style(f"border-left:3px solid {color};"):
        ui.label(name).classes("text-sm font-medium").style(f"color:{INK_PRIMARY}")
        ui.label(amount).classes("text-2xl font-semibold").style(
            f"color:{color};line-height:1.2;margin:2px 0;"
        )
        ui.label(delta_text).classes("text-xs").style(f"color:{delta_color}")
        if note:
            ui.label(note).classes("text-xs").style(f"color:{INK_MUTED}")
        if href:
            ui.link(f"Open {name} →", href).classes("text-xs").style(f"color:{ACCENT}")


# ── Plotly ───────────────────────────────────────────────────────────────────
MAX_SERIES = len(CATEGORICAL_SLOTS)
OTHER_SERIES = "Other"


def cap_series(df: pd.DataFrame, series: str, y_col: str) -> pd.DataFrame:
    """Fold all but the top ``MAX_SERIES - 1`` values of *series* into one "Other"
    bucket, so the legend stays readable and every slot keeps a distinct colour.
    Totals are preserved — nothing is dropped, only grouped.

    Shared by every stacked chart whose series count is data-driven (an Assistant
    reply's arbitrary result set, a provider's service list): there are only
    ``len(CATEGORICAL_SLOTS)`` hues, and past that Plotly starts recycling them,
    which reads as two segments being the same thing.
    """
    totals = df.groupby(series)[y_col].sum().sort_values(ascending=False)
    if len(totals) <= MAX_SERIES:
        return df
    keep = set(totals.index[: MAX_SERIES - 1])
    return df.assign(**{series: df[series].where(df[series].isin(keep), OTHER_SERIES)})


def style_fig(
    fig: go.Figure,
    *,
    height: int = 260,
    has_legend: bool = False,
    currency_axis: str | None = "y",
    category_x: bool = False,
    title: str | None = None,
) -> go.Figure:
    """Flat, quiet chart chrome: transparent canvas, hairline grid, muted axis text.

    ``category_x=True`` forces a categorical x-axis — pass it for bar charts keyed
    by a "YYYY-MM" month string: Plotly Express auto-detects that shape as a date
    axis and picks its own tick positions, which can land between bars instead of
    under them (e.g. showing "May 31" under what is actually the June bar). Leave
    it False for a real continuous date axis (e.g. daily spend by `charge_day`),
    where date-aware spacing/zoom is the wanted behavior.

    ``title`` names what's plotted directly on the chart — every other chart in
    this dashboard sits under a section heading that already says so, but a chart
    with no surrounding heading (e.g. one dropped into an Assistant reply) needs its own.
    """
    fig.update_layout(
        template="plotly_dark",
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        height=height,
        margin=dict(l=8, r=8, t=28 if (has_legend or title) else 4, b=8),
        font=dict(family=FONT_STACK, size=12, color=INK_MUTED),
        showlegend=has_legend,
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0, title_text="")
        if has_legend
        else None,
        hoverlabel=dict(bgcolor=SURFACE, bordercolor=BORDER, font_size=12, font_color=INK_PRIMARY),
        bargap=0.5,
        title=dict(text=title, font=dict(size=13, color=INK_SECONDARY), x=0, xanchor="left")
        if title
        else None,
    )
    fig.update_xaxes(
        showgrid=False, zeroline=False, linecolor=BASELINE, color=INK_MUTED, title_text=""
    )
    fig.update_yaxes(
        showgrid=True, gridcolor=GRIDLINE, zeroline=False, color=INK_MUTED, title_text=""
    )
    if currency_axis in ("y", "both"):
        fig.update_yaxes(tickprefix="$", tickformat=",.0f")
    if currency_axis in ("x", "both"):
        fig.update_xaxes(tickprefix="$", tickformat=",.0f")
    if category_x:
        fig.update_xaxes(type="category")
    return fig


def plot(fig: go.Figure) -> ui.plotly:
    return ui.plotly(fig).classes("w-full")


# ── Date range ───────────────────────────────────────────────────────────────
DateState = dict[str, date]


def month_where(col: str, state: DateState) -> str:
    """A month-grain column is in range if its month overlaps [start, end] at all."""
    start, end = f"DATE '{state['start']}'", f"DATE '{state['end']}'"
    return f"{col} BETWEEN date_trunc('month', {start}) AND date_trunc('month', {end})"


def day_where(col: str, state: DateState) -> str:
    return f"{col} BETWEEN DATE '{state['start']}' AND DATE '{state['end']}'"


def months_back(end: date, months: int) -> date:
    """Snap to the 1st of the month *months* back from *end* — GOLD is month-grain."""
    ts = pd.Timestamp(end) - pd.DateOffset(months=months)
    return date(ts.year, ts.month, 1)


def year_start(end: date) -> date:
    """Jan 1 of *end*'s year — the YTD anchor.

    One definition shared by the ``YTD`` quick range and the pages' *default* window, so
    the two can't drift. Keyed off the data's last month, not today: a lake whose latest
    bill is December of last year should open on that year, not on an empty January.
    """
    return date(end.year, 1, 1)


def _format_range(state: DateState) -> str:
    s, e = state["start"], state["end"]
    if s.year == e.year:
        return f"{s:%b %-d} – {e:%b %-d, %Y}"
    return f"{s:%b %-d, %Y} – {e:%b %-d, %Y}"


_QUICK_RANGES = (("1mo", 1), ("3mo", 3), ("6mo", 6), ("12mo", 12), ("YTD", "ytd"), ("All", None))


def date_range_control(date_state: DateState, on_change: Callable[[], object]) -> None:
    """A collapsed time-picker: one small trigger button, a popover on click.

    Quick ranges apply immediately. Start and end are two separate calendars, each
    showing one unambiguous date, rather than one calendar in range-select mode —
    clearer about which end you're changing.
    """
    trigger = ui.button(_format_range(date_state), icon="event").props("flat dense no-caps").style(
        f"color:{INK_SECONDARY};background:{SURFACE};border:1px solid {BORDER};"
        "border-radius:8px;font-size:12px;padding:4px 12px;"
    )
    with trigger, ui.menu().props("anchor='bottom right' self='top right'") as menu:
        with ui.row().classes("gap-3 p-2 items-start").style(f"background:{SURFACE};"):
            with ui.column().classes("gap-0"):
                for text, months in _QUICK_RANGES:

                    def _quick(months: int | str | None = months) -> None:
                        end = date_state["bounds_max"]
                        if isinstance(months, str):
                            start = max(date_state["bounds_min"], year_start(end))
                        elif months is None:
                            start = date_state["bounds_min"]
                        else:
                            start = max(date_state["bounds_min"], months_back(end, months))
                        date_state["start"], date_state["end"] = start, end
                        start_calendar.value = start.isoformat()
                        end_calendar.value = end.isoformat()
                        trigger.text = _format_range(date_state)
                        menu.close()
                        on_change()

                    ui.button(text, on_click=_quick).props("flat dense no-caps align=left").style(
                        f"color:{INK_SECONDARY};min-width:72px;justify-content:flex-start;"
                    )

            with ui.column().classes("gap-1"):
                ui.label("Start date").classes("text-xs").style(f"color:{INK_MUTED}")
                start_calendar = ui.date(value=date_state["start"].isoformat()).props("minimal")
            with ui.column().classes("gap-1"):
                ui.label("End date").classes("text-xs").style(f"color:{INK_MUTED}")
                end_calendar = ui.date(value=date_state["end"].isoformat()).props("minimal")

            def _apply() -> None:
                start_value, end_value = start_calendar.value, end_calendar.value
                if not start_value or not end_value:
                    return
                start, end = date.fromisoformat(start_value), date.fromisoformat(end_value)
                if start > end:
                    ui.notify("Start date must be before end date", type="warning")
                    return
                date_state["start"], date_state["end"] = start, end
                trigger.text = _format_range(date_state)
                menu.close()
                on_change()

        with ui.row().classes("justify-end w-full p-2 pt-0"):
            ui.button("Apply", on_click=_apply).props("flat dense no-caps").style(
                f"color:{ACCENT};"
            )


# ── Tables ───────────────────────────────────────────────────────────────────
def _fmt_columns(
    df: pd.DataFrame,
    *,
    money_cols: Sequence[str] = (),
    pct_cols: Sequence[str] = (),
    int_cols: Sequence[str] = (),
    num_cols: Sequence[str] = (),
    rename: dict[str, str] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Format a DataFrame's money/pct/int/num columns to display strings.

    Returns ``(quasar_columns, rows)`` for :func:`nicegui.ui.table`. Non-numeric
    columns pass through as plain strings ("—" for NA).
    """
    disp = df.rename(columns=rename) if rename else df.copy()

    def _name(col: str) -> str:
        return (rename or {}).get(col, col)

    money = {_name(c) for c in money_cols}
    pct = {_name(c) for c in pct_cols}
    ints = {_name(c) for c in int_cols}
    nums = {_name(c) for c in num_cols}

    def _fmt(col: str, v: Any) -> str:
        if pd.isna(v):
            return "—"
        if col in money:
            return f"${float(v):,.0f}"
        if col in pct:
            return f"{float(v):.1f}%"
        if col in ints:
            return f"{float(v):,.0f}"
        if col in nums:
            return f"{float(v):,.2f}"
        return str(v)

    columns = [
        {
            "name": col,
            "label": col,
            "field": col,
            "align": "right" if col in money | pct | ints | nums else "left",
            "sortable": True,
        }
        for col in disp.columns
    ]
    rows = [
        {col: _fmt(col, row[col]) for col in disp.columns} for _, row in disp.iterrows()
    ]
    return columns, rows


def searchable_table(
    df: pd.DataFrame,
    *,
    key: str,
    search_col: str | None = None,
    money_cols: Sequence[str] = (),
    pct_cols: Sequence[str] = (),
    int_cols: Sequence[str] = (),
    num_cols: Sequence[str] = (),
    rename: dict[str, str] | None = None,
    max_rows: int | None = None,
    pagination: int = 20,
    on_row_click: Callable[[dict[str, Any]], None] | None = None,
) -> ui.table:
    """A paginated, optionally search-filterable ``ui.table`` with CSV export.

    ``max_rows`` caps the *displayed* rows; the CSV export always carries the
    full set. ``on_row_click`` gets the raw (pre-format) row dict.
    """
    shown = df if max_rows is None else df.head(max_rows)
    columns, rows = _fmt_columns(
        shown, money_cols=money_cols, pct_cols=pct_cols, int_cols=int_cols,
        num_cols=num_cols, rename=rename,
    )
    raw_rows = df.to_dict("records")

    with ui.row().classes("items-center justify-between w-full mb-2"):
        search: ui.input | None = None
        display_search_col = (rename or {}).get(search_col, search_col) if search_col else None
        if display_search_col and display_search_col in {c["name"] for c in columns}:
            search = (
                ui.input(f"Search {display_search_col}")
                .props("dense outlined clearable")
                .classes("w-64")
                .style(f"color:{INK_PRIMARY}")
            )
        if max_rows is not None and len(df) > max_rows:
            section_caption(
                f"Showing top {max_rows} of {len(df):,} — download the CSV for the full list."
            )
        ui.button(
            "Download CSV",
            icon="download",
            on_click=lambda: ui.download(df.to_csv(index=False).encode(), filename=f"{key}.csv"),
        ).props("flat dense no-caps").style(f"color:{ACCENT};")

    classes = "fl-table w-full" + (" fl-table-clickable" if on_row_click else "")
    table = ui.table(columns=columns, rows=rows, pagination=pagination).props("flat dense").classes(
        classes
    )
    if search is not None and display_search_col is not None:
        table.bind_filter_from(search, "value")
    if on_row_click is not None:

        def _handle_click(e: Any) -> None:
            idx = rows.index(e.args[1]) if e.args[1] in rows else None
            if idx is not None and idx < len(raw_rows):
                on_row_click(raw_rows[idx])

        table.on("rowClick", _handle_click)
    return table


def flat_table(
    df: pd.DataFrame,
    *,
    key: str,
    money_cols: Sequence[str] = (),
    pct_cols: Sequence[str] = (),
    int_cols: Sequence[str] = (),
    num_cols: Sequence[str] = (),
    rename: dict[str, str] | None = None,
) -> ui.table:
    """An unpaginated ``ui.table`` for a fully-visible pivot/summary table."""
    columns, rows = _fmt_columns(
        df, money_cols=money_cols, pct_cols=pct_cols, int_cols=int_cols,
        num_cols=num_cols, rename=rename,
    )
    return (
        ui.table(columns=columns, rows=rows)
        .props("flat dense hide-pagination")
        .classes(f"fl-table w-full {key}")
    )


def _heat_style(val: Any, cap: float) -> str:
    """Diverging cell background for a signed % — red for increases, green for drops."""
    if pd.isna(val):
        return ""
    frac = max(-1.0, min(1.0, float(val) / cap)) if cap else 0.0
    hex_color = SEMANTIC["increase"] if frac >= 0 else SEMANTIC["decrease"]
    h = hex_color.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    alpha = 0.10 + 0.55 * abs(frac)
    return f"background-color: rgba({r}, {g}, {b}, {alpha:.2f})"


def heatmap_table(
    df: pd.DataFrame,
    *,
    heat_col: str,
    key: str,
    money_cols: Sequence[str] = (),
    heat_cap: float = 50.0,
    rename: dict[str, str] | None = None,
) -> ui.table:
    """A flat table with ``heat_col`` cells background-tinted red (increase) / green
    (decrease), scaled by magnitude (capped at ``heat_cap`` %) — Grafana-style MoM
    coloring. Values past ``heat_cap`` clamp.

    The per-row CSS is precomputed in Python and carried on each row dict as
    ``_heat_style`` (not registered as a display column), then referenced from the
    Quasar cell slot via ``props.row._heat_style`` — simpler and safer than trying
    to interpolate a Python list into the Vue template string.
    """
    disp = df.rename(columns=rename) if rename else df.copy()
    heat_name = (rename or {}).get(heat_col, heat_col)

    columns, rows = _fmt_columns(
        disp, money_cols=money_cols, pct_cols=[heat_name] if heat_name in disp.columns else [],
    )
    if heat_name in disp.columns:
        for row, val in zip(rows, disp[heat_name], strict=True):
            row["_heat_style"] = _heat_style(val, heat_cap)

    table = (
        ui.table(columns=columns, rows=rows)
        .props("flat dense hide-pagination")
        .classes(f"fl-table w-full {key}")
    )
    if heat_name in disp.columns:
        table.add_slot(
            f"body-cell-{heat_name}",
            '<q-td :props="props" :style="props.row._heat_style">'
            "{{ props.value }}"
            "</q-td>",
        )
    return table
