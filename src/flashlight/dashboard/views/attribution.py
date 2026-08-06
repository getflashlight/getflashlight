"""Attribution — who and what one provider's spend belongs to.

The "Attribution" tab on every provider page (``provider_focus``, ``redshift_focus``). Two
named sections (:func:`chrome.section_title`), because it answers two different questions
about the same bill and used to read as one undifferentiated stack of panels:

* **Tags** — *what* was it spent on. :func:`tag_coverage` (the honest denominator for
  everything below it), **Spend by tag key**
  (``<group>.spend_by_tag_key_month``, with case/separator variants folded together), and
  **Spend by tag value** (the chosen key's values, ``<group>.spend_by_tag_month``).
* **Ownership** — *who* spent it. **Owners** and **Projects**, recoverable waste ranked
  per person/service principal and per project tag (``efficiency.waste_by_owner_month``,
  its two ``owner_dimension`` values).

Renamed from "Owners & tags": that name described its two sections by enumerating their
panels rather than the question the tab answers, and it put "Owners" first even though tags
render first. "Attribution" is the one word for what both sections are doing.

This was a cross-provider ``/leaderboard`` page. It moved here because attribution answers a
question you ask *about a bill* — "who ran up this provider's spend?" — and the views it
reads are either already per-provider group (the tag views) or carry ``provider_name`` as a
column (``waste_by_owner_month``), so scoping is a filter, not a rewrite.

The design constraint throughout is that **unattributed spend is the finding**, not a row to
drop. On real data the single largest owner bucket is "no owner at all" (~$143k of shared
SQL-warehouse compute, which has no owner *by design*), and the project dimension is ~99%
unattributed. So every table leads with its Unattributed row and every panel states its own
denominator — a leaderboard that quietly ranked only the attributable remainder would be both
wrong and reassuring, which is the worst combination.

Ranking discipline: WASTE and OPPORTUNITY are never summed (different remedies), and
per-tag-key spend is never totalled (a resource with two tags is counted under both — the
honest denominator is ``spend_tag_coverage_month.tagged_cost``).

There is exactly **one** tag-coverage implementation, :func:`tag_coverage`, and it sums
``gross_cost``/``tagged_cost`` over the page's date range and divides once. The old
``/leaderboard`` page had a second one reading the single-month ``tagged_pct`` column; that
can't generalize to a range (averaging per-month percentages is wrong), and this tab lives
under the page's range picker. Its NULL-denominator honesty is folded in here instead.

Every finding still gets said out loud, but methodology (*why* a number is shaped that
way — folded spellings, per-key double counting, "unowned" being shared compute by design)
belongs behind :func:`chrome.info_icon`/:func:`chrome.caption_info`, not a standing
paragraph — this tab is a ranking, not a stats-methodology page. The one-line captions
that remain are the findings themselves (coverage %, unattributed $, spelling-collision
count); only their *explanation* moved to hover.

Density discipline, learned from a real render of this tab: a KPI-card row for tag
coverage (two cards stretched across a full-width grid) looked oversized and mostly empty
next to a one-line caption carrying the same two numbers — reverted. WASTE/OPPORTUNITY
used to each get their own table per owner dimension; :func:`_owner_table` renders both
lenses in one table with a "Lens" column instead. And each Owners/Projects KPI used to be
its own bordered card sitting *above* a separately-bordered table for the same finding —
ten containers on the tab in total. :func:`chrome.stat_row` (undecorated numbers, no card
of their own) now sits *inside* the same :func:`chrome.panel` as its table, so "waste &
opportunity by owner" is one card, not four. Four containers total now: two tag panels,
one owner panel, one project panel. None of this touches the honesty invariants above — the
numbers, the denominators, and the never-summed lenses are unchanged; only how many
cards/panels they're spread across is.
"""

from __future__ import annotations

from datetime import date

import pandas as pd
from nicegui import ui

from flashlight.dashboard import chrome
from flashlight.dashboard.data import gold_df, gold_view_published, provider_name_for_group
from flashlight.dashboard.theme import compact_money

_UNATTRIBUTED_KEY = "(unattributed)"

_STALE_MSG = (
    "This lake's published GOLD predates the owner-attribution view — run "
    "`flashlight transform` to rebuild it."
)

_OWNER_COLS = ["owner_display", "owner_kind", "lens", "recoverable_cost",
               "recoverable_cost_high_confidence", "billed_cost", "entity_count",
               "finding_count"]
_OWNER_RENAME = {
    "owner_display": "Owner",
    "owner_kind": "Kind",
    "lens": "Lens",
    "recoverable_cost": "Recoverable",
    "recoverable_cost_high_confidence": "High confidence",
    "billed_cost": "Billed",
    "entity_count": "Entities",
    "finding_count": "Findings",
}
_PROJECT_RENAME = {**_OWNER_RENAME, "owner_display": "Project"}
_KIND_LABELS = {
    "user": "Person",
    "service_principal": "Service principal",
    "project": "Project",
    "unattributed_shared_compute": "No owner (shared compute)",
    "unattributed": "No project tag",
}
# WASTE first: it's the more actionable lens (tune/right-size beats "move it" as a next
# step), and it's what the rule fires on most often on real data.
_LENS_LABELS = {"WASTE": "Waste — tune it", "OPPORTUNITY": "Opportunity — move it"}
_LENS_ORDER = {"WASTE": 0, "OPPORTUNITY": 1}

_TAG_COLS = ["tag_key_normalized", "tag_key_variants", "variant_count", "net_cost",
             "tag_value_count"]
_TAG_RENAME = {
    "tag_key_normalized": "Tag key",
    "tag_key_variants": "Spelled as",
    "variant_count": "Spellings",
    "net_cost": "Spend",
    "tag_value_count": "Distinct values",
}

_MAX_ROWS = 40


def _q(value: str) -> str:
    """Escape a string for inlining as a single-quoted SQL literal."""
    return value.replace("'", "''")


def _df(sql: str) -> pd.DataFrame:
    """Query an attribution view, returning empty on any issue (view may be unbuilt)."""
    try:
        return gold_df(sql)
    except Exception:  # noqa: BLE001 - missing/empty view → render the empty state
        return pd.DataFrame()


def _info(text: str) -> None:
    ui.label(text).classes("text-sm").style(f"color:{chrome.INK_MUTED}")


def render(group: str, label: str, end: date, sm: date) -> None:
    """The whole "Attribution" tab body for one provider.

    Draws its own panels — callers must NOT wrap this in ``chrome.panel()`` (the same
    convention ``provider_focus``'s ``extra_tabs`` follow).
    """
    provider = provider_name_for_group(group)
    tag_coverage(group, end, sm)
    tag_breakdown(group, end, sm)
    owners(label, provider)
    projects(label, provider)


# ── Tags ─────────────────────────────────────────────────────────────────────
def tag_coverage(group: str, end: date, sm: date) -> None:
    """How much of the range's spend is attributable at all — the one denominator.

    The breakdowns below drop untagged rows by construction, so without this a
    fully-untagged bill renders as a tidy, complete-looking tag table.

    Skipped (with a rebuild hint) on a lake published before this view existed — see
    :func:`gold_view_published`. The caption is the honest thing to show: the breakdowns
    below are still correct, they just can't say what they omit.
    """
    chrome.section_title("Tags")
    if not gold_view_published(group, "spend_tag_coverage_month"):
        chrome.section_caption(
            "Tag coverage is unavailable until GOLD is rebuilt — run `flashlight transform`."
        )
        return
    cov = _df(
        "SELECT sum(gross_cost) AS gross, sum(tagged_cost) AS tagged, "
        f'sum(untagged_cost) AS untagged FROM "{group}".spend_tag_coverage_month '
        f"WHERE charge_month >= '{sm}' AND charge_month <= '{end}'"
    )
    gross = float(cov["gross"].iloc[0] or 0) if not cov.empty and cov["gross"].notna().any() else 0
    if not gross:
        # Named, not skipped: `tagged_pct` is NULL — not 0 — when a connector reports no
        # tagged cost at all, and rendering "0%" would claim we measured zero coverage
        # when in fact we measured nothing.
        chrome.section_caption(
            "This provider reports no tagged cost for the range, so there is no coverage "
            "denominator — the tag spend below is all this data can attribute, not a share "
            "of the bill."
        )
        return
    tagged = float(cov["tagged"].iloc[0] or 0)
    untagged = float(cov["untagged"].iloc[0] or 0)
    # A KPI row here was tried and reverted: two cards stretched across a full-width grid
    # dwarf a "65% / $73.6K" payload — oversized and mostly empty. A line carries the same
    # two numbers without the empty space a 2-card grid can't help but have.
    chrome.caption_info(
        f"{tagged / gross:.0%} of charges carry a cost-allocation tag — "
        f"{compact_money(untagged)} unattributed, not in the breakdowns below.",
        "Spend below is per tag key, so a resource tagged twice is counted under both "
        "keys — use this coverage figure as the denominator, not a column total from the "
        "table below.",
    )


def _tag_key_rows(group: str, end: date, sm: date) -> pd.DataFrame:
    """Spend per folded tag key for the latest month in range, or empty.

    Latest month rather than the whole range on purpose: ``variant_count`` and
    ``tag_key_variants`` describe how a key is spelled *right now*, and a key renamed
    mid-range would otherwise read as two permanently-colliding spellings.
    """
    if not gold_view_published(group, "spend_by_tag_key_month"):
        return pd.DataFrame()
    return _df(
        f'SELECT * FROM "{group}".spend_by_tag_key_month WHERE charge_month = ('
        f'SELECT max(charge_month) FROM "{group}".spend_by_tag_key_month '
        f"WHERE charge_month >= '{sm}' AND charge_month <= '{end}') "
        "ORDER BY net_cost DESC"
    )


def _tag_key_body(rows: pd.DataFrame, group: str) -> None:
    """The key-ranking title row + table — content only, no panel. Shared by
    :func:`tag_keys` (its own panel, for Redshift's separately-scoped value drill) and
    :func:`tag_breakdown` (same panel as the value drill, for every other provider).
    """
    month_label = pd.Timestamp(rows["charge_month"].iloc[0]).strftime("%b %Y")
    collisions = int((rows["variant_count"] > 1).sum())
    cols = [c for c in _TAG_COLS if c in rows]

    with ui.row().classes("items-center gap-1"):
        chrome.panel_title(f"Spend by tag · {month_label} ({len(rows):,} keys)")
        chrome.info_icon(
            "Keys differing only by case or separator (epic/Epic, app-long/app_long) "
            "are folded into one row; 'Spelled as' shows the raw spellings."
        )
    if collisions:
        chrome.caption_info(
            f"{collisions:,} key(s) spelled multiple ways — see 'Spelled as'.",
            "Worth fixing upstream so the raw views agree too, not just this rollup.",
        )
    chrome.searchable_table(
        rows[cols],
        key=f"{group}_tag_keys",
        search_col="tag_key_normalized",
        money_cols=["net_cost"],
        int_cols=["variant_count", "tag_value_count"],
        rename=_TAG_RENAME,
        max_rows=_MAX_ROWS,
    )


def _no_tagged_spend_panel(title: str) -> None:
    # Named rather than skipped: "this provider tags nothing" is a real finding, and
    # silently omitting the panel looks identical to the feature not existing.
    with chrome.panel():
        chrome.panel_title(title)
        chrome.section_caption(
            "No cost-allocation tags appear on this provider's charges, so none of its "
            "spend can be attributed to a team or project."
        )


def tag_keys(group: str, end: date, sm: date) -> None:
    """Standalone "Spend by tag key" panel. Used only by ``redshift_focus``, which pairs
    it with its own SKU-scoped value drill instead of :func:`tag_breakdown`'s — see
    ``redshift_focus._attribution_section``.
    """
    rows = _tag_key_rows(group, end, sm)
    if rows.empty:
        _no_tagged_spend_panel("Spend by tag key — no tagged spend")
        return
    with chrome.panel():
        _tag_key_body(rows, group)


def _tag_value_rows(group: str, tag_key_normalized: str, end: date, sm: date) -> pd.DataFrame:
    """One folded key's values over the range.

    Filters ``spend_by_tag_month`` with the same case/separator fold
    ``spend_by_tag_key_month`` uses, rather than an exact match on its raw ``tag_key`` —
    otherwise picking "team" would miss dollars raw-tagged "Team", even though the
    ranking above already folded those two into one row.
    """
    return _df(
        "SELECT tag_value, sum(net_cost) AS net_cost FROM "
        f'"{group}".spend_by_tag_month '
        f"WHERE replace(lower(trim(tag_key)), '-', '_') = '{_q(tag_key_normalized)}' "
        f"AND charge_month >= '{sm}' AND charge_month <= '{end}' "
        "GROUP BY tag_value ORDER BY net_cost DESC LIMIT 20"
    )


def tag_breakdown(group: str, end: date, sm: date) -> None:
    """Spend by tag key, then a drill into one key's values — ONE panel.

    Used to be two panels ("Spend by tag key" ranking, "Spend by tag value" drill) that
    read as duplicates because they nearly were: rank a dimension, then open one — that's
    a single flow, not two questions. Folding the value drill's key picker onto the same
    normalized keys the ranking uses also fixed a real inconsistency: the picker used to
    list ``spend_by_tag_month``'s raw, un-folded spellings (Epic and epic as two separate
    options) while the table right above it had already folded them into one row.
    """
    rows = _tag_key_rows(group, end, sm)
    if rows.empty:
        _no_tagged_spend_panel("Spend by tag — no tagged spend")
        return

    with chrome.panel():
        _tag_key_body(rows, group)

        options = rows["tag_key_normalized"].tolist()
        default = "team" if "team" in options else options[0]
        chrome.section_caption("Values for one key:")
        body_container = ui.column().classes("w-full gap-2")

        @ui.refreshable
        def _values_body(sel: str) -> None:
            body_container.clear()
            with body_container:
                tags = _tag_value_rows(group, sel, end, sm)
                if tags.empty:
                    _info(f"No values for `{sel}` in range.")
                    return
                chrome.searchable_table(
                    tags,
                    key=f"{group}_tags",
                    search_col="tag_value",
                    money_cols=["net_cost"],
                    rename={"tag_value": sel, "net_cost": "Net cost"},
                )

        (
            ui.select(
                options=options, value=default, on_change=lambda e: _values_body.refresh(e.value)
            )
            .props("dense outlined")
            .classes("w-48")
            .style(f"color:{chrome.INK_PRIMARY}")
        )
        _values_body(default)


# ── Owners / Projects ────────────────────────────────────────────────────────
def _owner_rows(dimension: str, provider: str) -> tuple[pd.DataFrame, str]:
    """The latest month of one owner dimension for one provider, plus its label.

    *provider* is the raw FOCUS ``provider_name`` (``data.provider_name_for_group``), never
    the display label — ``"AWS Redshift"`` matches no row.
    """
    if not gold_view_published("efficiency", "waste_by_owner_month"):
        return pd.DataFrame(), ""
    rows = _df(
        "SELECT * FROM efficiency.waste_by_owner_month "
        f"WHERE owner_dimension = '{dimension}' AND provider_name = '{_q(provider)}'"
    )
    if rows.empty:
        return rows, ""
    months = sorted(rows["charge_month"].astype(str).unique())
    month = months[-1]
    latest = rows[rows["charge_month"].astype(str) == month]
    return latest, pd.Timestamp(month).strftime("%b %Y")


def _attributed_pct(rows: pd.DataFrame) -> float | None:
    """Share of recoverable $ that has an owner. By dollars, not row count.

    Deliberately not a row-count share: the unattributed bucket is one row but hundreds
    of findings, so counting rows would report ~90% attributed on data that is mostly not.
    """
    total = float(rows["recoverable_cost"].sum())
    if total <= 0:
        return None
    unattributed = float(
        rows.loc[rows["owner_key"] == _UNATTRIBUTED_KEY, "recoverable_cost"].sum()
    )
    return 100 * (total - unattributed) / total


def _owner_table(rows: pd.DataFrame, *, key: str, rename: dict[str, str]) -> None:
    """Both lenses' rankings in ONE table (a "Lens" column keeps them apart), grouped by
    lens (WASTE block, then OPPORTUNITY), Unattributed pinned first within each.

    Renders table content only — no :func:`chrome.panel`, no title. Callers own the
    panel and put their KPI numbers (:func:`chrome.stat_row`) above this as the same
    card's header, so one finding is one container instead of "KPIs in one card, table
    in another." Merging the rows into one table doesn't merge their dollars: WASTE and
    OPPORTUNITY stay two separate column sums, never one.
    """
    if rows.empty:
        return
    # Sort key, not a concat: keeps the frame a single sorted object so the CSV export
    # carries the same order the reader saw.
    ordered = rows.assign(
        _lens_order=rows["lens"].map(_LENS_ORDER).fillna(len(_LENS_ORDER)),
        _pin=(rows["owner_key"] != _UNATTRIBUTED_KEY).astype(int),
    ).sort_values(["_lens_order", "_pin", "recoverable_cost"], ascending=[True, True, False])
    display = ordered.assign(
        owner_kind=ordered["owner_kind"].map(lambda k: _KIND_LABELS.get(str(k), str(k))),
        lens=ordered["lens"].map(lambda lens: _LENS_LABELS.get(str(lens), str(lens))),
    )
    cols = [c for c in _OWNER_COLS if c in display]
    chrome.searchable_table(
        display[cols],
        key=f"lb_{key}",
        search_col="owner_display",
        money_cols=["recoverable_cost", "recoverable_cost_high_confidence", "billed_cost"],
        int_cols=["entity_count", "finding_count"],
        rename=rename,
        max_rows=_MAX_ROWS,
    )


def _no_findings(label: str) -> None:
    _info(
        f"No waste findings for {label} yet. Owner attribution comes from an efficiency pull "
        "— run `flashlight ingest` with a Databricks or Redshift connector configured."
    )


def owners(label: str, provider: str) -> None:
    chrome.section_title("Ownership")
    rows, month_label = _owner_rows("owner_user", provider)
    if not gold_view_published("efficiency", "waste_by_owner_month"):
        _info(_STALE_MSG)
        return
    if rows.empty:
        _no_findings(label)
        return

    unattributed = float(
        rows.loc[rows["owner_key"] == _UNATTRIBUTED_KEY, "recoverable_cost"].sum()
    )
    pct = _attributed_pct(rows)
    n_people = int(rows.loc[rows["owner_kind"] == "user", "owner_key"].nunique())
    n_sp = int(rows.loc[rows["owner_kind"] == "service_principal", "owner_key"].nunique())

    with chrome.panel():
        with ui.row().classes("items-center gap-1"):
            chrome.panel_title("Waste & opportunity by owner")
            chrome.info_icon(
                "Ranked by recoverable $ within each lens, unowned spend pinned first. "
                "WASTE (tune it) and OPPORTUNITY (move it) are different remedies for the "
                "same entity, so they're never summed into one figure."
            )
        chrome.section_caption(f"Showing {month_label} — the latest month with findings.")
        chrome.stat_row(
            [
                (
                    "Attributed to an owner",
                    f"{pct:.1f}%" if pct is not None else "—",
                    "of recoverable $",
                    "rate",
                ),
                (
                    "Unowned",
                    compact_money(unattributed),
                    "shared compute — no owner by design",
                    "unattributed",
                ),
                (
                    "Distinct owners",
                    f"{n_people + n_sp:,}",
                    f"{n_people:,} people · {n_sp:,} service principals "
                    "(automation, not people)",
                ),
            ]
        )
        chrome.caption_info(
            "Unowned is shared compute, not missing data.",
            "Owner names are normalized in GOLD — case-folded and whitespace-trimmed, so "
            "one person is one row — and bare UUIDs are labelled as service principals "
            "rather than left looking like a colleague. SQL warehouses are shared compute "
            "with no per-entity owner, which is why they land in Unowned by design.",
        )
        _owner_table(rows, key="owner", rename=_OWNER_RENAME)


def projects(label: str, provider: str) -> None:
    rows, month_label = _owner_rows("owner_project", provider)
    if not gold_view_published("efficiency", "waste_by_owner_month") or rows.empty:
        # The owners section above already explained the absence — saying it twice on one
        # tab reads as two broken panels rather than one missing pull.
        return

    total = float(rows["recoverable_cost"].sum())
    pct = _attributed_pct(rows)
    n_projects = int(rows.loc[rows["owner_kind"] == "project", "owner_key"].nunique())

    with chrome.panel():
        with ui.row().classes("items-center gap-1"):
            chrome.panel_title("Waste & opportunity by project")
            chrome.info_icon(
                "Ranked by recoverable $ within each lens, unowned spend pinned first. "
                "WASTE (tune it) and OPPORTUNITY (move it) are different remedies for the "
                "same entity, so they're never summed into one figure."
            )
        chrome.stat_row(
            [
                (
                    "Attributed to a project",
                    f"{pct:.1f}%" if pct is not None else "—",
                    f"of {compact_money(total)} recoverable",
                    # Deliberately the "unattributed" colour even when the number is high:
                    # this stat exists to make a low figure impossible to skim past.
                    "unattributed",
                ),
                ("Projects", f"{n_projects:,}", "distinct project tags"),
                ("Recoverable", compact_money(total), f"{month_label} · all projects"),
            ]
        )
        # Not an empty_state: the rows exist, they just have no project tag. Calling that
        # "no data" would suggest a broken pull rather than an un-tagged fleet.
        chrome.caption_info(
            "A low share here means tagging is missing upstream, not that spend is unknown.",
            "Project attribution comes from a cost-allocation tag, so anything untagged "
            "lands in Unattributed — the same dollars are fully attributed by owner above, "
            "and by tag key at the top of this tab.",
        )
        _owner_table(rows, key="project", rename=_PROJECT_RENAME)
